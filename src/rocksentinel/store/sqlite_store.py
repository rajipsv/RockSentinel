"""Local SQLite adapter implementing JobStore, TriageQueue and StateStore in one file."""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import astuple, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rocksentinel.store.base import JobResult


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize access: the MCP server calls tools from worker threads."""

    @functools.wraps(method)
    def wrapper(self: SQLiteStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper

_COLUMNS = [f.name for f in fields(JobResult)]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS job_results (
    {", ".join(_COLUMNS)},
    PRIMARY KEY (job_id)
);
CREATE INDEX IF NOT EXISTS ix_job_results_run ON job_results (run_id, attempt);
CREATE INDEX IF NOT EXISTS ix_job_results_pattern ON job_results (workflow, component, gpu_family);
CREATE INDEX IF NOT EXISTS ix_job_results_identity ON job_results (repo, workflow, job_name);

CREATE TABLE IF NOT EXISTS triage_queue (
    case_key   TEXT PRIMARY KEY,
    job_id     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    queued_at  TEXT NOT NULL
);

-- Deterministic triage of one failed job (root cause, verdict, evidence windows as JSON).
CREATE TABLE IF NOT EXISTS triage_results (
    job_id       INTEGER PRIMARY KEY,
    repo         TEXT NOT NULL,
    signature    TEXT,
    root_kind    TEXT,
    root_line    TEXT,
    headline     TEXT NOT NULL,
    category     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    failing_test TEXT,
    faulting_kernel TEXT,
    ignored_noise INTEGER NOT NULL DEFAULT 0,
    evidence     TEXT NOT NULL,
    source       TEXT NOT NULL,
    triaged_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_triage_signature ON triage_results (signature);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = ".data/rocksentinel.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across threads; every method holds self._lock.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.executescript(_SCHEMA)

    @_locked
    def close(self) -> None:
        self._db.close()

    # --- JobStore -----------------------------------------------------------------------

    @_locked
    def upsert_jobs(self, jobs: list[JobResult]) -> int:
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._db:
            self._db.executemany(
                f"INSERT OR REPLACE INTO job_results ({', '.join(_COLUMNS)}) "
                f"VALUES ({placeholders})",
                [astuple(j) for j in jobs],
            )
        return len(jobs)

    @_locked
    def get_job(self, job_id: int) -> JobResult | None:
        row = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM job_results WHERE job_id = ?", (job_id,)
        ).fetchone()
        return JobResult(*row) if row else None

    @_locked
    def jobs_for_run(self, run_id: int) -> list[JobResult]:
        rows = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM job_results WHERE run_id = ? ORDER BY job_id",
            (run_id,),
        ).fetchall()
        return [JobResult(*r) for r in rows]

    @_locked
    def count_jobs(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM job_results").fetchone()[0]

    @_locked
    def history(self, repo: str, workflow: str, job_name: str, limit: int = 50) -> list[JobResult]:
        rows = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM job_results "
            "WHERE repo = ? AND workflow = ? AND job_name = ? "
            "ORDER BY COALESCE(finished_at, started_at) DESC, attempt DESC LIMIT ?",
            (repo, workflow, job_name, limit),
        ).fetchall()
        return [JobResult(*r) for r in rows]

    # --- TriageQueue --------------------------------------------------------------------

    # Date filters are inclusive UTC days ("YYYY-MM-DD") on the run's start (first job start).
    _DAY_FILTER = ("HAVING (? IS NULL OR substr(MIN(started_at), 1, 10) >= ?) "
                   "AND (? IS NULL OR substr(MIN(started_at), 1, 10) <= ?) ")

    @_locked
    def recent_runs(self, limit: int = 50, workflow: str | None = None,
                    since: str | None = None, until: str | None = None,
                    repo: str | None = None) -> list[dict]:
        """Runs newest first, with job and failure counts (optionally one repo/workflow/days)."""
        rows = self._db.execute(
            "SELECT run_id, repo, workflow, head_sha, branch, MIN(started_at), MAX(finished_at), "
            "COUNT(*), SUM(conclusion IN ('failure','timed_out')), MAX(attempt) "
            "FROM job_results WHERE (? IS NULL OR workflow = ?) AND (? IS NULL OR repo = ?) "
            "GROUP BY run_id " + self._DAY_FILTER + "ORDER BY MIN(started_at) DESC LIMIT ?",
            (workflow, workflow, repo, repo, since, since, until, until, limit),
        ).fetchall()
        keys = ("run_id", "repo", "workflow", "head_sha", "branch", "started_at", "finished_at",
                "jobs", "failed", "attempts")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    @_locked
    def run_date_range(self) -> tuple[str | None, str | None]:
        """First and last UTC day with any run, for the date picker bounds."""
        lo, hi = self._db.execute(
            "SELECT substr(MIN(started_at), 1, 10), substr(MAX(started_at), 1, 10) "
            "FROM job_results").fetchone()
        return lo, hi

    @_locked
    def workflow_summaries(self, recent: int = 20, since: str | None = None,
                           until: str | None = None, repo: str | None = None) -> list[dict]:
        """Per (repo, workflow): run and failure counts, last run, and recent run outcomes."""
        per_run = self._db.execute(
            "SELECT repo, workflow, run_id, MIN(started_at) AS started, "
            "SUM(conclusion IN ('failure','timed_out')) AS failed, COUNT(*) AS jobs "
            "FROM job_results WHERE (? IS NULL OR repo = ?) "
            "GROUP BY run_id " + self._DAY_FILTER + "ORDER BY started DESC",
            (repo, repo, since, since, until, until)).fetchall()
        out: dict[tuple[str, str], dict] = {}
        for rp, wf, run_id, started, failed, jobs in per_run:
            w = out.setdefault((rp, wf), {"repo": rp, "workflow": wf, "runs": 0,
                                          "failed_runs": 0, "jobs": 0, "failed_jobs": 0,
                                          "last_run_at": started, "recent": []})
            w["runs"] += 1
            w["failed_runs"] += failed > 0
            w["jobs"] += jobs
            w["failed_jobs"] += failed
            if len(w["recent"]) < recent:
                w["recent"].append({"run_id": run_id, "failed": failed, "jobs": jobs,
                                    "started_at": started})
        for w in out.values():
            w["failure_rate"] = round(w["failed_runs"] / w["runs"], 3) if w["runs"] else 0.0
        return sorted(out.values(), key=lambda w: (-w["failed_runs"], w["repo"], w["workflow"]))

    @_locked
    def failed_jobs(self, limit: int = 200, repo: str | None = None) -> list[JobResult]:
        rows = self._db.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM job_results "
            "WHERE conclusion IN ('failure', 'timed_out') AND (? IS NULL OR repo = ?) "
            "ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?",
            (repo, repo, limit),
        ).fetchall()
        return [JobResult(*r) for r in rows]

    @_locked
    def enqueue(self, job: JobResult) -> bool:
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO triage_queue (case_key, job_id, queued_at) VALUES (?, ?, ?)",
                (job.case_key, job.job_id, datetime.now(UTC).isoformat()),
            )
        return cur.rowcount == 1

    @_locked
    def pending(self, limit: int = 100) -> list[str]:
        rows = self._db.execute(
            "SELECT case_key FROM triage_queue WHERE status = 'pending' "
            "ORDER BY queued_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    @_locked
    def mark_queue(self, case_key: str, status: str) -> None:
        with self._db:
            self._db.execute("UPDATE triage_queue SET status = ? WHERE case_key = ?",
                             (status, case_key))

    # --- Triage results -----------------------------------------------------------------

    _TRIAGE_COLS = ("job_id", "repo", "signature", "root_kind", "root_line", "headline",
                    "category", "reason", "failing_test", "faulting_kernel", "ignored_noise",
                    "evidence", "source", "triaged_at")

    @_locked
    def save_triage(self, row: dict) -> None:
        values = dict(row, evidence=json.dumps(row["evidence"]))
        cols = self._TRIAGE_COLS
        with self._db:
            self._db.execute(
                f"INSERT OR REPLACE INTO triage_results ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})", [values[c] for c in cols])

    def _triage_row(self, r: tuple) -> dict:
        row = dict(zip(self._TRIAGE_COLS, r, strict=True))
        row["evidence"] = json.loads(row["evidence"])
        return row

    @_locked
    def get_triage(self, job_id: int) -> dict | None:
        r = self._db.execute(f"SELECT {', '.join(self._TRIAGE_COLS)} FROM triage_results "
                             "WHERE job_id = ?", (job_id,)).fetchone()
        return self._triage_row(r) if r else None

    @_locked
    def triaged_failures(self, since: str | None = None, until: str | None = None,
                         repo: str | None = None) -> list[dict]:
        """Triaged failed jobs joined with their job facts (newest first), for grouping."""
        rows = self._db.execute(
            "SELECT t.job_id, t.repo, t.signature, t.root_kind, t.root_line, t.headline, "
            "t.category, t.reason, t.failing_test, j.run_id, j.workflow, j.job_name, "
            "j.component, j.gpu_family, j.platform, j.started_at "
            "FROM triage_results t JOIN job_results j ON j.job_id = t.job_id "
            "WHERE (? IS NULL OR substr(j.started_at, 1, 10) >= ?) "
            "AND (? IS NULL OR substr(j.started_at, 1, 10) <= ?) "
            "AND (? IS NULL OR t.repo = ?) ORDER BY j.started_at DESC",
            (since, since, until, until, repo, repo)).fetchall()
        keys = ("job_id", "repo", "signature", "root_kind", "root_line", "headline", "category",
                "reason", "failing_test", "run_id", "workflow", "job_name", "component",
                "gpu_family", "platform", "started_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    @_locked
    def signature_first_seen(self) -> dict[str, str]:
        """Earliest job start per signature across all triaged jobs."""
        return dict(self._db.execute(
            "SELECT t.signature, MIN(j.started_at) FROM triage_results t "
            "JOIN job_results j ON j.job_id = t.job_id WHERE t.signature IS NOT NULL "
            "GROUP BY t.signature").fetchall())

    @_locked
    def failed_job_count(self, since: str | None = None, until: str | None = None,
                         repo: str | None = None) -> tuple[int, int]:
        """(failed jobs, of which triaged) in the range."""
        r = self._db.execute(
            "SELECT COUNT(*), SUM(t.job_id IS NOT NULL) FROM job_results j "
            "LEFT JOIN triage_results t ON t.job_id = j.job_id "
            "WHERE j.conclusion IN ('failure','timed_out') "
            "AND (? IS NULL OR substr(j.started_at, 1, 10) >= ?) "
            "AND (? IS NULL OR substr(j.started_at, 1, 10) <= ?) "
            "AND (? IS NULL OR j.repo = ?)",
            (since, since, until, until, repo, repo)).fetchone()
        return r[0], r[1] or 0

    # --- StateStore ---------------------------------------------------------------------

    @_locked
    def get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @_locked
    def set_state(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
