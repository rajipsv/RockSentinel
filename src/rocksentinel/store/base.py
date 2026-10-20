"""Storage interfaces (ports). Adapters: SQLite locally; ADX / Cosmos / Service Bus in Azure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

FAILED_CONCLUSIONS = frozenset({"failure", "timed_out"})


@dataclass(frozen=True)
class JobResult:
    """One job attempt, passes included. Mirrors the ADX `job_results` table (design §2.6)."""

    run_id: int
    attempt: int
    job_id: int
    repo: str
    workflow: str
    job_name: str
    component: str | None
    gpu_family: str | None
    platform: str | None
    shard: str | None
    runner_label: str | None
    conclusion: str | None
    started_at: str | None
    finished_at: str | None
    duration_s: int | None
    head_sha: str
    branch: str | None
    event: str

    @property
    def failed(self) -> bool:
        return self.conclusion in FAILED_CONCLUSIONS

    @property
    def case_key(self) -> str:
        """Idempotency key for triage: one case per job attempt."""
        return f"{self.run_id}:{self.attempt}:{self.job_id}"


class JobStore(Protocol):
    def upsert_jobs(self, jobs: list[JobResult]) -> int: ...
    def get_job(self, job_id: int) -> JobResult | None: ...
    def jobs_for_run(self, run_id: int) -> list[JobResult]: ...
    def count_jobs(self) -> int: ...


class TriageQueue(Protocol):
    def enqueue(self, job: JobResult) -> bool:
        """Queue a failed job for triage. Returns False if already queued (idempotent)."""
        ...

    def pending(self, limit: int = 100) -> list[str]: ...


class StateStore(Protocol):
    def get_state(self, key: str) -> str | None: ...
    def set_state(self, key: str, value: str) -> None: ...
