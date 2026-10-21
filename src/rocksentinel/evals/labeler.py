"""Labelling helper: propose golden cases from the job store; a human reviews them.

    python -m rocksentinel.evals.labeler propose --max 10   # fetch logs, write proposals
    python -m rocksentinel.evals.labeler list                # show pending proposals
    python -m rocksentinel.evals.labeler accept cand-123 [--category infra] [--note "..."]
    python -m rocksentinel.evals.labeler reject cand-123

Proposed labels come from B1's rules, so accepting them unchecked would grade B1
against itself. Each proposal therefore says where its label came from
(`label_source`): outcome-based sources (e.g. passed on rerun) are strong, rule-based
ones must be verified against the log. Only `accept`ed cases are marked `confirmed`.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rocksentinel.baseline.rules import CATEGORIES, classify
from rocksentinel.github.client import GitHubClient, GitHubError
from rocksentinel.store.base import JobResult, JobStore
from rocksentinel.therock_ci.extract import extract_errors
from rocksentinel.therock_ci.history import job_history
from rocksentinel.therock_ci.mappers import map_symbol_to_component

ROOT = Path(__file__).resolve().parents[3]
MAX_FIXTURE_LINES = 1500
CONTEXT_BEFORE_FIRST_WINDOW = 50
EXCERPT_TOP_CANDIDATES = 5


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT

    @property
    def dataset(self) -> Path:
        return self.root / "evals" / "golden" / "dataset.jsonl"

    @property
    def proposals(self) -> Path:
        return self.root / "evals" / "golden" / "proposals.jsonl"

    @property
    def fixtures(self) -> Path:
        return self.root / "tests" / "fixtures" / "logs"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def fixture_excerpt(log_text: str) -> list[str]:
    """Keep enough of the raw log for both baselines to be scored fairly.

    Starts a little before the first ranked error and runs to the end of the log (where
    B0 looks), trimming the middle only when the result would be huge.
    """
    lines = log_text.splitlines()
    if len(lines) <= MAX_FIXTURE_LINES:
        return lines
    # Start before the EARLIEST of the top-ranked errors, not before the single top one:
    # a suite's final summary can outrank the per-test failure lines printed earlier.
    top = extract_errors(log_text, max_candidates=EXCERPT_TOP_CANDIDATES).candidates
    first_start = min((c.window[0] for c in top), default=1)
    start = max(0, first_start - 1 - CONTEXT_BEFORE_FIRST_WINDOW) if top else 0
    segment = lines[start:]
    if len(segment) <= MAX_FIXTURE_LINES:
        return segment
    head, tail = 900, MAX_FIXTURE_LINES - 900
    return segment[:head] + ["... [middle trimmed by labeler] ..."] + segment[-tail:]


def _diverse(jobs: list[JobResult], skip_ids: set[int], limit: int) -> list[JobResult]:
    """One job per (workflow, job name), preferring new component/family/platform combos."""
    seen_names: set[tuple[str, str]] = set()
    seen_combo: set[tuple] = set()
    first_pass, second_pass = [], []
    for job in jobs:
        if job.job_id in skip_ids or (job.workflow, job.job_name) in seen_names:
            continue
        seen_names.add((job.workflow, job.job_name))
        combo = (job.component, job.gpu_family, job.platform)
        (second_pass if combo in seen_combo else first_pass).append(job)
        seen_combo.add(combo)
    return (first_pass + second_pass)[:limit]


def _must_cite(result) -> list[str]:
    cites: list[str] = []
    for c in result.candidates:
        if c.kind != "exit_code" and c.text not in cites:
            cites.append(c.text)
        if len(cites) == 3:
            break
    return cites


def refresh(paths: Paths | None = None) -> list[str]:
    """Re-derive pending proposals from their saved logs after extractor/rule changes.

    Outcome-based labels (passed on rerun, ...) keep their category; rule-based ones are
    re-classified. Must-cite lines, failing test and kernel are always re-extracted.
    """
    paths = paths or Paths()
    proposals = _read_jsonl(paths.proposals)
    changed = []
    for p in proposals:
        fixture = paths.root / p["log_fixture"]
        if not fixture.is_file():
            continue
        result = extract_errors(fixture.read_text(encoding="utf-8"))
        expected = dict(p["expected"])
        expected["must_cite"] = _must_cite(result)
        expected["failing_test"] = result.failing_test
        expected["failing_kernel"] = result.faulting_kernel
        source = p.get("label_source", "")
        if not source.startswith("outcome"):
            category, reason = classify([c.kind for c in result.candidates],
                                        workflow=p["workflow"])
            expected["category"] = category
            source = f"rules (B1): {reason} - VERIFY against the log"
        if expected != p["expected"] or source != p.get("label_source"):
            p["expected"], p["label_source"] = expected, source
            changed.append(p["case_id"])
    _write_jsonl(paths.proposals, proposals)
    return changed


def _save_fixture(paths: Paths, *, repo: str, run_id: int, attempt: int, job_id: int,
                  job_name: str, workflow: str, log: str) -> tuple[Path, list[str]]:
    excerpt = fixture_excerpt(log)
    fixture = paths.fixtures / f"run-{run_id}_job-{job_id}.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"# Source: {repo} Actions run {run_id} attempt {attempt}, job {job_id}",
        f"# Job: {job_name} ({workflow})",
        f"# Excerpt saved by rocksentinel.evals.labeler on {datetime.now(UTC):%Y-%m-%d}",
    ]
    fixture.write_text("\n".join(header + excerpt) + "\n", encoding="utf-8")
    return fixture, excerpt


def refetch(case_id: str, gh: GitHubClient, paths: Paths | None = None) -> dict:
    """Re-download a pending proposal's log with the current excerpt logic, then refresh it."""
    paths = paths or Paths()
    p = next((x for x in _read_jsonl(paths.proposals) if x["case_id"] == case_id), None)
    if p is None:
        raise KeyError(f"no proposal {case_id}")
    _save_fixture(paths, repo=p["repo"], run_id=p["run_id"], attempt=p.get("attempt", 1),
                  job_id=p["job_id"], job_name=p["job_name"], workflow=p["workflow"],
                  log=gh.get_job_log(p["repo"], p["job_id"]))
    refresh(paths)
    return next(x for x in _read_jsonl(paths.proposals) if x["case_id"] == case_id)


