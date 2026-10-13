"""SQLite-backed event store. All SQL is parameterized; WAL mode; one connection per call."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hookdesk.models import Event, Status, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_due ON events (status, next_attempt_at);
"""


class EventStore:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> Event:
        return Event(
            id=r["id"],
            source=r["source"],
            payload=json.loads(r["payload"]),
            status=Status(r["status"]),
            attempts=r["attempts"],
            next_attempt_at=r["next_attempt_at"],
            last_error=r["last_error"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def insert_if_new(
        self, event_id: str, source: str, payload: dict[str, Any], now: float | None = None
    ) -> bool:
        """Insert a pending event. Returns False if the id was already seen (idempotency)."""
        t = time.time() if now is None else now
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO events "
                "(id, source, payload, status, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, source, dumps(payload), Status.PENDING.value, t, t, t),
            )
            return cur.rowcount == 1

    def claim_due(self, limit: int, now: float | None = None) -> list[Event]:
        """Atomically move due pending events to processing and bump their attempt count."""
        t = time.time() if now is None else now
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute(
                    "SELECT id FROM events WHERE status = ? AND next_attempt_at <= ? "
                    "ORDER BY next_attempt_at LIMIT ?",
                    (Status.PENDING.value, t, limit),
                ).fetchall()
                ids = [r["id"] for r in rows]
                for eid in ids:
                    c.execute(
                        "UPDATE events SET status = ?, attempts = attempts + 1, updated_at = ? "
                        "WHERE id = ?",
                        (Status.PROCESSING.value, t, eid),
                    )
                claimed = [
                    self._row(c.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone())
                    for eid in ids
                ]
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return claimed

    def mark_done(self, event_id: str, now: float | None = None) -> None:
        self._set(event_id, Status.DONE, None, None, now)

    def mark_retry(
        self, event_id: str, error: str, next_at: float, now: float | None = None
    ) -> None:
        self._set(event_id, Status.PENDING, error, next_at, now)

    def mark_dead(self, event_id: str, error: str, now: float | None = None) -> None:
        self._set(event_id, Status.DEAD, error, None, now)

    def _set(
        self,
        event_id: str,
        status: Status,
        error: str | None,
        next_at: float | None,
        now: float | None,
    ) -> None:
        t = time.time() if now is None else now
        with self._conn() as c:
            c.execute(
                "UPDATE events SET status = ?, last_error = COALESCE(?, last_error), "
                "next_attempt_at = COALESCE(?, next_attempt_at), updated_at = ? WHERE id = ?",
                (status.value, error, next_at, t, event_id),
            )

    def requeue_dead(self, event_id: str, now: float | None = None) -> bool:
        """Give a dead event a fresh set of attempts. Returns False if it was not dead."""
        t = time.time() if now is None else now
        with self._conn() as c:
            cur = c.execute(
                "UPDATE events SET status = ?, attempts = 0, next_attempt_at = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (Status.PENDING.value, t, t, event_id, Status.DEAD.value),
            )
            return cur.rowcount == 1

    def recover_stuck(self, now: float | None = None) -> int:
        """After a crash, events left in 'processing' go back to pending."""
        t = time.time() if now is None else now
        with self._conn() as c:
            cur = c.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE status = ?",
                (Status.PENDING.value, t, Status.PROCESSING.value),
            )
            return cur.rowcount

    def get(self, event_id: str) -> Event | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return None if r is None else self._row(r)

    def list(self, status: Status | None = None, limit: int = 50, offset: int = 0) -> list[Event]:
        sql = "SELECT * FROM events"
        args: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            args.append(status.value)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._conn() as c:
            return [self._row(r) for r in c.execute(sql, args).fetchall()]

    def stats(self) -> dict[str, int]:
        counts = dict.fromkeys((s.value for s in Status), 0)
        with self._conn() as c:
            for r in c.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status"):
                counts[r["status"]] = r["n"]
        return counts
