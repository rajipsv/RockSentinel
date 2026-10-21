"""Console API handlers as plain functions (framework-free).

The local dev server (server.py) calls these; in Azure the same functions sit behind
Static Web Apps managed Functions. They only read/write the golden-set files and run
the offline evals. Nothing here talks to GitHub.
"""

from __future__ import annotations

from pathlib import Path

from rocksentinel.baseline.rules import CATEGORIES
from rocksentinel.evals import labeler
from rocksentinel.evals.labeler import Paths, _read_jsonl
from rocksentinel.evals.runner import evaluate
from rocksentinel.therock_ci.extract import normalize_line

EVIDENCE_CONTEXT = 8
MAX_EVIDENCE_LINES = 300


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _summary(case: dict) -> dict:
    e = case.get("expected", {})
    return {
        "case_id": case["case_id"],
        "status": case.get("label_status") or case.get("status"),
        "run_id": case.get("run_id"),
        "job_id": case.get("job_id"),
        "workflow": case.get("workflow"),
        "job_name": case.get("job_name"),
        "job_url": case.get("job_url") or (
            f"https://github.com/{case.get('repo', 'ROCm/TheRock')}/actions/runs/"
            f"{case.get('run_id')}/job/{case.get('job_id')}" if case.get("job_id") else None
        ),
        "label_source": case.get("label_source"),
        "label_strength": "strong" if str(case.get("label_source", "")).startswith("outcome")
        else "weak",
        "category": e.get("category"),
        "component": e.get("component"),
        "gpu_family": e.get("gpu_family"),
        "platform": e.get("platform"),
        "failing_test": e.get("failing_test"),
        "failing_kernel": e.get("failing_kernel"),
        "must_cite": e.get("must_cite", []),
        "context": case.get("context"),
        "review_note": case.get("review_note"),
    }


def list_proposals(paths: Paths) -> dict:
    proposals = _read_jsonl(paths.proposals)
    return {
        "categories": list(CATEGORIES),
        "count": len(proposals),
        "runs": len({p.get("run_id") for p in proposals}),
        "items": [_summary(p) for p in proposals],
    }


def list_golden(paths: Paths) -> dict:
    cases = _read_jsonl(paths.dataset)
    return {
        "count": len(cases),
        "confirmed": sum(c.get("label_status") == "confirmed" for c in cases),
        "items": [_summary(c) for c in cases],
    }


def _find(case_id: str, paths: Paths) -> dict:
    for case in _read_jsonl(paths.proposals) + _read_jsonl(paths.dataset):
        if case["case_id"] == case_id:
            return case
    raise ApiError(404, f"unknown case {case_id}")


def _safe_fixture(case: dict, paths: Paths) -> Path:
    fixtures = paths.fixtures.resolve()
    path = (paths.root / case["log_fixture"]).resolve()
    if fixtures not in path.parents:
        raise ApiError(400, "fixture path outside tests/fixtures/logs")
    if not path.is_file():
        raise ApiError(404, "log fixture not found")
    return path


def evidence(case_id: str, paths: Paths) -> dict:
    """Log segments around the must-cite lines, with cite lines flagged."""
    case = _find(case_id, paths)
    lines = [normalize_line(x) for x in
             _safe_fixture(case, paths).read_text(encoding="utf-8").splitlines()]
    cites = case.get("expected", {}).get("must_cite", [])
    hits = [i for i, line in enumerate(lines) if any(c and c in line for c in cites)]
    fallback = not hits
    if fallback:  # nothing to anchor on: show the tail, but say so (it is not evidence)
        hits = [len(lines) - 1] if lines else []

    ranges: list[list[int]] = []
    for i in hits:
        lo, hi = max(0, i - EVIDENCE_CONTEXT), min(len(lines) - 1, i + EVIDENCE_CONTEXT)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])

    segments, shown = [], 0
    hit_set = set() if fallback else set(hits)
    for lo, hi in ranges:
        if shown >= MAX_EVIDENCE_LINES:
            break
        hi = min(hi, lo + MAX_EVIDENCE_LINES - shown - 1)
        segments.append({"lines": [
            {"n": n + 1, "text": lines[n], "cite": n in hit_set} for n in range(lo, hi + 1)
        ]})
        shown += hi - lo + 1
    return {"case_id": case_id, "total_lines": len(lines), "segments": segments,
            "fallback": fallback}


