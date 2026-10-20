"""Step 1: poller + local SQLite job store, using recorded TheRock API responses."""

from dataclasses import replace

import httpx
import pytest
from _recorded import JOB_ID, RUN_ID, recorded_transport

from rocksentinel.github.client import GitHubClient, JobRef, RunRef
from rocksentinel.ingest.poller import poll_once, to_job_result
from rocksentinel.store.sqlite_store import SQLiteStore


@pytest.fixture
def store():
    s = SQLiteStore(":memory:")
    yield s
    s.close()


def _run() -> RunRef:
    return RunRef(
        run_id=RUN_ID, run_number=3351, attempt=1, workflow="test_artifacts.yml",
        title="Test Artifacts", head_sha="f79fc42", branch="main", event="workflow_dispatch",
        conclusion="failure", created_at="2026-10-19T08:17:00Z", html_url="https://x",
    )


def _job(**over) -> JobRef:
    base = JobRef(
        job_id=JOB_ID, run_id=RUN_ID, attempt=1,
        name="Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)", conclusion="failure",
        started_at="2026-10-19T13:35:00Z", completed_at="2026-10-19T13:37:39Z",
        runner_label="linux-gfx1151-gpu-rocm", html_url="https://x",
    )
    return replace(base, **over)


# --- mapping -------------------------------------------------------------------------------

def test_to_job_result_maps_fields_and_duration():
    jr = to_job_result("ROCm/TheRock", _run(), _job())
    assert (jr.component, jr.gpu_family, jr.platform, jr.shard) == (
        "rocsparse", "gfx1151", "linux", "2/3"
    )
    assert jr.duration_s == 159
    assert jr.failed and jr.case_key == f"{RUN_ID}:1:{JOB_ID}"


def test_success_is_not_failed():
    assert not to_job_result("ROCm/TheRock", _run(), _job(conclusion="success")).failed


# --- store ---------------------------------------------------------------------------------

def test_store_upsert_is_idempotent(store):
    jr = to_job_result("ROCm/TheRock", _run(), _job())
    store.upsert_jobs([jr])
    store.upsert_jobs([replace(jr, conclusion="success")])  # rerun result overwrites
    assert store.count_jobs() == 1
    assert store.get_job(JOB_ID).conclusion == "success"
    assert store.jobs_for_run(RUN_ID) == [store.get_job(JOB_ID)]


def test_queue_is_idempotent(store):
    jr = to_job_result("ROCm/TheRock", _run(), _job())
    assert store.enqueue(jr) is True
    assert store.enqueue(jr) is False
    assert store.pending() == [jr.case_key]


def test_state_roundtrip(store):
    assert store.get_state("k") is None
    store.set_state("k", "a")
    store.set_state("k", "b")
    assert store.get_state("k") == "b"


def test_store_creates_db_file(tmp_path):
    db = tmp_path / "nested" / "rs.db"
    SQLiteStore(db).close()
    assert db.exists()


# --- poller --------------------------------------------------------------------------------

def test_poll_once_stores_all_jobs_and_queues_failures(store):
    with GitHubClient("t", transport=recorded_transport()) as gh:
        result = poll_once(gh, store, store, store, since_hours=24)
    jobs = store.jobs_for_run(RUN_ID)
    failed = [j for j in jobs if j.failed]
    assert result.runs_seen == 1
    assert result.jobs_written == len(jobs) == store.count_jobs()
    assert any(not j.failed for j in jobs)  # passes are stored too (for analytics)
    assert result.failures_queued == len(failed) == len(store.pending())
    rocsparse = store.get_job(JOB_ID)
    assert (rocsparse.component, rocsparse.gpu_family, rocsparse.platform) == (
        "rocsparse", "gfx1151", "linux"
    )
    assert result.cursor and store.get_state("poller:ROCm/TheRock:last_created_at")


def test_poll_twice_is_idempotent_and_uses_cursor_with_lookback(store):
    calls: list[httpx.Request] = []
    with GitHubClient("t", transport=recorded_transport(calls)) as gh:
        first = poll_once(gh, store, store, store)
        count = store.count_jobs()
        second = poll_once(gh, store, store, store)
    assert store.count_jobs() == count
    assert second.failures_queued == 0
    run_calls = [c for c in calls if c.url.path.endswith("/actions/runs")]
    assert run_calls[0].url.params["status"] == "completed"
    # second poll starts 6h before the saved cursor (late-finishing runs)
    assert first.cursor is not None
    cursor_hour = int(first.cursor[11:13])
    assert run_calls[-1].url.params["created"].startswith(">=")
    assert int(run_calls[-1].url.params["created"][13:15]) == (cursor_hour - 6) % 24
