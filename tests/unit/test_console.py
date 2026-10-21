"""Console v0: API functions and the local HTTP server (no network beyond 127.0.0.1)."""

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rocksentinel.console import api
from rocksentinel.console.server import make_server
from rocksentinel.evals.labeler import Paths

REPO = Path(__file__).resolve().parents[2]
PROPOSAL = {
    "case_id": "cand-1", "status": "proposed", "repo": "ROCm/TheRock", "run_id": 9, "attempt": 1,
    "job_id": 1, "workflow": "test_artifacts.yml", "job_name": "Test x (gfx1151)",
    "job_url": "https://github.com/ROCm/TheRock/actions/runs/9/job/1",
    "log_fixture": "tests/fixtures/logs/cand1.txt",
    "label_source": "rules (B1): ... - VERIFY against the log",
    "expected": {"category": "regression", "component": "x", "gpu_family": "gfx1151",
                 "platform": "linux", "failing_test": "T.t", "failing_kernel": None,
                 "must_cite": ["Memory access fault by GPU"]},
    "context": {"failing_since": None, "co_failures": 0, "platform_event_hint": False},
}


@pytest.fixture
def paths(tmp_path):
    (tmp_path / "evals" / "golden").mkdir(parents=True)
    logs = tmp_path / "tests" / "fixtures" / "logs"
    logs.mkdir(parents=True)
    rows = [json.loads(x) for x in (REPO / "evals/golden/dataset.jsonl").read_text("utf-8")
            .splitlines() if x][:2]
    for r in rows:
        shutil.copy(REPO / r["log_fixture"], tmp_path / r["log_fixture"])
    (tmp_path / "evals/golden/dataset.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    lines = [f"2026-10-19T13:40:{i:02d}.0Z noise {i}" for i in range(40)]
    lines[20] = "2026-10-19T13:40:20.0Z 16: Memory access fault by GPU node-1 on address 0x1."
    (logs / "cand1.txt").write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "evals/golden/proposals.jsonl").write_text(json.dumps(PROPOSAL) + "\n", "utf-8")
    return Paths(root=tmp_path)


def test_list_proposals_and_golden(paths):
    p = api.list_proposals(paths)
    assert p["count"] == 1 and p["runs"] == 1 and "regression" in p["categories"]
    assert p["items"][0]["label_strength"] == "weak"
    g = api.list_golden(paths)
    assert g["count"] == 2 and g["items"][0]["case_id"] == "golden-001"


def test_evidence_highlights_cite_lines_normalized(paths):
    ev = api.evidence("cand-1", paths)
    lines = [ln for s in ev["segments"] for ln in s["lines"]]
    cited = [ln for ln in lines if ln["cite"]]
    assert len(cited) == 1 and cited[0]["n"] == 21
    assert cited[0]["text"].startswith("Memory access fault")  # timestamp + ctest prefix gone
    assert len(lines) == 2 * api.EVIDENCE_CONTEXT + 1


def test_evidence_without_cites_is_flagged_as_fallback(paths):
    row = dict(PROPOSAL, case_id="cand-3")
    row["expected"] = dict(PROPOSAL["expected"], must_cite=[])
    with paths.proposals.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    ev = api.evidence("cand-3", paths)
    assert ev["fallback"] is True
    assert not any(ln["cite"] for s in ev["segments"] for ln in s["lines"])
    assert api.evidence("cand-1", paths)["fallback"] is False


def test_evidence_rejects_fixture_outside_fixtures_dir(paths):
    bad = dict(PROPOSAL, case_id="cand-2", log_fixture="../../etc/passwd")
    with paths.proposals.open("a", encoding="utf-8") as f:
        f.write(json.dumps(bad) + "\n")
    with pytest.raises(api.ApiError) as exc:
        api.evidence("cand-2", paths)
    assert exc.value.status == 400


def test_accept_and_reject_via_api(paths):
    case = api.accept("cand-1", {"category": "infra", "note": "runner died"}, paths)
    assert case["case_id"] == "golden-003" and case["category"] == "infra"
    assert case["review_note"] == "runner died"
    assert api.list_proposals(paths)["count"] == 0
    with pytest.raises(api.ApiError) as exc:
        api.reject("cand-1", paths)
    assert exc.value.status == 404


def test_accept_bad_category_is_400(paths):
    with pytest.raises(api.ApiError) as exc:
        api.accept("cand-1", {"category": "banana"}, paths)
    assert exc.value.status == 400


def test_evals_endpoint_shape(paths):
    r = api.evals(paths)
    assert r["n_cases"] == 2 and set(r["systems"]) == {"B0", "B1"}
    assert api.evals(paths, confirmed_only=True)["n_cases"] == 0
    assert api.evals(paths, split="test")["n_cases"] == sum(
        json.loads(x).get("split") == "test"
        for x in paths.dataset.read_text("utf-8").splitlines() if x)


# --- HTTP ----------------------------------------------------------------------------------

@pytest.fixture
def server(paths):
    srv = make_server(port=0, paths=paths, web_dir=REPO / "web")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.headers.get("Content-Type"), res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_http_static_and_api(server):
    status, ctype, body = _req(server + "/")
    assert status == 200 and ctype.startswith("text/html") and b"RockSentinel" in body
    status, _, body = _req(server + "/api/proposals")
    assert status == 200 and json.loads(body)["count"] == 1
    status, _, _ = _req(server + "/api/cases/cand-1/evidence")
    assert status == 200


def test_http_accept_and_errors(server):
    status, _, body = _req(server + "/api/proposals/cand-1/accept", "POST", {"note": "ok"})
    assert status == 200 and json.loads(body)["case_id"] == "golden-003"
    assert _req(server + "/api/proposals/cand-1/reject", "POST", {})[0] == 404
    assert _req(server + "/api/nope")[0] == 404
    assert _req(server + "/../pyproject.toml")[0] == 404


def test_http_blocks_cross_origin_writes(server):
    status, _, _ = _req(server + "/api/proposals/cand-1/reject", "POST", {},
                        headers={"Origin": "https://evil.example"})
    assert status == 403