def accept(case_id: str, body: dict, paths: Paths) -> dict:
    category = body.get("category") or None
    note = (body.get("note") or "").strip() or None
    try:
        case = labeler.accept(case_id, category=category, note=note,
                              reviewer="human reviewer (console)", paths=paths)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    return _summary(case)


def reject(case_id: str, paths: Paths) -> dict:
    try:
        labeler.reject(case_id, paths=paths)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    return {"case_id": case_id, "rejected": True}


def evals(paths: Paths, confirmed_only: bool = False, split: str = "all") -> dict:
    cases = _read_jsonl(paths.dataset)
    if confirmed_only:
        cases = [c for c in cases if c.get("label_status") == "confirmed"]
    if split in ("dev", "test"):
        cases = [c for c in cases if c.get("split") == split]
    if not cases:
        return {"n_cases": 0, "n_confirmed": 0, "systems": {}, "cases": []}
    return evaluate(cases, root=paths.root)


# --- Runs, Case and Signature views (step 8) ------------------------------------------------
# These read the job store (poller) and memory; case evidence is fetched live through the
# same therock-ci functions the agents use (read-only GitHub), then cached per job.

_EVIDENCE_CACHE: dict[int, dict] = {}


def list_runs(jobs, limit: int = 50) -> dict:
    runs = jobs.recent_runs(limit=max(1, min(limit, 200)))
    return {"count": len(runs), "items": runs}


def run_detail(jobs, run_id: int) -> dict:
    from collections import defaultdict

    all_jobs = jobs.jobs_for_run(run_id)
    if not all_jobs:
        raise ApiError(404, f"run {run_id} not in the job store")
    failed = [j for j in all_jobs if j.failed]
    groups: dict = defaultdict(list)
    for j in failed:
        groups[j.runner_label or "?"].append({
            "job_id": j.job_id, "job_name": j.job_name, "component": j.component,
            "gpu_family": j.gpu_family, "platform": j.platform, "conclusion": j.conclusion,
            "attempt": j.attempt})
    by_pool = sorted(({"runner_label": k, "failed": v,
                       "components": sorted({x["component"] for x in v if x["component"]})}
                      for k, v in groups.items()), key=lambda g: -len(g["failed"]))
    first = all_jobs[0]
    return {"run_id": run_id, "workflow": first.workflow, "head_sha": first.head_sha,
            "branch": first.branch, "jobs": len(all_jobs), "failed": len(failed),
            "by_runner_pool": by_pool,
            "platform_event_hint": any(len(g["components"]) >= 3 for g in by_pool),
            "run_url": f"https://github.com/{first.repo}/actions/runs/{run_id}"}


def case_detail(jobs, memory, job_id: int, fetch_evidence: bool = True) -> dict:
    """One failed job: history, deterministic (B1) verdict, evidence and memory match."""
    from rocksentinel.baseline.rules import classify
    from rocksentinel.memory.signatures import make_signature
    from rocksentinel.therock_ci import server as therock
    from rocksentinel.therock_ci.history import job_history

    job = jobs.get_job(job_id)
    if job is None:
        raise ApiError(404, f"job {job_id} not in the job store")
    history = job_history(jobs, job_id)
    out = {"job": history["job"], "history": history,
           "job_url": f"https://github.com/{job.repo}/actions/runs/{job.run_id}/job/{job_id}",
           "evidence": None, "b1": None, "memory": None}
    if not (fetch_evidence and job.failed):
        return out
    if job_id not in _EVIDENCE_CACHE:
        try:
            _EVIDENCE_CACHE[job_id] = therock.get_job_log_windows(job_id, repo=job.repo)
        except Exception as exc:  # e.g. log expired (404) or no token
            raise ApiError(502, f"could not fetch log: {exc}") from exc
    ev = _EVIDENCE_CACHE[job_id]
    kinds = [w["anchor"]["kind"] for w in ev["windows"]]
    category, reason = classify(kinds, workflow=job.workflow,
                                likely_flaky=history["flake_evidence"]["likely_flaky"])
    first_text = next((w["anchor"]["text"] for w in ev["windows"]
                       if w["anchor"]["kind"] not in ("exit_code", "generic_error")), None)
    out.update(evidence=ev, b1={"category": category, "reason": reason})
    if first_text:
        sig = make_signature(first_text, job.component)
        out["memory"] = {"signature": sig, "match": memory.lookup_signature(sig),
                         "similar": memory.search_similar(first_text, k=3)}
    return out


def list_signatures(memory, limit: int = 100) -> dict:
    items = memory.list_signatures(limit=max(1, min(limit, 500)))
    return {"count": len(items), "items": items}
