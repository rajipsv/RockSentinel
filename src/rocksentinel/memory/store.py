"""Memory store (SQLite locally; Cosmos DB in Azure behind the same methods).

- cases:      one triaged failure per case key (run:attempt:job), idempotent upserts
- signatures: occurrence counts, first/last seen, components and GPU families
- instincts:  signature -> likely cause/resolution with a confidence that rises on
              confirmation, falls on contradiction and decays without new evidence
- feedback:   outcomes (Console verdicts, reruns, fix commits) for online evals
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rocksentinel.memory.signatures import make_signature, normalize, similarity

INITIAL_CONFIDENCE = 0.5
CONFIRM_STEP = 0.2          # c <- c + 0.2 * (1 - c)
CONTRADICT_FACTOR = 0.7     # c <- c * 0.7
WEEKLY_DECAY = 0.98         # c <- c * 0.98 per week without new evidence
ARCHIVE_BELOW = 0.3
TRUST_AT = 0.8              # a known signature at this confidence can skip the strong model

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mem_cases (
    case_key TEXT PRIMARY KEY, signature TEXT NOT NULL, component TEXT, gpu_family TEXT,
    platform TEXT, category TEXT, created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_mem_cases_sig ON mem_cases (signature);
CREATE TABLE IF NOT EXISTS mem_signatures (
    signature TEXT PRIMARY KEY, normalized TEXT NOT NULL, example TEXT NOT NULL,
    component TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    count INTEGER NOT NULL, families TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mem_instincts (
    signature TEXT PRIMARY KEY, category TEXT, cause TEXT, resolution TEXT,
    confidence REAL NOT NULL, updated_at TEXT NOT NULL, history TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mem_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_key TEXT NOT NULL, source TEXT NOT NULL,
    verdict TEXT NOT NULL, comment TEXT, at TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: MemoryStore, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def effective_confidence(confidence: float, updated_at: str, now: datetime | None = None) -> float:
    weeks = max(0.0, ((now or datetime.now(UTC)) - _parse(updated_at)).days / 7)
    return round(confidence * (WEEKLY_DECAY ** int(weeks)), 3)


class MemoryStore:
    def __init__(self, path: str | Path = ".data/rocksentinel.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._db.executescript(_SCHEMA)

    @_locked
    def close(self) -> None:
        self._db.close()

    # --- cases + signatures ---------------------------------------------------------------

    @_locked
    def upsert_case(self, case: dict) -> dict:
        """Store a triaged case. Signature counts change only the first time a key is seen."""
        key, first_error = case["case_key"], case["first_error"]
        sig = case.get("signature") or make_signature(first_error, case.get("component"))
        now = case.get("at") or _now()
        with self._db:
            new = self._db.execute(
                "INSERT OR IGNORE INTO mem_cases VALUES (?,?,?,?,?,?,?,?)",
                (key, sig, case.get("component"), case.get("gpu_family"), case.get("platform"),
                 case.get("category"), now, json.dumps(case)),
            ).rowcount == 1
            if not new:
                self._db.execute(
                    "UPDATE mem_cases SET category = ?, body = ? WHERE case_key = ?",
                    (case.get("category"), json.dumps(case), key))
            else:
                row = self._db.execute(
                    "SELECT families FROM mem_signatures WHERE signature = ?", (sig,)).fetchone()
                families = set(json.loads(row["families"])) if row else set()
                if case.get("gpu_family"):
                    families.add(case["gpu_family"])
                if row:
                    self._db.execute(
                        "UPDATE mem_signatures SET last_seen = ?, count = count + 1, "
                        "families = ? WHERE signature = ?",
                        (now, json.dumps(sorted(families)), sig))
                else:
                    self._db.execute(
                        "INSERT INTO mem_signatures VALUES (?,?,?,?,?,?,?,?)",
                        (sig, normalize(first_error), first_error, case.get("component"),
                         now, now, 1, json.dumps(sorted(families))))
        return {"case_key": key, "signature": sig, "new": new}

    @_locked
    def lookup_signature(self, signature: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM mem_signatures WHERE signature = ?", (signature,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "families": json.loads(row["families"]),
                "instinct": self._instinct(signature)}

    @_locked
    def list_signatures(self, limit: int = 100) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM mem_signatures ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "families": json.loads(r["families"]),
                 "instinct": self._instinct(r["signature"])} for r in rows]

    @_locked
    def search_similar(self, text: str, k: int = 3, component: str | None = None,
                       min_score: float = 0.3) -> list[dict]:
        """v1: exact signature first, then token similarity over known signatures."""
        exact = make_signature(text, component)
        scored = []
        for row in self._db.execute("SELECT * FROM mem_signatures").fetchall():
            if component and row["component"] and row["component"].lower() != component.lower():
                continue
            score = 1.0 if row["signature"] == exact else similarity(text, row["example"])
            if score >= min_score:
                scored.append((score, row))
        scored.sort(key=lambda x: (-x[0], -x[1]["count"]))
        return [{"signature": r["signature"], "score": round(s, 3), "example": r["example"],
                 "component": r["component"], "count": r["count"],
                 "families": json.loads(r["families"]), "instinct": self._instinct(r["signature"])}
                for s, r in scored[:k]]

    # --- instincts ------------------------------------------------------------------------

    def _instinct(self, signature: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM mem_instincts WHERE signature = ?", (signature,)).fetchone()
        if row is None:
            return None
        eff = effective_confidence(row["confidence"], row["updated_at"])
        return {"category": row["category"], "cause": row["cause"],
                "resolution": row["resolution"], "confidence": eff,
                "trusted": eff >= TRUST_AT, "archived": eff < ARCHIVE_BELOW,
                "updated_at": row["updated_at"], "history": json.loads(row["history"])}

    @_locked
    def update_instinct(self, signature: str, outcome: str, *, category: str | None = None,
                        cause: str | None = None, resolution: str | None = None,
                        evidence: str | None = None) -> dict:
        """outcome: "confirm" (raises confidence) or "contradict" (lowers it)."""
        if outcome not in ("confirm", "contradict"):
            raise ValueError("outcome must be 'confirm' or 'contradict'")
        row = self._db.execute(
            "SELECT * FROM mem_instincts WHERE signature = ?", (signature,)).fetchone()
        now = _now()
        if row is None:
            c = INITIAL_CONFIDENCE
            if outcome == "contradict":
                c *= CONTRADICT_FACTOR
            history: list = []
        else:
            c = effective_confidence(row["confidence"], row["updated_at"])  # apply decay first
            c = c + CONFIRM_STEP * (1 - c) if outcome == "confirm" else c * CONTRADICT_FACTOR
            history = json.loads(row["history"])
            category = category or row["category"]
            cause = cause or row["cause"]
            resolution = resolution or row["resolution"]
        history.append({"at": now, "outcome": outcome, "evidence": evidence,
                        "confidence": round(c, 3)})
        with self._db:
            self._db.execute(
                "INSERT INTO mem_instincts VALUES (?,?,?,?,?,?,?) ON CONFLICT(signature) DO "
                "UPDATE SET category=excluded.category, cause=excluded.cause, "
                "resolution=excluded.resolution, confidence=excluded.confidence, "
                "updated_at=excluded.updated_at, history=excluded.history",
                (signature, category, cause, resolution, round(c, 3), now, json.dumps(history)))
        return self._instinct(signature)

    # --- feedback + aggregates ------------------------------------------------------------

    @_locked
    def record_outcome(self, case_key: str, source: str, verdict: str,
                       comment: str | None = None) -> dict:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO mem_feedback (case_key, source, verdict, comment, at) "
                "VALUES (?,?,?,?,?)", (case_key, source, verdict, comment, _now()))
        return {"id": cur.lastrowid, "case_key": case_key, "verdict": verdict}

    @_locked
    def aggregate_by_family(self, since: str | None = None) -> list[dict]:
        rows = self._db.execute(
            "SELECT gpu_family, category, COUNT(*) AS n FROM mem_cases "
            "WHERE (? IS NULL OR created_at >= ?) GROUP BY 1, 2 ORDER BY n DESC",
            (since, since)).fetchall()
        return [dict(r) for r in rows]
