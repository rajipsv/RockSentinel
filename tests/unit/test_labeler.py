"""Step 4: labelling helper (propose / accept / reject), on recorded responses."""

import json
import shutil
from pathlib import Path

import pytest
from _recorded import JOB_ID, RUN_ID, recorded_transport

from rocksentinel.evals import labeler
from rocksentinel.evals.labeler import Paths, _diverse, accept, fixture_excerpt, propose, reject
from rocksentinel.github.client import GitHubClient
from rocksentinel.store.base import JobResult
from rocksentinel.store.sqlite_store import SQLiteStore

REPO_ROOT = Path(__file__).resolve().parents[2]


def _job(job_id, name, *, component="rocsparse", family="gfx1151", platform="linux",
         conclusion="failure", run_id=RUN_ID):
    return JobResult(
        run_id=run_id, attempt=1, job_id=job_id, repo="ROCm/TheRock",
        workflow="test_artifacts.yml", job_name=name, component=component, gpu_family=family,
        platform=platform, shard=None, runner_label="linux-gfx1151-gpu-rocm",
        conclusion=conclusion, started_at="2026-10-19T13:35:00Z",
        finished_at="2026-10-19T13:38:00Z", duration_s=180, head_sha="f79fc42",
        branch="main", event="schedule",
    )


@pytest.fixture
def paths(tmp_path):
    golden = tmp_path / "evals" / "golden"
    golden.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "evals" / "golden" / "dataset.jsonl", golden / "dataset.jsonl")
    return Paths(root=tmp_path)


@pytest.fixture
def store():
    s = SQLiteStore(":memory:")
    s.upsert_jobs([
        _job(JOB_ID, "Test rocsparse / Test rocsparse (shard 2/3) (gfx1151)"),
        _job(1, "Test passing (gfx1151)", conclusion="success"),
    ])
    yield s
    s.close()


@pytest.fixture
def gh():
    with GitHubClient("t", transport=recorded_transport()) as client:
        yield client


def _rows(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def test_propose_writes_proposal_and_fixture(store, gh, paths):
    # golden-001 already covers JOB_ID, so make the dataset forget it for this test
    rows = [r for r in _rows(paths.dataset) if r.get("job_id") != JOB_ID]
    paths.dataset.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    new = propose(store, gh, max_cases=5, paths=paths)
    assert [p["case_id"] for p in new] == [f"cand-{JOB_ID}"]
    p = new[0]
    assert p["status"] == "proposed"
    assert p["expected"]["category"] == "regression"
    kernel = "rocsparse::csrgemm_bitmap_emit_kernel<256u, long, long>"
    assert p["expected"]["failing_kernel"] == kernel
    assert p["label_source"].startswith("rules (B1)") and "VERIFY" in p["label_source"]
    fixture = paths.root / p["log_fixture"]
    text = fixture.read_text(encoding="utf-8")
    assert all(c in text for c in p["expected"]["must_cite"])
    cites = p["expected"]["must_cite"]
    assert "MEMORY_FAULT" in cites[0]  # the fault ranks first
    assert any("Memory access fault by GPU" in c for c in cites)
    assert _rows(paths.proposals)[0]["case_id"] == p["case_id"]


def test_propose_skips_jobs_without_logs(gh, paths):
    s = SQLiteStore(":memory:")
    s.upsert_jobs([_job(999, "Test nolog (gfx1151)")])  # recorded transport -> 404 log
    assert propose(s, gh, max_cases=5, paths=paths) == []
    s.close()


def test_propose_skips_jobs_already_in_dataset(store, gh, paths):
    assert propose(store, gh, max_cases=5, paths=paths) == []  # golden-001 is this job


def test_accept_confirms_and_numbers_case(store, gh, paths):
    rows = [r for r in _rows(paths.dataset) if r.get("job_id") != JOB_ID]
    paths.dataset.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    propose(store, gh, paths=paths)
    highest = max(int(r["case_id"].split("-")[1]) for r in rows)
    expected_id = f"golden-{highest + 1:03d}"

    case = accept(f"cand-{JOB_ID}", note="checked the log", paths=paths)
    assert case["case_id"] == expected_id
    assert case["label_status"] == "confirmed" and case["review_note"] == "checked the log"
    assert _rows(paths.proposals) == []
    assert _rows(paths.dataset)[-1]["case_id"] == expected_id


def test_accept_with_category_override(store, gh, paths):
    rows = [r for r in _rows(paths.dataset) if r.get("job_id") != JOB_ID]
    paths.dataset.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    propose(store, gh, paths=paths)
    case = accept(f"cand-{JOB_ID}", category="infra", paths=paths)
    assert case["expected"]["category"] == "infra"
    assert case["label_source"] == "human override (proposed: regression)"
    with pytest.raises(KeyError):
        accept("cand-missing", paths=paths)


def test_reject_removes_proposal_and_fixture(store, gh, paths):
    rows = [r for r in _rows(paths.dataset) if r.get("job_id") != JOB_ID]
    paths.dataset.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    p = propose(store, gh, paths=paths)[0]
    fixture = paths.root / p["log_fixture"]
    assert fixture.exists()
    reject(p["case_id"], paths=paths)
    assert not fixture.exists() and _rows(paths.proposals) == []


def test_invalid_category_rejected(store, gh, paths):
    rows = [r for r in _rows(paths.dataset) if r.get("job_id") != JOB_ID]
    paths.dataset.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    propose(store, gh, paths=paths)
    with pytest.raises(ValueError):
        accept(f"cand-{JOB_ID}", category="banana", paths=paths)


def test_per_run_cap_and_rebalance(paths, monkeypatch):
    s = SQLiteStore(":memory:")
    s.upsert_jobs([
        _job(10 + i, f"Test c{i} (gfx1151)", component=f"c{i}", run_id=500) for i in range(4)
    ] + [_job(20, "Test d (gfx90a)", component="d", family="gfx90a", run_id=600)])

    def fake_case(store, gh, job, paths):
        return {"case_id": f"cand-{job.job_id}", "run_id": job.run_id, "job_id": job.job_id,
                "log_fixture": f"tests/fixtures/logs/x-{job.job_id}.txt"}

    monkeypatch.setattr(labeler, "propose_case", fake_case)
    new = propose(s, None, max_cases=10, paths=paths, max_per_run=2)
    runs = [p["run_id"] for p in new]
    assert runs.count(500) == 2 and runs.count(600) == 1

    # proposals written before the cap existed get trimmed by rebalance
    over = [{"case_id": f"cand-{n}", "run_id": 700, "job_id": n,
             "log_fixture": f"tests/fixtures/logs/x-{n}.txt"} for n in (1, 2, 3)]
    paths.proposals.write_text("".join(json.dumps(r) + "\n" for r in over), encoding="utf-8")
    assert labeler.rebalance(paths, max_per_run=2) == ["cand-3"]
    assert [p["case_id"] for p in _rows(paths.proposals)] == ["cand-1", "cand-2"]
    s.close()


def test_refresh_rederives_rule_labels_but_keeps_outcome_labels(paths):
    logs = paths.fixtures
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "a.txt").write_text(
        "hipModuleLoad failed: /tmp/x.hsaco\n error: file not found\n", encoding="utf-8")
    base = {"run_id": 1, "workflow": "test_artifacts.yml",
            "log_fixture": "tests/fixtures/logs/a.txt",
            "expected": {"category": "infra", "must_cite": ["error: file not found"]}}
    rows = [dict(base, case_id="cand-1", job_id=1, label_source="rules (B1): old"),
            dict(base, case_id="cand-2", job_id=2,
                 label_source="outcome: passed on rerun of the same run (strong)",
                 expected={"category": "flaky", "must_cite": []})]
    paths.proposals.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    assert labeler.refresh(paths) == ["cand-1", "cand-2"]
    after = {r["case_id"]: r for r in _rows(paths.proposals)}
    assert after["cand-1"]["expected"]["category"] == "regression"  # hip_runtime_error
    assert after["cand-1"]["expected"]["must_cite"] == ["hipModuleLoad failed: /tmp/x.hsaco"]
    assert after["cand-2"]["expected"]["category"] == "flaky"  # outcome label kept
    assert after["cand-2"]["expected"]["must_cite"] == ["hipModuleLoad failed: /tmp/x.hsaco"]


