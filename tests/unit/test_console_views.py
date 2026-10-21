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
