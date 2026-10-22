"""Job history analysis: failing-since, rerun/flake evidence, same-run co-failures.

Deterministic (no LLM). Reads the job store filled by the poller. Answers the questions
a CI engineer asks first: "since when?", "is it flaky?", "is it just this job, or is the
whole runner pool / run broken?".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rocksentinel.store.base import FAILED_CONCLUSIONS, JobResult, JobStore

# This many distinct components failing on the same runner label in one run attempt
# suggests a platform/infra event rather than one component's bug (e.g. run #3351).
PLATFORM_EVENT_MIN_COMPONENTS = 3


@dataclass(frozen=True)
class _RunOutcome:
    run_id: int
    head_sha: str
    final: JobResult  # the highest attempt of this job in the run
    first_failed: JobResult | None


def _runs_newest_first(history: list[JobResult]) -> list[_RunOutcome]:
    by_run: dict[int, list[JobResult]] = {}
    order: list[int] = []
    for job in history:  # history is newest first
        if job.run_id not in by_run:
            order.append(job.run_id)
        by_run.setdefault(job.run_id, []).append(job)
    outcomes = []
    for run_id in order:
        attempts = sorted(by_run[run_id], key=lambda j: j.attempt)
        failed = [j for j in attempts if j.failed]
        outcomes.append(
            _RunOutcome(run_id, attempts[-1].head_sha, attempts[-1], failed[0] if failed else None)
        )
    return outcomes


def _brief(job: JobResult) -> dict:
    return {
        "run_id": job.run_id,
        "attempt": job.attempt,
        "job_id": job.job_id,
        "conclusion": job.conclusion,
        "finished_at": job.finished_at,
        "head_sha": job.head_sha,
    }


def job_history(store: JobStore, job_id: int, limit: int = 50) -> dict:
    job = store.get_job(job_id)
    if job is None:
        raise LookupError(f"job {job_id} is not in the job store; run the poller first")

    history = store.history(job.repo, job.workflow, job.job_name, limit=limit)
    runs = _runs_newest_first(history)

    # --- failing since: consecutive runs (newest first) whose final attempt failed --------
    streak: list[_RunOutcome] = []
    last_success: JobResult | None = None
    for outcome in runs:
        if outcome.final.failed:
            streak.append(outcome)
        elif outcome.final.conclusion == "success":
            last_success = outcome.final
            break
    failing_since = None
    if streak:
        oldest = streak[-1]
        first = oldest.first_failed or oldest.final
        failing_since = {**_brief(first), "streak_runs": len(streak)}

    # --- rerun / flake evidence ---------------------------------------------------------
    same_run = sorted((j for j in history if j.run_id == job.run_id), key=lambda j: j.attempt)
    passed_on_rerun = (
        any(j.failed for j in same_run)
        and same_run[-1].conclusion == "success"
        if len(same_run) > 1
        else None
    )
    by_sha: dict[str, set[str]] = {}
    for j in history:
        if j.conclusion:
            by_sha.setdefault(j.head_sha, set()).add("fail" if j.failed else j.conclusion)
    mixed_shas = sorted(sha for sha, seen in by_sha.items() if {"fail", "success"} <= seen)
    finals = [o.final for o in runs if o.final.conclusion in FAILED_CONCLUSIONS | {"success"}]
    failure_rate = (
        round(sum(j.failed for j in finals) / len(finals), 3) if finals else None
    )

    # --- same-run co-failures (same run attempt) ----------------------------------------
    same_attempt = [j for j in store.jobs_for_run(job.run_id) if j.attempt == job.attempt]
    co_failed = [j for j in same_attempt if j.failed and j.job_id != job.job_id]
    by_runner = Counter(j.runner_label for j in co_failed if j.runner_label)
    same_runner_components = {
        j.component for j in co_failed if j.runner_label and j.runner_label == job.runner_label
    } | ({job.component} if job.failed else set())
    same_runner_components.discard(None)

    return {
        "job": {
            "job_id": job.job_id,
            "run_id": job.run_id,
            "attempt": job.attempt,
            "workflow": job.workflow,
            "job_name": job.job_name,
            "component": job.component,
            "gpu_family": job.gpu_family,
            "platform": job.platform,
            "runner_label": job.runner_label,
            "conclusion": job.conclusion,
        },
        "failing_since": failing_since,
        "last_success": _brief(last_success) if last_success else None,
        "recent": [_brief(o.final) for o in runs[:10]],
        "runs_considered": len(runs),
        "failure_rate": failure_rate,
        "flake_evidence": {
            # About this failure: did a rerun of the same run (same commit) pass?
            "passed_on_rerun_same_run": passed_on_rerun,
            # About this failure: did the same commit pass in another run?
            "same_sha_passed_elsewhere": job.head_sha in mixed_shas,
            # Context only: other commits with mixed results, and past rerun passes.
            "history_mixed_shas": [s for s in mixed_shas if s != job.head_sha],
            "history_rerun_passes": sum(
                1 for o in runs if o.first_failed and o.final.conclusion == "success"
            ),
            "likely_flaky": bool(passed_on_rerun) or job.head_sha in mixed_shas,
        },
        "same_run_co_failures": {
            "count": len(co_failed),
            "jobs_in_attempt": len(same_attempt),
            "components": sorted({j.component for j in co_failed if j.component}),
            "by_runner_label": dict(by_runner),
            "platform_event_hint": len(same_runner_components) >= PLATFORM_EVENT_MIN_COMPONENTS,
        },
    }
