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

:class:`Store` is instantiated once per process, pointed at the same
database file (``Config.db_path``) by both the collector and the server, and
is meant to be the sole point of contact with SQL for the rest of the
codebase — callers pass and receive plain dicts, never a connection or a raw
query. Depends on :mod:`overwatch.redact` for scrubbing values before
they're written, and on the standard library ``sqlite3``/``json`` for
everything else.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from overwatch.redact import redact_text, redact_value

# The full set of valid ``issues.status`` values — used by set_issue_status
# to reject a typo'd/unknown target status before it ever reaches _TRANSITIONS.
ISSUE_STATUSES = frozenset(
    {"open", "drafted", "pending_approval", "executing", "resolved", "failed", "wontfix"}
)

# Allowed status -> {next statuses} edges, enforced by set_issue_status. Kept
# as data (rather than inline if/elif checks) so there is one place that
# decides what transitions are legal. THIS TABLE is authoritative for that —
# set_issue_status checks against it, never the module docstring's diagram.
# The two currently disagree: this table allows "open" -> "resolved" directly,
# an edge the docstring's diagram and its four named shortcuts do not show.
# TODO(comprehension): could not determine whether that edge is intentional
# (diagram is just incomplete) or a bug (this table over-permits) — flagging
# the discrepancy rather than guessing which side is wrong.
# An empty frozenset means terminal: "wontfix" has no outgoing edges,
# matching the module docstring's "wontfix is terminal".
_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"drafted", "resolved"}),
    "drafted": frozenset({"pending_approval", "executing"}),
    "pending_approval": frozenset({"executing", "open", "wontfix"}),
    "executing": frozenset({"resolved", "failed"}),
    "failed": frozenset({"drafted"}),
    "resolved": frozenset({"open"}),
    "wontfix": frozenset(),
}

# Schema notes (kept here rather than as SQL comments, so editing this
# string can't be mistaken for an executable change):
# - `fingerprint` is UNIQUE on `issues`: it's the dedup key upsert_issue
#   selects on to decide "new issue" vs. "recurrence of an existing one".
# - `evidence`/`summary`-style payload columns are TEXT holding a JSON blob
#   (see _issue_row) rather than normalized columns, since the shape of
#   evidence varies by issue source and isn't queried on directly.
# - `solutions.issue_id` / `approvals.issue_id` / `approvals.solution_id`
#   use REFERENCES so PRAGMA foreign_keys=ON (set in Store.__init__) can
#   catch orphaned rows; `approvals.solution_id` is additionally UNIQUE,
#   which is what record_approval relies on to allow only one decision per
#   solution (see its docstring).
# - `idx_issues_status` backs list_issues' `WHERE status = ?` filter.
#   `idx_events_created` indexes `created_at`, but recent_events (the only
#   read of `events` in this file) currently orders by `id`, not
#   `created_at` — TODO(comprehension): could not verify this index is
#   actually used by any query in this codebase; may be provisioned for a
#   query outside this file, or unused.
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
    auto_eligible INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'investigate-fix'
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
    """Return the current UTC timestamp as an ISO-8601 string, for storage in a TEXT column.

    Centralized so every write path stamps times the same way (UTC,
    ISO-8601) — SQLite has no native datetime type, and mixing formats or
    timezones across columns would make chronological comparisons unreliable.

    Returns:
        The current time, formatted via ``datetime.isoformat()``.
    """
    return datetime.now(UTC).isoformat()


class TransitionError(ValueError):
    """Raised when an issue status change is invalid.

    Covers three distinct cases, all treated the same way by callers: an
    unrecognized target status, a reference to an issue that doesn't exist,
    and a structurally-known but disallowed transition (see _TRANSITIONS).
    """


