"""Local SQLite adapter implementing JobStore, TriageQueue and StateStore in one file."""

from __future__ import annotations

import sqlite3
from dataclasses import astuple, fields
from datetime import UTC, datetime
from pathlib import Path

from rocksentinel.store.base import JobResult

_COLUMNS = [f.name for f in fields(JobResult)]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS job_results (
    {", ".join(_COLUMNS)},
    PRIMARY KEY (job_id)
);
CREATE INDEX IF NOT EXISTS ix_job_results_run ON job_results (run_id, attempt);
CREATE INDEX IF NOT EXISTS ix_job_results_pattern ON job_results (workflow, component, gpu_family);

CREATE TABLE IF NOT EXISTS triage_queue (
    case_key   TEXT PRIMARY KEY,
    job_id     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    queued_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = ".data/rocksentinel.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- JobStore -----------------------------------------------------------------------

    def upsert_jobs(self, jobs: list[JobResult]) -> int:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._db:
            self._db.executemany(
                f"INSERT OR REPLACE INTO job_results ({', '.join(_COLUMNS)}) "
                f"VALUES ({placeholders})",
                [astuple(j) for j in jobs],
            )
        return len(jobs)

    def get_job(self, job_id: int) -> JobResult | None:
        row = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM job_results WHERE job_id = ?", (job_id,)
        ).fetchone()
        return JobResult(*row) if row else None

    def jobs_for_run(self, run_id: int) -> list[JobResult]:
        rows = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM job_results WHERE run_id = ? ORDER BY job_id",
            (run_id,),
        ).fetchall()
        return [JobResult(*r) for r in rows]

    def count_jobs(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM job_results").fetchone()[0]

    # --- TriageQueue --------------------------------------------------------------------

    def enqueue(self, job: JobResult) -> bool:
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO triage_queue (case_key, job_id, queued_at) VALUES (?, ?, ?)",
                (job.case_key, job.job_id, datetime.now(UTC).isoformat()),
            )
        return cur.rowcount == 1

    def pending(self, limit: int = 100) -> list[str]:
        rows = self._db.execute(
            "SELECT case_key FROM triage_queue WHERE status = 'pending' "
            "ORDER BY queued_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    # --- StateStore ---------------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