def test_diverse_prefers_new_combos_and_dedupes_names():
    jobs = [
        _job(1, "A (gfx1151)"),
        _job(2, "A (gfx1151)"),  # same job name -> dropped
        _job(3, "B (gfx1151)"),  # same combo as 1 -> second pass
        _job(4, "C (gfx90a)", component="rocfft", family="gfx90a"),
    ]
    assert [j.job_id for j in _diverse(jobs, skip_ids=set(), limit=10)] == [1, 4, 3]
    assert [j.job_id for j in _diverse(jobs, skip_ids={1}, limit=2)] == [2, 4]


def test_fixture_excerpt_keeps_small_logs_whole():
    log = "\n".join(f"line {i}" for i in range(100))
    assert fixture_excerpt(log) == log.splitlines()


def test_fixture_excerpt_starts_before_earliest_top_error_not_top_ranked():
    lines = [f"noise {i}" for i in range(6000)]
    lines[2000] = "FAIL: gdb.rocm/a.exp: wait for gpu stops (timeout)"  # earlier, per-test
    lines[5900] = "Memory access fault by GPU node-1"  # later, ranked higher
    out = fixture_excerpt("\n".join(lines))
    assert lines[2000] in out and lines[5900] in out


def test_fixture_excerpt_trims_large_logs_but_keeps_error_and_tail():
    lines = [f"noise {i}" for i in range(10_000)]
    lines[5000] = "Memory access fault by GPU node-1 on address 0x1."
    lines[-1] = "Error: Process completed with exit code 8."
    out = fixture_excerpt("\n".join(lines))
    assert len(out) <= labeler.MAX_FIXTURE_LINES + 1
    assert lines[5000] in out and out[-1] == lines[-1]
    assert any("trimmed by labeler" in x for x in out)


def test_fixture_excerpt_keeps_errors_in_the_middle_of_a_long_log():
    # rockrel hipblaslt shape: noise early, the hang mid-log, the ctest summary later, and
    # a long tail; head+tail trimming used to cut out both failure lines.
    lines = [f"noise {i}" for i in range(10_600)]
    lines[197] = "HEAD is now at 93291d1 update nightly status -> failure"  # generic noise
    lines[5428] = "HW Exception by GPU node-1 (Agent handle: 0x5e) reason :GPU Hang"
    lines[9113] = "\t  3 - hipblaslt-test_comprehensive_suite (Subprocess aborted)"
    lines[-1] = "Error: Process completed with exit code 8."
    out = fixture_excerpt("\n".join(lines))
    assert lines[5428] in out and lines[9113] in out and out[-1] == lines[-1]
    assert len(out) <= labeler.MAX_FIXTURE_LINES