class Store:
    """Thin data-access layer over a single WAL-mode SQLite database.

    One instance owns one long-lived connection for the lifetime of the
    process; there is no per-call connect/close. Every public method here
    corresponds to one specific query or write — this class deliberately
    does not expose the underlying connection or accept arbitrary SQL, so
    callers can't bypass the redaction applied on write paths (see the
    module docstring).
    """

    def __init__(self, db_path: Path | str) -> None:
        """Open (creating if needed) the database at ``db_path`` and ensure its schema.

        Args:
            db_path: Path to the SQLite database file. Its parent directory
                is created if missing.
        """
        # --- Step 1: resolve the db path, creating its parent directory if needed ---
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # --- Step 2: open the shared connection ---
        # check_same_thread=False: this one connection is shared by whatever
        # threads call into the Store (e.g. concurrent request handlers), so
        # sqlite3 must not reject cross-thread use — SQLite's own locking
        # (see PRAGMA journal_mode below) is relied on for safety instead of
        # Python-level per-thread connections.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # --- Step 3: configure pragmas ---
        # WAL mode lets readers (the dashboard) proceed concurrently with a
        # writer (the collector) instead of blocking on a single writer lock,
        # which matters since this database is shared by both (see module
        # docstring).
        self._conn.execute("PRAGMA journal_mode=WAL")
        # sqlite3 does not enforce FOREIGN KEY constraints unless this pragma
        # is set on the connection — without it, the REFERENCES clauses in
        # _SCHEMA would be silently decorative.
        self._conn.execute("PRAGMA foreign_keys=ON")
        # --- Step 4: apply schema and any pending migrations, then commit ---
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Apply additive migrations for databases created before a schema change.

        _SCHEMA already declares `solutions.kind` for a freshly created
        database; this only patches databases that were created before that
        column existed. There's no migration-version table — each migration
        checks for its own column/table and is a no-op if already applied,
        which keeps this simple for a single-file local database at the cost
        of not scaling to many migrations.
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(solutions)")}
        if "kind" not in cols:
            self._conn.execute(
                "ALTER TABLE solutions ADD COLUMN kind TEXT NOT NULL DEFAULT 'investigate-fix'"
            )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    # -- events ----------------------------------------------------------

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Redact and persist an event.

        Args:
            event: The raw event payload (e.g. a serialized ``AnyEvent``
                from :mod:`models`) to store.

        Returns:
            The redacted copy that was actually written to disk — callers
            that echo the event back (e.g. over a websocket) should use
            this, not the input, so nothing secret-shaped leaks past storage.
        """
        clean: dict[str, Any] = redact_value(event)
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (created_at, payload) VALUES (?, ?)",
                (_now(), json.dumps(clean)),
            )
        return clean

    def recent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return the most recent events, oldest first.

        Args:
            limit: Maximum number of events to return.

        Returns:
            Up to ``limit`` most recent event payloads, in chronological
            (oldest-first) order for display. Fetched as ``ORDER BY id DESC
            LIMIT ?`` (cheap: newest rows first, capped) and then reversed
            in Python, rather than ``ORDER BY id ASC`` with an offset, which
            would have to scan past every older row to reach the tail.
        """
        rows = self._conn.execute(
            "SELECT payload FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["payload"]) for r in reversed(rows)]

    def event_count(self) -> int:
        """Return the total number of stored events."""
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

        Args:
            fingerprint: Stable dedup key identifying "the same issue" across
                runs (see the UNIQUE constraint on ``issues.fingerprint`` in
                _SCHEMA).
            source: Where this issue came from (e.g. a collector name).
            severity: Severity label; overwritten on every recurrence, so a
                worsening/improving signal is reflected on the existing row.
            title: Human-readable summary; redacted before storage.
            evidence: Arbitrary structured detail for the issue; redacted
                and stored as JSON. Defaults to ``{}`` if omitted.

        Returns:
            The issue's row id, whether newly inserted or an existing one
            that was bumped.

        Note:
            The existence check (``SELECT ... WHERE fingerprint = ?``) and
            the following INSERT/UPDATE are two separate statements. Each is
            wrapped for commit by ``with self._conn:``, but that context
            manager governs transaction commit/rollback, not mutual
            exclusion between callers — it does not make the check-then-act
            sequence atomic. Two concurrent calls for a brand-new fingerprint
            could both see "no existing row" and both attempt the INSERT,
            and the second would fail on the UNIQUE constraint instead of
            falling through to the update path.
        """
        # --- Step 1: sanitize inputs before they touch SQL or disk ---
        clean_evidence = json.dumps(redact_value(evidence or {}))
        title = redact_text(title)
        now = _now()
        with self._conn:
            # --- Step 2: look up whether this fingerprint already has a row ---
            existing = self._conn.execute(
                "SELECT id, status FROM issues WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is None:
                # --- Step 3a: no existing row -> insert a brand-new issue ---
                cur = self._conn.execute(
                    "INSERT INTO issues (fingerprint, source, severity, title, evidence,"
                    " status, first_seen, last_seen, count)"
                    " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, 1)",
                    (fingerprint, source, severity, title, clean_evidence, now, now),
                )
                return int(cur.lastrowid)  # type: ignore[arg-type]
            # --- Step 3b: existing row -> bump it, reopening only if it was resolved ---
            # A recurrence of a resolved issue means the signal came back
            # after the operator thought it was fixed, so it goes back to
            # "open"; any other status (drafted/pending_approval/executing/
            # wontfix) is left alone rather than clobbered by a recurring
            # signal — in particular this keeps a "wontfix" issue closed.
            reopen = existing["status"] == "resolved"
            self._conn.execute(
                "UPDATE issues SET last_seen = ?, count = count + 1, evidence = ?,"
                " severity = ?, status = CASE WHEN ? THEN 'open' ELSE status END"
                " WHERE id = ?",
                (now, clean_evidence, severity, reopen, existing["id"]),
            )
            return int(existing["id"])

    def get_issue(self, issue_id: int) -> dict[str, Any] | None:
        """Fetch one issue by id.

        Args:
            issue_id: The issue's row id.

        Returns:
            The issue as a dict (see :meth:`_issue_row`), or ``None`` if no
            issue with that id exists.
        """
        row = self._conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return self._issue_row(row) if row else None

    def list_issues(self, status: str | None = None) -> list[dict[str, Any]]:
        """List issues, optionally filtered by status, most recently seen first.

        Args:
            status: If given, only issues with this exact status are
                returned (see ``idx_issues_status`` in _SCHEMA, which backs
                this filter). If omitted, all issues are returned.

        Returns:
            Matching issues as dicts, ordered by ``last_seen`` descending.
        """
        if status is not None:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE status = ? ORDER BY last_seen DESC", (status,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM issues ORDER BY last_seen DESC").fetchall()
        return [self._issue_row(r) for r in rows]

    def set_issue_status(self, issue_id: int, new_status: str) -> None:
        """Move an issue to a new status, enforcing the lifecycle in _TRANSITIONS.

        Args:
            issue_id: The issue's row id.
            new_status: The status to move to.

        Raises:
            TransitionError: If ``new_status`` isn't a known status, if
                ``issue_id`` doesn't exist, or if the current status doesn't
                allow moving to ``new_status`` (see the module docstring's
                lifecycle diagram and _TRANSITIONS).
        """
        # Reject an unknown status up front — this doubles as the KeyError
        # guard for the `_TRANSITIONS[current]` lookup below, since every
        # key in _TRANSITIONS is a member of ISSUE_STATUSES.
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
        """Convert a raw ``issues`` row into a dict with ``evidence`` deserialized.

        Args:
            row: A row fetched from the ``issues`` table.

        Returns:
            A dict of the row's columns, with ``evidence`` parsed from its
            stored JSON-text form back into a Python object.
        """
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"])
        return d

    # -- solutions ---------------------------------------------------------

    def add_solution(
        self,
        issue_id: int,
        body_md: str,
        gate_category: str,
        auto_eligible: bool,
        kind: str = "investigate-fix",
    ) -> int:
        """Record a proposed solution for an issue.

        Args:
            issue_id: The issue this solution addresses.
            body_md: The solution's write-up, in Markdown; redacted before
                storage.
            gate_category: Which approval-gate category this solution falls
                under (matching is done elsewhere; this module just stores
                the label).
            auto_eligible: Whether this solution may run without an operator
                approval — stored as 0/1 since SQLite has no bool type.
            kind: What kind of solution this is; defaults to
                ``"investigate-fix"``.

        Returns:
            The new solution's row id.
        """
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO solutions (issue_id, created_at, body_md, gate_category,"
                " auto_eligible, kind) VALUES (?, ?, ?, ?, ?, ?)",
                (issue_id, _now(), redact_text(body_md), gate_category, int(auto_eligible), kind),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def get_solution(self, solution_id: int) -> dict[str, Any] | None:
        """Fetch one solution by id, or ``None`` if it doesn't exist."""
        row = self._conn.execute(
            "SELECT * FROM solutions WHERE id = ?", (solution_id,)
        ).fetchone()
        return dict(row) if row else None

    def solutions_for_issue(self, issue_id: int) -> list[dict[str, Any]]:
        """List all solutions proposed for an issue, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM solutions WHERE issue_id = ? ORDER BY id", (issue_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- approvals (append-only audit trail) -------------------------------

    def record_approval(
        self, solution_id: int, issue_id: int, decision: str, decided_by: str
    ) -> int:
        """Record an operator decision. Exactly one decision per solution, ever.

        Args:
            solution_id: The solution being decided on.
            issue_id: The issue that solution belongs to (denormalized onto
                the approvals row so a decision can be looked up without a
                join back through solutions).
            decision: Either ``"approved"`` or ``"denied"``.
            decided_by: Identifier for who/what made the decision.

        Returns:
            The new approval record's row id.

        Raises:
            ValueError: If ``decision`` isn't ``"approved"``/``"denied"``,
                or if ``solution_id`` already has a decision recorded.
        """
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
            # approvals.solution_id is UNIQUE (see _SCHEMA): rather than a
            # separate check-then-insert (which would have the same race as
            # upsert_issue's, see its docstring), the "one decision per
            # solution" rule is enforced by the database and this just
            # translates the resulting IntegrityError into a clearer error.
            raise ValueError(f"solution {solution_id} already has a decision") from exc

    def approval_for_solution(self, solution_id: int) -> dict[str, Any] | None:
        """Fetch the recorded decision for a solution, or ``None`` if undecided."""
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE solution_id = ?", (solution_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- aar / host health / purge audit -----------------------------------

    def add_aar_record(self, report_date: str, path: str, summary: str) -> int:
        """Record that an after-action report was generated.

        Args:
            report_date: The date the report covers (caller-defined format).
            path: Where the generated report was written.
            summary: Short summary of the report; redacted before storage.

        Returns:
            The new record's row id.
        """
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO aar_records (report_date, created_at, path, summary)"
                " VALUES (?, ?, ?, ?)",
                (report_date, _now(), path, redact_text(summary)),
            )
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def latest_aar(self) -> dict[str, Any] | None:
        """Fetch the most recently created AAR record, or ``None`` if there are none."""
        row = self._conn.execute("SELECT * FROM aar_records ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def add_host_health(self, metric: str, value: str, healthy: bool) -> None:
        """Record one host-health reading.

        Args:
            metric: Name of the health metric (e.g. a check identifier).
            value: The reading's value, as text; redacted before storage.
            healthy: Whether this reading is within the healthy range —
                stored as 0/1 since SQLite has no bool type.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO host_health (created_at, metric, value, healthy)"
                " VALUES (?, ?, ?, ?)",
                (_now(), metric, redact_text(value), int(healthy)),
            )

    def latest_host_health(self) -> list[dict[str, Any]]:
        """Return the single most recent reading for each distinct metric.

        The subquery picks the max row id per ``metric`` group, so each
        metric contributes exactly one row to the result — this reports
        current status per check, not the full reading history, which is
        what a dashboard summary needs.
        """
        rows = self._conn.execute(
            "SELECT * FROM host_health WHERE id IN"
            " (SELECT MAX(id) FROM host_health GROUP BY metric) ORDER BY metric"
        ).fetchall()
        return [dict(r) for r in rows]

    def add_log_purge_run(
        self, target: str, files_deleted: int, bytes_freed: int, dry_run: bool
    ) -> None:
        """Record the outcome of one log-purge run, for audit purposes.

        Args:
            target: What was purged (e.g. a target name or log directory).
            files_deleted: Number of files removed (or that would have been,
                if ``dry_run``).
            bytes_freed: Total size of those files.
            dry_run: Whether this run only simulated the purge rather than
                actually deleting anything — stored as 0/1 since SQLite has
                no bool type. Recording this alongside real runs keeps the
                audit trail able to distinguish a dry-run from an actual
                deletion.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO log_purge_runs (created_at, target, files_deleted, bytes_freed,"
                " dry_run) VALUES (?, ?, ?, ?, ?)",
                (_now(), target, files_deleted, bytes_freed, int(dry_run)),
            )