def propose_case(store: JobStore, gh: GitHubClient, job: JobResult, paths: Paths) -> dict:
    fixture, excerpt = _save_fixture(
        paths, repo=job.repo, run_id=job.run_id, attempt=job.attempt, job_id=job.job_id,
        job_name=job.job_name, workflow=job.workflow, log=gh.get_job_log(job.repo, job.job_id),
    )

    result = extract_errors("\n".join(excerpt))
    history = job_history(store, job.job_id)
    flake = history["flake_evidence"]
    category, reason = classify(
        [c.kind for c in result.candidates], workflow=job.workflow,
        likely_flaky=flake["likely_flaky"],
    )
    if flake["passed_on_rerun_same_run"]:
        source = "outcome: passed on rerun of the same run (strong)"
    elif flake["same_sha_passed_elsewhere"]:
        source = "outcome: same commit passed in another run (strong)"
    else:
        source = f"rules (B1): {reason} - VERIFY against the log"

    must_cite = _must_cite(result)
    component = job.component or (
        map_symbol_to_component(result.faulting_kernel).component
        if result.faulting_kernel else None
    )
    return {
        "case_id": f"cand-{job.job_id}",
        "status": "proposed",
        "repo": job.repo,
        "run_id": job.run_id,
        "attempt": job.attempt,
        "job_id": job.job_id,
        "workflow": job.workflow,
        "job_name": job.job_name,
        "runner_label": job.runner_label,
        "head_sha": job.head_sha,
        "failed_at": job.finished_at,
        "job_url": f"https://github.com/{job.repo}/actions/runs/{job.run_id}/job/{job.job_id}",
        "log_fixture": fixture.relative_to(paths.root).as_posix(),
        "label_source": source,
        "expected": {
            "category": category,
            "component": component,
            "gpu_family": job.gpu_family,
            "platform": job.platform,
            "failing_test": result.failing_test,
            "failing_kernel": result.faulting_kernel,
            "must_cite": must_cite,
        },
        "context": {
            "failing_since": history["failing_since"],
            "co_failures": history["same_run_co_failures"]["count"],
            "platform_event_hint": history["same_run_co_failures"]["platform_event_hint"],
        },
    }


DEFAULT_MAX_PER_RUN = 2


def propose(store: JobStore, gh: GitHubClient, max_cases: int = 10,
            paths: Paths | None = None, max_per_run: int = DEFAULT_MAX_PER_RUN) -> list[dict]:
    """Propose up to `max_cases` new cases, at most `max_per_run` per CI run.

    Failures in one run often share a cause (one runner pool, one commit), so a per-run
    cap keeps the golden set from being dominated by a few correlated cases.
    """
    paths = paths or Paths()
    existing = _read_jsonl(paths.dataset) + _read_jsonl(paths.proposals)
    skip = {c["job_id"] for c in existing if c.get("job_id")}
    per_run = Counter(c["run_id"] for c in existing if c.get("run_id"))
    candidates = _diverse(store.failed_jobs(limit=1000), skip, limit=1000)
    new: list[dict] = []
    for job in candidates:
        if len(new) >= max_cases:
            break
        if per_run[job.run_id] >= max_per_run:
            continue
        try:
            new.append(propose_case(store, gh, job, paths))
            per_run[job.run_id] += 1
        except GitHubError as exc:
            # e.g. 404: the job failed before any step ran, or its log has expired
            print(f"skipped job {job.job_id}: {exc}")
    _write_jsonl(paths.proposals, _read_jsonl(paths.proposals) + new)
    return new


def rebalance(paths: Paths | None = None, max_per_run: int = DEFAULT_MAX_PER_RUN) -> list[str]:
    """Drop pending proposals beyond `max_per_run` per run (keeps the earliest proposed)."""
    paths = paths or Paths()
    per_run = Counter(c["run_id"] for c in _read_jsonl(paths.dataset) if c.get("run_id"))
    dropped = []
    for p in _read_jsonl(paths.proposals):
        if per_run[p["run_id"]] >= max_per_run:
            reject(p["case_id"], paths)
            dropped.append(p["case_id"])
        else:
            per_run[p["run_id"]] += 1
    return dropped


