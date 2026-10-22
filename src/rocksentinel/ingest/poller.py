"""Poll completed GitHub Actions runs, store every job result, queue failures for triage.

Polling works for any repo the token (or GitHub App installation) can read, including
repos whose owners don't send us webhooks. Each poll re-reads a lookback window before
the saved cursor, because a run created earlier may finish later; writes are idempotent
(job_id upsert, case_key queue), so overlap is safe.

Run locally:
    python -m rocksentinel.ingest.poller                 # every enabled repo in config/repos.toml
    python -m rocksentinel.ingest.poller --repo ROCm/TheRock --since-hours 24 --max-runs 20
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from rocksentinel.github.client import GitHubClient, GitHubError, JobRef, RunRef
from rocksentinel.packs import Profile, default_profile
from rocksentinel.store.base import JobResult, JobStore, StateStore, TriageQueue

DEFAULT_REPO = "ROCm/TheRock"
LOOKBACK = timedelta(hours=6)
_CURSOR_KEY = "poller:{repo}:last_created_at"


@dataclass(frozen=True)
class PollResult:
    runs_seen: int
    jobs_written: int
    failures_queued: int
    cursor: str | None
    skipped_runs: tuple[int, ...] = ()


def _parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def cursor_key(repo: str, workflow: str | None = None, branch: str | None = "main") -> str:
    """One cursor per (repo, workflow filter, branch filter), so polls never skip each other's
    runs. The plain key is the repo's main branch, all workflows."""
    key = _CURSOR_KEY.format(repo=repo) + (f":{workflow}" if workflow else "")
    if branch != "main":
        key += f":branch={branch or '*'}"
    return key


def to_job_result(repo: str, run: RunRef, job: JobRef, profile: Profile | None = None) -> JobResult:
    info = (profile or default_profile()).map_job(run.workflow, job.name, job.runner_label)
    started, finished = _parse_ts(job.started_at), _parse_ts(job.completed_at)
    duration = int((finished - started).total_seconds()) if started and finished else None
    return JobResult(
        run_id=run.run_id,
        attempt=job.attempt,
        job_id=job.job_id,
        repo=repo,
        workflow=run.workflow,
        job_name=job.name,
        component=info.component,
        gpu_family=info.gpu_family,
        platform=info.platform,
        shard=info.shard,
        runner_label=job.runner_label,
        conclusion=job.conclusion,
        started_at=job.started_at,
        finished_at=job.completed_at,
        duration_s=duration,
        head_sha=run.head_sha,
        branch=run.branch,
        event=run.event,
    )


def poll_once(
    gh: GitHubClient,
    jobs: JobStore,
    queue: TriageQueue,
    state: StateStore,
    *,
    repo: str = DEFAULT_REPO,
    since_hours: int = 24,
    branch: str | None = "main",
    max_runs: int = 50,
    backfill: bool = False,
    workflow: str | None = None,
    profile: Profile | None = None,
) -> PollResult:
    """Poll once. `backfill` ignores the saved cursor and reads the last `since_hours`.

    `workflow` (a file name such as "multi_arch_release.yml") polls just that workflow,
    with its own cursor, so it never moves the all-workflows cursor past unread runs.
    """
    key = cursor_key(repo, workflow, branch)
    cursor = _parse_ts(state.get_state(key))
    if cursor and not backfill:
        since = cursor - LOOKBACK
    else:
        since = datetime.now(UTC) - timedelta(hours=since_hours)

    runs = gh.list_runs(repo, since=since, status="completed", branch=branch,
                        workflow=workflow, limit=max_runs)
    written = queued = 0
    newest = cursor
    skipped: list[RunRef] = []
    for run in runs:
        try:
            # all_attempts: reruns get new job ids; keeping earlier attempts keeps flake evidence
            run_jobs = gh.list_jobs(repo, run.run_id, failed_only=False, all_attempts=True)
        except GitHubError as exc:
            # One flaky GitHub response (e.g. 502 after retries) must not stop ingestion.
            print(f"skipped run {run.run_id}: {exc}")
            skipped.append(run)
            continue
        results = [to_job_result(repo, run, j, profile) for j in run_jobs]
        written += jobs.upsert_jobs(results)
        queued += sum(queue.enqueue(r) for r in results if r.failed)
        created = _parse_ts(run.created_at)
        if created and (newest is None or created > newest):
            newest = created

    # Never move the cursor past a skipped run, so the next poll retries it.
    for run in skipped:
        created = _parse_ts(run.created_at)
        if created and newest and created <= newest:
            newest = created - timedelta(seconds=1)

    new_cursor = newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None
    if new_cursor:
        state.set_state(key, new_cursor)
    return PollResult(
        len(runs), written, queued, new_cursor, tuple(r.run_id for r in skipped)
    )


def poll_repo(gh: GitHubClient, store, cfg, *, since_hours: int = 24, max_runs: int = 20,
              backfill: bool = False, config=None) -> list[tuple[str, PollResult]]:
    """Poll one configured repo: every (branch, workflow) filter it asks for."""
    from rocksentinel.config import profile_for

    prof = profile_for(cfg.repo, config)
    out = []
    for branch in cfg.branch_filters():
        for workflow in cfg.workflow_filters():
            result = poll_once(gh, store, store, store, repo=cfg.repo, since_hours=since_hours,
                               branch=branch, max_runs=max_runs, backfill=backfill,
                               workflow=workflow, profile=prof)
            label = f"{cfg.repo} [{workflow or 'all workflows'} @ {branch or 'all branches'}]"
            out.append((label, result))
    return out


def main() -> None:
    from rocksentinel.config import load_config, profile_for
    from rocksentinel.store.sqlite_store import SQLiteStore

    parser = argparse.ArgumentParser(description="Poll GitHub Actions CI into the job store.")
    parser.add_argument("--repo", default=None,
                        help="one repo (default: every enabled repo in config/repos.toml)")
    parser.add_argument("--since-hours", type=int, default=24)
    parser.add_argument("--branch", default="main", help='with --repo; "" = all branches')
    parser.add_argument("--max-runs", type=int, default=20)
    parser.add_argument("--db", default=".data/rocksentinel.db")
    parser.add_argument("--backfill", action="store_true",
                        help="ignore the saved cursor and read the whole --since-hours window")
    parser.add_argument("--workflow", default=None,
                        help="with --repo: one workflow file only, e.g. multi_arch_release.yml")
    args = parser.parse_args()

    config = load_config()
    store = SQLiteStore(args.db)
    with GitHubClient(api_url=config.settings.github_api_url) as gh:
        if args.repo:
            results = [(args.repo, poll_once(
                gh, store, store, store, repo=args.repo, since_hours=args.since_hours,
                branch=args.branch or None, max_runs=args.max_runs, backfill=args.backfill,
                workflow=args.workflow, profile=profile_for(args.repo, config)))]
        else:
            results = []
            for cfg in config.enabled():
                try:  # one repo's outage must not stop the others
                    results += poll_repo(gh, store, cfg, since_hours=args.since_hours,
                                         max_runs=args.max_runs, backfill=args.backfill,
                                         config=config)
                except GitHubError as exc:
                    print(f"{cfg.repo}: skipped this poll ({exc})")
    for label, r in results:
        print(f"{label}: runs={r.runs_seen} jobs_written={r.jobs_written} "
              f"failures_queued={r.failures_queued} cursor={r.cursor} "
              f"skipped_runs={len(r.skipped_runs)}")
    print(f"total_jobs_in_store={store.count_jobs()}")
    store.close()


if __name__ == "__main__":
    main()
