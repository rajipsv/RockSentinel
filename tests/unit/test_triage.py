"""Deterministic triage: headlines, root-cause choice, offline worker over saved excerpts."""

from rocksentinel.store.base import JobResult
from rocksentinel.store.sqlite_store import SQLiteStore
from rocksentinel.triage import headline, triage_log
from rocksentinel.triage.worker import fixture_logs, triage_from_fixtures


def _job(job_id, conclusion="failure"):
    return JobResult(run_id=7, attempt=1, job_id=job_id, repo="ROCm/TheRock",
                     workflow="test_artifacts.yml", job_name="Test rocsparse (gfx1151)",
                     component="rocsparse", gpu_family="gfx1151", platform="linux", shard=None,
                     runner_label="r", conclusion=conclusion, started_at="2026-10-19T13:00:00Z",
                     finished_at="2026-10-19T13:30:00Z", duration_s=60, head_sha="abc",
                     branch="main", event="push")


def test_headlines_are_plain_sentences():
    assert headline("gpu_memory_fault", "Memory access fault", kernel="k", failing_test="T.t",
                    where="gfx1151 · linux") == (
        "GPU memory access fault in kernel k while running T.t on gfx1151 · linux; "
        "the test process died.")
    assert headline("gpu_hw_exception", "HW Exception by GPU node-1 reason : GPU Hang") \
        .startswith("GPU hardware exception (GPU Hang)")
    assert headline("step_timeout", "x", step_timeout=15) == "A CI step timed out after 15 minutes."
    two = headline("ctest_failed_list", "The following tests FAILED:",
                   failed_tests=["a (Failed)", "b (Failed)"])
    assert two == "Test failure: a (Failed) (+1 more)"
    assert headline(None, None).startswith("No error lines recognised")
    assert len(headline("crash", "x" * 500)) < 220   # long lines are shortened


def test_timeout_verdict_points_at_the_timeout_line():
    store = SQLiteStore(":memory:")
    store.upsert_jobs([_job(1)])
    log = "\n".join([
        "2026-10-19T13:00:00Z error: something noisy happened",
        "2026-10-19T13:30:00Z ##[error]The action 'Run tests' has timed out after 15 minutes.",
        "2026-10-19T13:30:01Z ##[error]Process completed with exit code 1."])
    row = triage_log(store.get_job(1), log, store, source="saved excerpt")
    assert row["category"] == "timeout" and row["root_kind"] == "step_timeout"
    assert row["headline"].startswith("A CI step timed out after 15 minutes")
    store.close()


def test_offline_worker_triages_saved_excerpts_without_network(tmp_path):
    job_id, path = next(iter(fixture_logs().items()))   # any real saved excerpt
    (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "run-1_no-job-id.txt").write_text("ignored", encoding="utf-8")
    store = SQLiteStore(":memory:")
    store.upsert_jobs([_job(job_id), _job(99, conclusion="success")])
    assert triage_from_fixtures(store, tmp_path) == {"triaged": 1, "skipped": 0}
    row = store.get_triage(job_id)
    assert row["source"] == "saved excerpt" and row["headline"] and row["evidence"]["windows"]
    assert store.failed_job_count() == (1, 1)
    store.close()
