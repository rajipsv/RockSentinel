"""Step 2: job_history (failing-since, rerun/flake evidence, same-run co-failures)."""

import json

import httpx
import pytest
from _recorded import recorded_transport
from mcp import Client

from rocksentinel.ci_evidence import server
from rocksentinel.ci_evidence.history import job_history
from rocksentinel.github.client import GitHubClient
from rocksentinel.ingest.poller import poll_once
from rocksentinel.store.base import JobResult
from rocksentinel.store.sqlite_store import SQLiteStore

ROCSPARSE = "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)"
GFX1151 = "linux-gfx1151-gpu-rocm"


def _job(job_id, run_id, attempt, conclusion, finished, sha, *, name=ROCSPARSE,
         component="rocsparse", runner=GFX1151):
    return JobResult(
        run_id=run_id, attempt=attempt, job_id=job_id, repo="ROCm/TheRock",
        workflow="test_artifacts.yml", job_name=name, component=component,
        gpu_family="gfx1151", platform="linux", shard="2/3", runner_label=runner,
        conclusion=conclusion, started_at=finished, finished_at=finished, duration_s=60,
        head_sha=sha, branch="main", event="schedule",
    )


@pytest.fixture
def store():
    """A history shaped like the real rocsparse/gfx1151 case."""
    s = SQLiteStore(":memory:")
    s.upsert_jobs([
        _job(1000, 100, 1, "success", "2026-10-16T10:00:00Z", "aaa"),
        # run 101: failed, then passed on rerun (same commit) -> flaky in the past
        _job(1011, 101, 1, "failure", "2026-10-17T10:00:00Z", "bbb"),
        _job(1012, 101, 2, "success", "2026-10-17T12:00:00Z", "bbb"),
        # current streak starts here
        _job(1020, 102, 1, "failure", "2026-10-18T23:47:00Z", "ccc"),
        _job(1031, 103, 1, "failure", "2026-10-19T13:38:17Z", "ddd"),
        # run 103 co-failures: three more components on the same gfx1151 runner pool
        _job(1032, 103, 1, "failure", "2026-10-19T13:30:00Z", "ddd",
             name="Test miopen / Test miopen (shard 2/4) (gfx1151)", component="miopen"),
        _job(1033, 103, 1, "failure", "2026-10-19T13:31:00Z", "ddd",
             name="Test rocblas / Test rocblas (shard 4/6) (gfx1151)", component="rocblas"),
        _job(1034, 103, 1, "timed_out", "2026-10-19T13:32:00Z", "ddd",
             name="Test hipblaslt / Test hipblaslt (shard 3/6) (gfx1151)", component="hipblaslt"),
        _job(1035, 103, 1, "success", "2026-10-19T13:33:00Z", "ddd",
             name="Test rocprim / Test rocprim (gfx1151)", component="rocprim"),
        _job(1036, 103, 1, "failure", "2026-10-19T13:34:00Z", "ddd",
             name="Test rocfft / Test rocfft (gfx110X-all)", component="rocfft",
             runner="windows-gfx110X-gpu-rocm"),
    ])
    yield s
    s.close()


def test_failing_since_and_last_success(store):
    h = job_history(store, 1031)
    assert h["failing_since"]["run_id"] == 102
    assert h["failing_since"]["finished_at"] == "2026-10-18T23:47:00Z"
    assert h["failing_since"]["streak_runs"] == 2
    assert h["last_success"]["run_id"] == 101 and h["last_success"]["attempt"] == 2
    assert h["failure_rate"] == 0.5  # finals: 100 ok, 101 ok (after rerun), 102 fail, 103 fail
    assert [r["run_id"] for r in h["recent"]] == [103, 102, 101, 100]


def test_current_failure_is_not_flagged_flaky(store):
    flake = job_history(store, 1031)["flake_evidence"]
    assert flake["passed_on_rerun_same_run"] is None  # not rerun yet
    assert flake["same_sha_passed_elsewhere"] is False
    assert flake["history_mixed_shas"] == ["bbb"]  # context: run 101's commit
    assert flake["history_rerun_passes"] == 1
    assert flake["likely_flaky"] is False


def test_failure_that_passed_on_rerun_is_flaky(store):
    flake = job_history(store, 1011)["flake_evidence"]
    assert flake["passed_on_rerun_same_run"] is True
    assert flake["same_sha_passed_elsewhere"] is True
    assert flake["likely_flaky"] is True


def test_same_run_co_failures_and_platform_hint(store):
    co = job_history(store, 1031)["same_run_co_failures"]
    assert co["count"] == 4
    assert co["components"] == ["hipblaslt", "miopen", "rocblas", "rocfft"]
    assert co["by_runner_label"] == {GFX1151: 3, "windows-gfx110X-gpu-rocm": 1}
    assert co["jobs_in_attempt"] == 6
    assert co["platform_event_hint"] is True


def test_isolated_failure_has_no_platform_hint():
    s = SQLiteStore(":memory:")
    s.upsert_jobs([
        _job(1, 9, 1, "failure", "2026-10-19T10:00:00Z", "x"),
        _job(2, 9, 1, "success", "2026-10-19T10:00:00Z", "x", name="Test a (gfx1151)",
             component="a"),
    ])
    h = job_history(s, 1)
    assert h["same_run_co_failures"]["platform_event_hint"] is False
    assert h["last_success"] is None and h["failing_since"]["streak_runs"] == 1


def test_unknown_job_raises(store):
    with pytest.raises(LookupError, match="run the poller"):
        job_history(store, 424242)


def test_poller_requests_all_attempts():
    calls: list[httpx.Request] = []
    s = SQLiteStore(":memory:")
    with GitHubClient("t", transport=recorded_transport(calls)) as gh:
        poll_once(gh, s, s, s)
    job_calls = [c for c in calls if c.url.path.endswith("/jobs")]
    assert job_calls and all(c.url.params["filter"] == "all" for c in job_calls)


# --- through the MCP server --------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_job_history_mcp_tool(store):
    server.set_store(store)
    try:
        async with Client(server.mcp) as client:
            ok = await client.call_tool("job_history", {"job_id": 1031})
            missing = await client.call_tool("job_history", {"job_id": 424242})
    finally:
        server.set_store(None)
    data = ok.structured_content or json.loads(ok.content[0].text)
    assert data["failing_since"]["run_id"] == 102
    assert data["same_run_co_failures"]["platform_event_hint"] is True
    assert missing.is_error
