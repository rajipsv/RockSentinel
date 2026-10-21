"""Pattern coverage on failures that are NOT in the golden set.

    python -m rocksentinel.evals.coverage --sample 30

For a diverse sample of failed jobs from the job store (excluding golden cases and
pending proposals, so this is not the eval set), download each log and check what
extract_errors finds:
  specific      at least one specific pattern (e.g. GPU fault, ctest, DejaGnu FAIL)
  generic_only  only the safety net ("error"/"failed"/...) -> a format we don't know yet
  none          nothing but the exit code (or nothing at all)
Jobs that aren't "specific" are a to-do list for new patterns.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from rocksentinel.evals.labeler import Paths, _diverse, _read_jsonl
from rocksentinel.github.client import GitHubClient, GitHubError
from rocksentinel.store.base import JobStore
from rocksentinel.therock_ci.extract import extract_errors

NON_SPECIFIC = frozenset({"generic_error", "exit_code"})


def classify_coverage(log: str) -> tuple[str, str | None]:
    candidates = extract_errors(log).candidates
    specific = [c for c in candidates if c.kind not in NON_SPECIFIC]
    if specific:
        return "specific", specific[0].kind
    if any(c.kind == "generic_error" for c in candidates):
        return "generic_only", "generic_error"
    return "none", None


def coverage(store: JobStore, gh: GitHubClient, sample: int = 30,
             paths: Paths | None = None) -> dict:
    paths = paths or Paths()
    known = {c.get("job_id") for c in _read_jsonl(paths.dataset) + _read_jsonl(paths.proposals)}
    jobs = _diverse(store.failed_jobs(limit=2000), {j for j in known if j}, limit=2000)
    rows, skipped = [], 0
    for job in jobs:
        if len(rows) >= sample:
            break
        try:
            status, kind = classify_coverage(gh.get_job_log(job.repo, job.job_id))
        except GitHubError:
            skipped += 1  # e.g. 404: failed before any step ran
            continue
        rows.append({
            "job_id": job.job_id, "workflow": job.workflow, "job_name": job.job_name,
            "status": status, "top_kind": kind,
            "job_url": f"https://github.com/{job.repo}/actions/runs/{job.run_id}/job/{job.job_id}",
        })
    counts = Counter(r["status"] for r in rows)
    n = len(rows)
    return {
        "sampled": n,
        "skipped_no_log": skipped,
        "specific_rate": round(counts["specific"] / n, 3) if n else None,
        "counts": dict(counts),
        "top_kinds": dict(Counter(r["top_kind"] for r in rows if r["status"] == "specific")),
        "todo": [r for r in rows if r["status"] != "specific"],
        "rows": rows,
    }


def main() -> None:
    from rocksentinel.store.sqlite_store import SQLiteStore

    parser = argparse.ArgumentParser(description="Pattern coverage on non-golden failures.")
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--db", default=".data/rocksentinel.db")
    parser.add_argument("--out", type=Path, default=Path("evals/reports/coverage.json"))
    args = parser.parse_args()

    store = SQLiteStore(args.db)
    with GitHubClient() as gh:
        report = coverage(store, gh, args.sample)
    store.close()
    print(f"sampled {report['sampled']} non-golden failed jobs "
          f"(skipped {report['skipped_no_log']} without logs)")
    print(f"specific pattern found: {report['specific_rate']}  counts: {report['counts']}")
    print(f"top kinds: {report['top_kinds']}")
    for r in report["todo"]:
        print(f"  TODO [{r['status']}] {r['job_name'][:80]} ({r['workflow']})")
        print(f"       {r['job_url']}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report written to {args.out}")


if __name__ == "__main__":
    main()
