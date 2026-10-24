"""Triage worker: deterministic triage of failed jobs into `triage_results`.

    python -m rocksentinel.triage.worker                 # offline: jobs with a saved log excerpt
    python -m rocksentinel.triage.worker --job 123 --fetch   # one job, downloads its log

Offline by default: it uses log excerpts already saved under tests/fixtures/logs/ and never
touches the network. Fetching is explicit and one job at a time.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from rocksentinel.config import ROOT
from rocksentinel.triage import triage_log

FIXTURES = ROOT / "tests" / "fixtures" / "logs"
_FIXTURE_JOB = re.compile(r"_job-(\d+)\.txt$")


def fixture_logs(directory: Path = FIXTURES) -> dict[int, Path]:
    """Saved log excerpts by job id (file names end with _job-<id>.txt)."""
    out: dict[int, Path] = {}
    for path in sorted(directory.glob("*.txt")):
        if m := _FIXTURE_JOB.search(path.name):
            out[int(m.group(1))] = path
    return out


def triage_from_fixtures(store, directory: Path = FIXTURES) -> dict:
    """Triage every failed job in the store that has a saved excerpt. No network."""
    done, skipped = 0, 0
    for job_id, path in fixture_logs(directory).items():
        job = store.get_job(job_id)
        if job is None or not job.failed:
            skipped += 1
            continue
        store.save_triage(triage_log(job, path.read_text(encoding="utf-8"), store,
                                     source="saved excerpt"))
        store.mark_queue(job.case_key, "done")
        done += 1
    return {"triaged": done, "skipped": skipped}


def triage_one_live(store, gh, job_id: int) -> dict:
    """Download ONE job's log and triage it (explicit, user-triggered)."""
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(f"job {job_id} not in the job store")
    if not job.failed:
        raise ValueError(f"job {job_id} did not fail")
    row = triage_log(job, gh.get_job_log(job.repo, job.job_id), store, source="fetched log")
    store.save_triage(row)
    store.mark_queue(job.case_key, "done")
    return row


def main() -> None:
    from rocksentinel.store.sqlite_store import SQLiteStore

    parser = argparse.ArgumentParser(description="Deterministic triage into triage_results.")
    parser.add_argument("--db", default=".data/rocksentinel.db")
    parser.add_argument("--job", type=int, help="with --fetch: the one job to download")
    parser.add_argument("--fetch", action="store_true",
                        help="download the log of --job (network); default is offline")
    args = parser.parse_args()
    store = SQLiteStore(args.db)
    try:
        if args.fetch:
            if not args.job:
                parser.error("--fetch needs --job")
            from rocksentinel.github.client import GitHubClient

            with GitHubClient() as gh:
                row = triage_one_live(store, gh, args.job)
            print(f"job {args.job}: {row['category']} - {row['headline']}")
        else:
            print(triage_from_fixtures(store))
    finally:
        store.close()


if __name__ == "__main__":
    main()
