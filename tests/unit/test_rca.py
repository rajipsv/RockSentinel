"""Step 7: find_introducing_change + RCA agent (replay), on the real golden-001 trace (mocked)."""

import json

import httpx
import pytest

from rocksentinel.agents.rca import RcaResult, build_spec, rca
from rocksentinel.agents.triage import TriageResult
from rocksentinel.github.client import GitHubClient
from rocksentinel.llm.provider import LLMResponse, ReplayProvider, ToolCall
from rocksentinel.therock_ci import server as therock
from rocksentinel.therock_ci.changes import find_introducing_change, search_terms

KERNEL = "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>"
CULPRIT = "183759b92ee5b9817967b246f80a586cdf10a83f"
CULPRIT_MSG = ("perf(rocsparse): bitmap path for csrgemm rows whose hash table does not fit in "
               "shared memory (#12830)")
# Real TheRock bumps of rocm-libraries around the regression (newest first, as the API lists).
BUMPS = [("9032", "3dc098e", "8a29d2e", "2026-10-16"), ("8986", "24bdd07", "3dc098e", "2026-10-14"),
         ("8945", "01f6f9c", "24bdd07", "2026-10-13")]
CONTAINS = {"24bdd07": "behind", "3dc098e": "ahead", "8a29d2e": "ahead", "01f6f9c": "behind"}


def github_mock(calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        path = request.url.path
        if path == "/search/commits":
            return httpx.Response(200, json={"items": [{
                "sha": CULPRIT, "html_url": f"https://github.com/ROCm/rocm-libraries/commit/{CULPRIT}",
                "commit": {"message": CULPRIT_MSG + "\n\nbody", "committer": {
                    "date": "2026-10-13T14:39:40Z"}}}]})
        if path == "/repos/ROCm/TheRock/commits":
            return httpx.Response(200, json=[{
                "sha": f"{pr}abcdef", "html_url": f"https://github.com/ROCm/TheRock/pull/{pr}",
                "commit": {"message": f"Bump rocm-libraries from {old} to {new} (#{pr})",
                           "committer": {"date": f"{date}T12:00:00Z"}}}
                for pr, old, new, date in BUMPS])
        if path.startswith("/repos/ROCm/rocm-libraries/compare/"):
            head = path.rsplit("...", 1)[1]
            return httpx.Response(200, json={"status": CONTAINS[head]})
        return httpx.Response(404, json={"message": "Not Found"})
    return httpx.MockTransport(handler)


def test_search_terms():
    assert search_terms(KERNEL) == ["csrgemm", "bitmap"]
    # "f64" is shorter than the 4-character minimum for a search word
    assert search_terms("internal_extra_csrgemm_bitmap.calc_tiled_f64_i64_i64") == [
        "calc", "tiled"]
    assert search_terms("kernel") == []


def test_traces_kernel_to_commit_and_bump():
    calls = []
    with GitHubClient("t", transport=github_mock(calls)) as gh:
        r = find_introducing_change(gh, KERNEL)
    cand = r["candidates"][0]
    assert cand["sha"] == CULPRIT and cand["message"] == CULPRIT_MSG
    via = cand["entered_therock_via"]
    assert (via["pr"], via["from"], via["to"]) == ("8986", "24bdd07", "3dc098e")
    q = [c for c in calls if c.url.path == "/search/commits"][0].url.params["q"]
    assert q == "repo:ROCm/rocm-libraries csrgemm bitmap"
    assert [c for c in calls if c.url.path == "/repos/ROCm/TheRock/commits"][0].url.params[
        "path"] == "rocm-libraries"


def test_no_terms_and_no_bumps_are_reported_not_raised():
    with GitHubClient("t", transport=github_mock()) as gh:
        assert find_introducing_change(gh, "kernel")["notes"] == [
            "no distinctive words in symbol; nothing to search"]


@pytest.fixture
def mocked_github():
    client = GitHubClient("t", transport=github_mock())
    therock.set_github_client(client)
    yield client
    therock.set_github_client(None)
    client.close()


def test_server_tool(mocked_github):
    out = therock.find_introducing_change(KERNEL, since_days=500)
    assert out["candidates"][0]["entered_therock_via"]["pr"] == "8986"


def test_rca_replay_on_golden_001(mocked_github):
    tools = {
        "find_introducing_change": therock.find_introducing_change,
        "search_similar_failures": lambda error_text, k=3: [],
        "job_history": lambda job_id: {"failing_since": {"finished_at": "2026-10-18T23:47:00Z"}},
    }
    triaged = TriageResult(category="regression", component="rocsparse", gpu_family="gfx1151",
                           platform="linux", confidence=0.8, needs_rca=True)
    final = {
        "root_cause_hypothesis": "Out-of-bounds access in the new csrgemm bitmap path.",
        "suspected_change": {"repo": "ROCm/rocm-libraries", "commit": "183759b",
                             "pr": "12830", "therock_bump_pr": "8986"},
        "evidence": [{"text": CULPRIT_MSG}],
        "suggested_next_step": "Run the test on gfx110X/gfx94X at the same commit.",
        "confidence": 0.7,
    }
    provider = ReplayProvider([
        LLMResponse(tool_calls=(ToolCall("1", "search_similar_failures",
                                         {"error_text": "Memory access fault"}),)),
        LLMResponse(tool_calls=(ToolCall("2", "find_introducing_change", {"symbol": KERNEL}),)),
        LLMResponse(tool_calls=(ToolCall("3", "job_history", {"job_id": "118078343556"}),)),
        LLMResponse(content=json.dumps(final)),
    ])
    job = {"job_id": 118078343556, "run_id": 39249671429, "workflow": "test_artifacts.yml",
           "job_name": "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)"}
    res = rca(job, triaged, provider, tools)
    assert res.status == "ok", res.events
    out: RcaResult = res.output
    assert out.suspected_change.pr == "12830" and out.suspected_change.therock_bump_pr == "8986"
    assert out.evidence[0].text == CULPRIT_MSG and out.confidence == 0.7   # grounded
    assert '"triage": ' not in provider.calls[0][1]["content"] or "regression" in (
        provider.calls[0][1]["content"])
    assert not any(t.writes for t in build_spec(tools).tools.values())
