"""Step 8: Console Runs / Case / Signature views (offline: recorded GitHub log)."""

import pytest
from _recorded import JOB_ID, RUN_ID, recorded_transport

from rocksentinel.console import api
from rocksentinel.github.client import GitHubClient
from rocksentinel.memory.store import MemoryStore
from rocksentinel.store.base import JobResult
from rocksentinel.store.sqlite_store import SQLiteStore
from rocksentinel.therock_ci import server as therock


def _job(job_id, comp, conclusion="failure", runner="linux-gfx1151-gpu-rocm"):
    return JobResult(run_id=RUN_ID, attempt=1, job_id=job_id, repo="ROCm/TheRock",
                     workflow="test_artifacts.yml", job_name=f"Test {comp} (gfx1151)",
                     component=comp, gpu_family="gfx1151", platform="linux", shard=None,
                     runner_label=runner, conclusion=conclusion,
                     started_at="2026-10-19T13:00:00Z", finished_at="2026-10-19T13:38:00Z",
                     duration_s=60, head_sha="f79fc42", branch="main", event="dispatch")


@pytest.fixture
def stores():
    jobs, mem = SQLiteStore(":memory:"), MemoryStore(":memory:")
    jobs.upsert_jobs([_job(JOB_ID, "rocsparse"), _job(2, "miopen"), _job(3, "rocblas"),
                      _job(4, "rocprim", conclusion="success"),
                      _job(5, "rocfft", runner="windows-gfx110X-gpu-rocm")])
    client = GitHubClient("t", transport=recorded_transport())
    therock.set_github_client(client)
    api._EVIDENCE_CACHE.clear()
    yield jobs, mem
    therock.set_github_client(None)
    client.close()
    jobs.close()
    mem.close()


def test_runs_and_run_detail(stores):
    jobs, _ = stores
    runs = api.list_runs(jobs)
    assert runs["items"][0]["run_id"] == RUN_ID and runs["items"][0]["failed"] == 4
    d = api.run_detail(jobs, RUN_ID)
    assert d["by_runner_pool"][0]["runner_label"] == "linux-gfx1151-gpu-rocm"
    assert d["by_runner_pool"][0]["components"] == ["miopen", "rocblas", "rocsparse"]
    assert d["platform_event_hint"] is True
    with pytest.raises(api.ApiError):
        api.run_detail(jobs, 1)


def _run(run_id, workflow, day, conclusions):
    """One run of `workflow` starting on `day` with one job per conclusion."""
    return [JobResult(run_id=run_id, attempt=1, job_id=run_id * 100 + i, repo="ROCm/TheRock",
                      workflow=workflow, job_name=f"job {i}", component=None, gpu_family=None,
                      platform="linux", shard=None, runner_label="r", conclusion=c,
                      started_at=f"{day}T0{i}:00:00Z", finished_at=f"{day}T0{i}:30:00Z",
                      duration_s=60, head_sha="abc", branch="main", event="push")
            for i, c in enumerate(conclusions)]


@pytest.fixture
def many_runs():
    jobs = SQLiteStore(":memory:")
    jobs.upsert_jobs(_run(10, "ci.yml", "2026-10-18", ["success", "failure"])
                     + _run(11, "ci.yml", "2026-10-19", ["success", "success"])
                     + _run(12, "ci.yml", "2026-10-20", ["timed_out"])
                     + _run(20, "nightly.yml", "2026-10-20", ["success"]))
    yield jobs
    jobs.close()


def test_workflow_cards_summary(many_runs):
    w = api.list_workflows(many_runs)
    assert w["data_range"] == {"first": "2026-10-18", "last": "2026-10-20"}
    ci, nightly = w["items"]                     # most failing first
    assert (ci["workflow"], ci["runs"], ci["failed_runs"], ci["failed_jobs"]) == ("ci.yml", 3, 2, 2)
    assert ci["failure_rate"] == pytest.approx(0.667)
    assert [r["run_id"] for r in ci["recent"]] == [12, 11, 10]   # newest first
    assert ci["last_run_at"].startswith("2026-10-20")
    assert (nightly["runs"], nightly["failed_runs"]) == (1, 0)


def test_date_filters_one_day_and_range(many_runs):
    one_day = api.list_workflows(many_runs, since="2026-10-19", until="2026-10-19")
    assert [(w["workflow"], w["runs"]) for w in one_day["items"]] == [("ci.yml", 1)]
    rng = api.list_runs(many_runs, workflow="ci.yml", since="2026-10-19", until="2026-10-20")
    assert [r["run_id"] for r in rng["items"]] == [12, 11]
    open_ended = api.list_runs(many_runs, since="2026-10-20")
    assert {r["run_id"] for r in open_ended["items"]} == {12, 20}
    assert api.list_runs(many_runs, until="2026-10-18")["count"] == 1
    assert api.list_workflows(many_runs, since="2026-11-01")["items"] == []


@pytest.mark.parametrize("since,until", [("2026/10/19", None), ("x", None),
                                         (None, "2026-10-1"), ("2026-10-20", "2026-10-19")])
def test_bad_dates_rejected(many_runs, since, until):
    with pytest.raises(api.ApiError) as e:
        api.list_runs(many_runs, since=since, until=until)
    assert e.value.status == 400


def test_http_workflows_and_runs_filters(many_runs, tmp_path):
    import json
    import threading
    import urllib.error
    import urllib.request

    from rocksentinel.console.server import make_server

    srv = make_server(port=0, stores={"jobs": many_runs})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/workflows?since=2026-10-20&until=2026-10-20") as r:
            assert {w["workflow"] for w in json.load(r)["items"]} == {"ci.yml", "nightly.yml"}
        with urllib.request.urlopen(base + "/api/runs?workflow=ci.yml&since=2026-10-19") as r:
            assert [x["run_id"] for x in json.load(r)["items"]] == [12, 11]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/runs?since=yesterday")
        assert e.value.code == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_case_detail_fetches_evidence_and_classifies(stores):
    jobs, mem = stores
    c = api.case_detail(jobs, mem, JOB_ID)
    assert c["b1"]["category"] == "regression"
    assert c["evidence"]["faulting_kernel"].startswith("rocsparse::csrgemm_bitmap_emit_kernel")
    assert c["memory"]["match"] is None  # new signature
    assert c["history"]["same_run_co_failures"]["platform_event_hint"] is True
    assert JOB_ID in api._EVIDENCE_CACHE   # second view is served from cache
    light = api.case_detail(jobs, mem, JOB_ID, fetch_evidence=False)
    assert light["evidence"] is None


def test_case_detail_shows_memory_match(stores):
    jobs, mem = stores
    first = api.case_detail(jobs, mem, JOB_ID)
    sig_text = next(w["anchor"]["text"] for w in first["evidence"]["windows"]
                    if w["anchor"]["kind"] not in ("exit_code", "generic_error"))
    mem.upsert_case({"case_key": "x", "first_error": sig_text, "component": "rocsparse",
                     "gpu_family": "gfx1151", "category": "regression"})
    again = api.case_detail(jobs, mem, JOB_ID)
    assert again["memory"]["match"]["count"] == 1
    sigs = api.list_signatures(mem)
    assert sigs["count"] == 1 and sigs["items"][0]["component"] == "rocsparse"


def test_case_unknown_job_and_missing_log(stores):
    jobs, mem = stores
    with pytest.raises(api.ApiError) as e1:
        api.case_detail(jobs, mem, 999)
    assert e1.value.status == 404
    with pytest.raises(api.ApiError) as e2:
        api.case_detail(jobs, mem, 2)    # recorded transport has no log for job 2 -> 404
    assert e2.value.status == 502
