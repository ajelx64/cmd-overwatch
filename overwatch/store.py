"""SQLite persistence layer.

One WAL-mode database shared by the scheduled collector (primary writer) and
the dashboard server (reads everything; writes only events and approval
decisions). All write paths apply :mod:`overwatch.redact` so nothing
secret-shaped reaches disk.

Issue lifecycle::

    open -> drafted -> pending_approval -> executing -> resolved | failed
            drafted -> executing                  (ungated / auto path)
            pending_approval -> open | wontfix    (denied)
            failed -> drafted                     (redraft)
            resolved -> open                      (signal recurred)

``wontfix`` is terminal: a recurring signal bumps its count but never
reopens it — the operator said no.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from overwatch.redact import redact_text, redact_value

ISSUE_STATUSES = frozenset(
    {"open", "drafted", "pending_approval", "executing", "resolved", "failed", "wontfix"}
)

_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"drafted", "resolved"}),
    "drafted": frozenset({"pending_approval", "executing"}),
    "pending_approval": frozenset({"executing", "open", "wontfix"}),
    "executing": frozenset({"resolved", "failed"}),
    "failed": frozenset({"drafted"}),
    "resolved": frozenset({"open"}),
    "wontfix": frozenset(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    gate_category TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    created_at TEXT NOT NULL,
    body_md TEXT NOT NULL,
    gate_category TEXT NOT NULL,
    auto_eligible INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    solution_id INTEGER NOT NULL UNIQUE REFERENCES solutions(id),
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    decided_at TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
    decided_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aar_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS host_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value TEXT NOT NULL,
    healthy INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS log_purge_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    target TEXT NOT NULL,
    files_deleted INTEGER NOT NULL,
    bytes_freed INTEGER NOT NULL,
    dry_run INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TransitionError(ValueError):
    """Raised on an illegal issue status transition."""


class Store:
    """Thin DAO over a WAL-mode SQLite database."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- events ----------------------------------------------------------

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Redact and persist an event; returns the redacted copy that was stored."""
        clean: dict[str, Any] = redact_value(event)
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (created_at, payload) VALUES (?, ?)",
                (_now(), json.dumps(clean)),
            )
        return clean

    def recent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        """Most recent events in chronological order."""
        rows = self._conn.execute(
            "SELECT payload FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["payload"]) for r in reversed(rows)]

    def event_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    # -- issues ----------------------------------------------------------

    def upsert_issue(
        self,
        fingerprint: str,
        source: str,
        severity: str,
        title: str,
        evidence: dict[str, Any] | None = None,
    ) -> int:
        """Insert a new issue or bump a recurring one.

        A recurrence bumps ``last_seen``/``count``. A recurrence of a
        *resolved* issue reopens it; ``wontfix`` stays closed.
        """
        clean_evidence = json.dumps(redact_value(evidence or {}))
        title = redact_text(title)
        now = _now()
        with self._conn:
            existing = self._conn.execute(
                "SELECT id, status FROM issues WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is None:
                cur = self._conn.execute(
                    "INSERT INTO issues (fingerprint, source, severity, title, evidence,"
                    " status, first_seen, last_seen, count)"
                    " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, 1)",
                    (fingerprint, source, severity, title, clean_evidence, now, now),
                )
                return int(cur.lastrowid)  # type: ignore[arg-type]
            reopen = existing["status"] == "resolved"
            self._conn.execute(
                "UPDATE issues SET last_seen = ?, count = count + 1, evidence = ?,"
                " severity = ?, status = CASE WHEN ? THEN 'open' ELSE status END"
                " WHERE id = ?",
                (now, clean_evidence, severity, reopen, existing["id"]),
            )
            return int(existing["id"])

    def get_issue(self, issue_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return self._issue_row(row) if row else None

    def list_issues(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE status = ? ORDER BY last_seen DESC", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM issues ORDER BY last_seen DESC").fetchall()
        return [self._issue_row(r) for r in rows]

    def set_issue_status(self, issue_id: int, new_status: str) -> None:
        if new_status not in ISSUE_STATUSES:
            raise TransitionError(f"unknown status {new_status!r}")
        with self._conn:
            row = self._conn.execute(
                "SELECT status FROM issues WHERE id = ?", (issue_id,)
            ).fetchone()
            if row is None:
                raise TransitionError(f"issue {issue_id} does not exist")
            current = row["status"]
            if new_status not in _TRANSITIONS[current]:
                raise TransitionError(
                    f"illegal transition {current!r} -> {new_status!r} for issue {issue_id}"
                )
            self._conn.execute(
                "UPDATE issues SET status = ?, last_seen = ? WHERE id = ?",
                (new_status, _now(), issue_id),
            )

    @staticmethod
    def _issue_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"])
        return d

    # -- solutions ---------------------------------------------------------

    def add_solution(
        self, issue_id: int, body_md: str, gate_category: str, auto_eligible: bool
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO solutions (issue_id, created_at, body_md, gate_category,"
                " auto_eligible) VALUES (?, ?, ?, ?, ?)",
                (issue_id, _now(), redact_text(body_md), gate_category, int(auto_eligible)),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_solution(self, solution_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM solutions WHERE id = ?", (solution_id,)
        ).fetchone()
        return dict(row) if row else None

    def solutions_for_issue(self, issue_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM solutions WHERE issue_id = ? ORDER BY id", (issue_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- approvals (append-only audit trail) -------------------------------

    def record_approval(
        self, solution_id: int, issue_id: int, decision: str, decided_by: str
    ) -> int:
        """Record an operator decision. Exactly one decision per solution, ever."""
        if decision not in ("approved", "denied"):
            raise ValueError(f"decision must be 'approved' or 'denied'; got {decision!r}")
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO approvals (solution_id, issue_id, decided_at, decision,"
                    " decided_by) VALUES (?, ?, ?, ?, ?)",
                    (solution_id, issue_id, _now(), decision, decided_by),
                )
                return int(cur.lastrowid)  # type: ignore[arg-type]
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"solution {solution_id} already has a decision") from exc

    def approval_for_solution(self, solution_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE solution_id = ?", (solution_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- aar / host health / purge audit -----------------------------------

    def add_aar_record(self, report_date: str, path: str, summary: str) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO aar_records (report_date, created_at, path, summary)"
                " VALUES (?, ?, ?, ?)",
                (report_date, _now(), path, redact_text(summary)),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def latest_aar(self) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM aar_records ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def add_host_health(self, metric: str, value: str, healthy: bool) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO host_health (created_at, metric, value, healthy)"
                " VALUES (?, ?, ?, ?)",
                (_now(), metric, redact_text(value), int(healthy)),
            )

    def latest_host_health(self) -> list[dict[str, Any]]:
        """Most recent reading per metric."""
        rows = self._conn.execute(
            "SELECT * FROM host_health WHERE id IN"
            " (SELECT MAX(id) FROM host_health GROUP BY metric) ORDER BY metric"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_log_purge_run(
        self, target: str, files_deleted: int, bytes_freed: int, dry_run: bool
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO log_purge_runs (created_at, target, files_deleted, bytes_freed,"
                " dry_run) VALUES (?, ?, ?, ?, ?)",
                (_now(), target, files_deleted, bytes_freed, int(dry_run)),
            )
