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
