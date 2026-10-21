"""Coverage report: classification of logs and sampling outside the golden set."""

import json

from _recorded import JOB_ID, recorded_transport

from rocksentinel.evals.coverage import classify_coverage, coverage
from rocksentinel.evals.labeler import Paths
from rocksentinel.github.client import GitHubClient
from rocksentinel.store.base import JobResult
from rocksentinel.store.sqlite_store import SQLiteStore


def test_classify_coverage():
    assert classify_coverage("Memory access fault by GPU node-1\n") == (
        "specific", "gpu_memory_fault")
    assert classify_coverage("tool X: fatal: device lost\n") == ("generic_only", "generic_error")
    assert classify_coverage("Process completed with exit code 1.\n") == ("none", None)


def _job(job_id, name):
    return JobResult(
        run_id=1, attempt=1, job_id=job_id, repo="ROCm/TheRock", workflow="w.yml",
        job_name=name, component=None, gpu_family=None, platform=None, shard=None,
        runner_label=None, conclusion="failure", started_at="2026-10-20T00:00:00Z",
        finished_at="2026-10-20T00:01:00Z", duration_s=60, head_sha="s", branch="main",
        event="push")


def test_coverage_samples_outside_golden_and_skips_missing_logs(tmp_path):
    (tmp_path / "evals" / "golden").mkdir(parents=True)
    (tmp_path / "evals/golden/dataset.jsonl").write_text(
        json.dumps({"case_id": "golden-001", "job_id": 7}) + "\n", encoding="utf-8")
    store = SQLiteStore(":memory:")
    store.upsert_jobs([_job(JOB_ID, "rocsparse"), _job(7, "in golden"), _job(999, "no log")])
    with GitHubClient("t", transport=recorded_transport()) as gh:
        report = coverage(store, gh, sample=10, paths=Paths(root=tmp_path))
    assert report["sampled"] == 1 and report["skipped_no_log"] == 1
    assert report["rows"][0]["job_id"] == JOB_ID
    assert report["specific_rate"] == 1.0 and report["todo"] == []
    store.close()