def _next_golden_id(dataset: list[dict]) -> str:
    nums = [int(c["case_id"].split("-")[1]) for c in dataset
            if c.get("case_id", "").startswith("golden-")]
    return f"golden-{max(nums, default=0) + 1:03d}"


def accept(case_id: str, *, category: str | None = None, note: str | None = None,
           reviewer: str = "human reviewer", paths: Paths | None = None) -> dict:
    paths = paths or Paths()
    proposals = _read_jsonl(paths.proposals)
    match = next((p for p in proposals if p["case_id"] == case_id), None)
    if match is None:
        raise KeyError(f"no proposal {case_id}")
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    dataset = _read_jsonl(paths.dataset)
    case = {k: v for k, v in match.items() if k not in ("status",)}
    case["case_id"] = _next_golden_id(dataset)
    case["split"] = "dev"
    case["label_status"] = "confirmed"
    case["labelled_by"] = f"{reviewer} via labeler"
    case["labelled_on"] = f"{datetime.now(UTC):%Y-%m-%d}"
    if category:
        case["expected"] = {**case["expected"], "category": category}
        case["label_source"] = f"human override (proposed: {match['expected']['category']})"
    if note:
        case["review_note"] = note
    _write_jsonl(paths.dataset, dataset + [case])
    _write_jsonl(paths.proposals, [p for p in proposals if p["case_id"] != case_id])
    return case


def reject(case_id: str, paths: Paths | None = None) -> None:
    paths = paths or Paths()
    proposals = _read_jsonl(paths.proposals)
    match = next((p for p in proposals if p["case_id"] == case_id), None)
    if match is None:
        raise KeyError(f"no proposal {case_id}")
    _write_jsonl(paths.proposals, [p for p in proposals if p["case_id"] != case_id])
    fixture = paths.root / match["log_fixture"]
    if fixture.exists():
        fixture.unlink()


def format_proposals(proposals: list[dict]) -> str:
    if not proposals:
        return "no pending proposals"
    out = []
    for p in proposals:
        e = p["expected"]
        out.append(
            f"{p['case_id']}  [{e['category']}]  {e['component']} | {e['gpu_family']} | "
            f"{e['platform']}\n"
            f"    job:    {p['job_name']} ({p['workflow']})\n"
            f"    why:    {p['label_source']}\n"
            f"    test:   {e['failing_test']}   kernel: {e['failing_kernel']}\n"
            f"    cites:  {e['must_cite'][:2]}\n"
            f"    log:    {p['job_url']}"
        )
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose and review golden eval cases.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_prop = sub.add_parser("propose")
    p_prop.add_argument("--max", type=int, default=10)
    p_prop.add_argument("--db", default=".data/rocksentinel.db")
    p_prop.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN)
    sub.add_parser("list")
    sub.add_parser("refresh", help="re-derive pending proposals after extractor/rule changes")
    p_ref = sub.add_parser("refetch", help="re-download a proposal's log, then refresh it")
    p_ref.add_argument("case_id")
    p_reb = sub.add_parser("rebalance", help="drop pending proposals beyond the per-run cap")
    p_reb.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN)
    p_acc = sub.add_parser("accept")
    p_acc.add_argument("case_id")
    p_acc.add_argument("--category", choices=CATEGORIES)
    p_acc.add_argument("--note")
    p_rej = sub.add_parser("reject")
    p_rej.add_argument("case_id")
    args = parser.parse_args()

    if args.cmd == "propose":
        from rocksentinel.store.sqlite_store import SQLiteStore

        store = SQLiteStore(args.db)
        with GitHubClient() as gh:
            new = propose(store, gh, args.max, max_per_run=args.max_per_run)
        store.close()
        print(format_proposals(new))
        print(f"\n{len(new)} new proposals in {Paths().proposals}")
    elif args.cmd == "list":
        proposals = _read_jsonl(Paths().proposals)
        print(format_proposals(proposals))
        print(f"\n{len(proposals)} pending from {len({p['run_id'] for p in proposals})} runs")
    elif args.cmd == "refetch":
        with GitHubClient() as gh:
            p = refetch(args.case_id, gh)
        print(format_proposals([p]))
    elif args.cmd == "refresh":
        changed = refresh()
        print(f"refreshed {len(changed)} proposals: {changed}")
    elif args.cmd == "rebalance":
        dropped = rebalance(max_per_run=args.max_per_run)
        print(f"dropped {len(dropped)} over-represented proposals: {dropped}")
    elif args.cmd == "accept":
        case = accept(args.case_id, category=args.category, note=args.note)
        print(f"accepted as {case['case_id']} [{case['expected']['category']}] (confirmed)")
    elif args.cmd == "reject":
        reject(args.case_id)
        print(f"rejected {args.case_id}")


if __name__ == "__main__":
    main()
