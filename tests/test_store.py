"""Store tests: persistence, issue lifecycle, append-only approvals, redaction at rest.

Covers ``overwatch.store.Store``: event persistence and WAL durability across
reopen, the issue upsert/recurrence/status-transition state machine (see the
lifecycle diagram in ``overwatch/store.py``'s module docstring), append-only
approval recording, and that every write path routes through
``overwatch.redact`` so nothing secret-shaped is ever written to the SQLite
file. Every test gets its own throwaway ``Store`` over a fresh ``tmp_path``
database (via the ``store`` fixture, or created inline where a test needs to
close and reopen the same file to prove persistence).
"""

from pathlib import Path

import pytest

from overwatch.store import Store, TransitionError


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """A fresh Store over its own SQLite file, isolated per test by tmp_path."""
    return Store(tmp_path / "test.db")


# -- events ----------------------------------------------------------------


def test_events_persist_across_reopen(tmp_path: Path) -> None:
    """Events written by one Store instance must still be readable, in
    order, after closing and reopening a new Store over the same db file --
    proves durability, not just in-memory buffering."""
    db = tmp_path / "e.db"
    s1 = Store(db)
    s1.add_event({"event_type": "tool", "tool_name": "Read", "input_summary": "x"})
    s1.add_event({"event_type": "tool", "tool_name": "Edit", "input_summary": "y"})
    s1.close()

    s2 = Store(db)
    events = s2.recent_events()
    assert [e["tool_name"] for e in events] == ["Read", "Edit"]
    assert s2.event_count() == 2
    s2.close()


def test_recent_events_chronological_with_limit(store: Store) -> None:
    """recent_events(limit=N) returns the N *most recent* events, oldest
    first -- proves both the truncation and the re-ordering back to
    chronological order (the underlying query fetches newest-first)."""
    for i in range(10):
        store.add_event({"n": i})
    out = store.recent_events(limit=3)
    assert [e["n"] for e in out] == [7, 8, 9]


def test_fake_token_never_reaches_db_file(tmp_path: Path) -> None:
    """A GitHub-token-shaped fixture value must be absent from both the
    in-memory returned copy and the raw bytes of the on-disk .db file --
    the stronger of the two checks, since redact_value could in principle
    scrub the returned dict while a bug left the raw payload unredacted
    before the INSERT."""
    fake = "ghp_aaaabbbbccccddddeeeeffff000011112222"  # gitleaks:allow
    db = tmp_path / "r.db"
    s = Store(db)
    returned = s.add_event({"tool_input": {"command": f"login --token {fake}"}})
    assert fake not in repr(returned)
    s.close()  # checkpoint WAL so the main db file holds everything
    raw = db.read_bytes()
    assert fake.encode() not in raw


# -- issues ------------------------------------------------------------------


def test_upsert_creates_then_bumps(store: Store) -> None:
    """Upserting the same fingerprint twice returns the same issue id and
    bumps its count to 2, rather than creating a second row -- this is the
    dedup contract every collector's persist_findings() relies on."""
    a = store.upsert_issue("fp-1", "log_scan", "high", "job failed", {"exit": 1})
    b = store.upsert_issue("fp-1", "log_scan", "high", "job failed", {"exit": 1})
    assert a == b
    issue = store.get_issue(a)
    assert issue is not None
    assert issue["count"] == 2
    assert issue["status"] == "open"


def test_distinct_fingerprints_distinct_issues(store: Store) -> None:
    """Two different fingerprints must create two distinct issue rows."""
    a = store.upsert_issue("fp-a", "log_scan", "high", "x")
    b = store.upsert_issue("fp-b", "log_scan", "low", "y")
    assert a != b
    assert len(store.list_issues()) == 2


def test_recurrence_reopens_resolved(store: Store) -> None:
    """A fingerprint recurring after its issue was marked resolved must
    reopen it (status back to 'open') and bump count -- per the module
    docstring's `resolved -> open` transition for "signal recurred"."""
    i = store.upsert_issue("fp-r", "sched", "medium", "task missed")
    store.set_issue_status(i, "resolved")
    store.upsert_issue("fp-r", "sched", "medium", "task missed")
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "open"
    assert issue["count"] == 2


def test_recurrence_never_reopens_wontfix(store: Store) -> None:
    """A fingerprint recurring after being marked 'wontfix' must stay
    'wontfix' -- the operator explicitly said no, and a recurring signal
    must not silently overrule that decision."""
    i = store.upsert_issue("fp-w", "git", "low", "stale branch")
    store.set_issue_status(i, "drafted")
    store.set_issue_status(i, "pending_approval")
    store.set_issue_status(i, "wontfix")
    store.upsert_issue("fp-w", "git", "low", "stale branch")
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "wontfix"


def test_full_happy_path_lifecycle(store: Store) -> None:
    """Walks the full open -> drafted -> pending_approval -> executing ->
    resolved chain to prove every forward transition in the documented
    lifecycle is legal in sequence."""
    i = store.upsert_issue("fp-l", "log_scan", "high", "boom")
    for status in ("drafted", "pending_approval", "executing", "resolved"):
        store.set_issue_status(i, status)
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "resolved"


def test_illegal_transitions_rejected(store: Store) -> None:
    """Three ways set_issue_status can be misused must all raise
    TransitionError: skipping a required intermediate state (open direct to
    executing), an unknown status string, and a nonexistent issue id."""
    i = store.upsert_issue("fp-x", "log_scan", "high", "boom")
    with pytest.raises(TransitionError):
        store.set_issue_status(i, "executing")  # open -> executing skips drafting
    with pytest.raises(TransitionError):
        store.set_issue_status(i, "nonsense")
    with pytest.raises(TransitionError):
        store.set_issue_status(99999, "drafted")


def test_denied_returns_to_open(store: Store) -> None:
    """An operator denying a pending_approval solution must send the issue
    back to 'open' (not 'wontfix') so it can be redrafted later."""
    i = store.upsert_issue("fp-d", "sched", "medium", "task disabled")
    store.set_issue_status(i, "drafted")
    store.set_issue_status(i, "pending_approval")
    store.set_issue_status(i, "open")
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "open"


def test_failed_can_be_redrafted(store: Store) -> None:
    """A solution that failed during execution must be able to return to
    'drafted' for another attempt, per the documented `failed -> drafted`
    redraft transition. (No final assertion needed: an illegal transition
    here would raise and fail the test on its own.)"""
    i = store.upsert_issue("fp-f", "log_scan", "high", "boom")
    store.set_issue_status(i, "drafted")
    store.set_issue_status(i, "executing")
    store.set_issue_status(i, "failed")
    store.set_issue_status(i, "drafted")


def test_list_issues_filters_by_status(store: Store) -> None:
    """list_issues(status=...) must return only issues in that status,
    excluding one that was moved to 'drafted'."""
    a = store.upsert_issue("fp-1", "s", "high", "one")
    store.upsert_issue("fp-2", "s", "low", "two")
    store.set_issue_status(a, "drafted")
    assert [i["fingerprint"] for i in store.list_issues(status="open")] == ["fp-2"]
    assert [i["fingerprint"] for i in store.list_issues(status="drafted")] == ["fp-1"]


# -- solutions + approvals -----------------------------------------------------


def test_solution_round_trip(store: Store) -> None:
    """A solution attached to an issue can be fetched by id and listed
    under its issue, with auto_eligible stored as the expected 1/0 int."""
    i = store.upsert_issue("fp-s", "log_scan", "high", "boom")
    sid = store.add_solution(i, "## Fix\nrestart it", "none", auto_eligible=True)
    sol = store.get_solution(sid)
    assert sol is not None
    assert sol["issue_id"] == i
    assert sol["auto_eligible"] == 1
    assert store.solutions_for_issue(i)[0]["id"] == sid


def test_approval_audit_row(store: Store) -> None:
    """record_approval writes a retrievable row carrying the decision,
    who decided, and a non-empty timestamp -- the audit trail's shape."""
    i = store.upsert_issue("fp-ap", "sched", "high", "boom")
    sid = store.add_solution(i, "fix", "secrets", auto_eligible=False)
    store.record_approval(sid, i, "approved", "localhost-operator")
    rec = store.approval_for_solution(sid)
    assert rec is not None
    assert rec["decision"] == "approved"
    assert rec["decided_by"] == "localhost-operator"
    assert rec["decided_at"]


def test_double_decision_rejected(store: Store) -> None:
    """A second record_approval call for the same solution must raise --
    the approvals table is append-only per solution (one decision, ever),
    enforced by the UNIQUE constraint on solution_id."""
    i = store.upsert_issue("fp-dd", "sched", "high", "boom")
    sid = store.add_solution(i, "fix", "secrets", auto_eligible=False)
    store.record_approval(sid, i, "denied", "localhost-operator")
    with pytest.raises(ValueError, match="already has a decision"):
        store.record_approval(sid, i, "approved", "localhost-operator")


def test_invalid_decision_rejected(store: Store) -> None:
    """A decision string outside {'approved', 'denied'} must be rejected
    before it ever reaches the database's CHECK constraint."""
    i = store.upsert_issue("fp-iv", "sched", "high", "boom")
    sid = store.add_solution(i, "fix", "none", auto_eligible=True)
    with pytest.raises(ValueError, match="decision"):
        store.record_approval(sid, i, "maybe", "localhost-operator")


# -- aar / host health / purge audit -------------------------------------------


def test_aar_records(store: Store) -> None:
    """latest_aar() returns the most recently added AAR record by insertion
    order, not by report_date sorting."""
    store.add_aar_record("2026-01-01", "reports/daily/2026-01-01-aar.md", "all green")
    store.add_aar_record("2026-01-02", "reports/daily/2026-01-02-aar.md", "1 issue")
    latest = store.latest_aar()
    assert latest is not None
    assert latest["report_date"] == "2026-01-02"


def test_host_health_latest_per_metric(store: Store) -> None:
    """latest_host_health() returns one row per metric -- the most recent
    reading -- so an earlier healthy reading for the same metric must not
    mask a later unhealthy one."""
    store.add_host_health("disk_free_pct", "44", True)
    store.add_host_health("disk_free_pct", "9", False)
    store.add_host_health("backup_age_hours", "3", True)
    latest = store.latest_host_health()
    by_metric = {r["metric"]: r for r in latest}
    assert by_metric["disk_free_pct"]["value"] == "9"
    assert by_metric["disk_free_pct"]["healthy"] == 0
    assert by_metric["backup_age_hours"]["healthy"] == 1


def test_log_purge_audit(store: Store) -> None:
    """add_log_purge_run writes a row with the given counts and dry_run
    flag, retrievable via a raw SELECT -- the shape log_purge.py relies on."""
    store.add_log_purge_run("example-project", 4, 123456, dry_run=True)
    row = store._conn.execute("SELECT * FROM log_purge_runs").fetchone()
    assert row["files_deleted"] == 4
    assert row["dry_run"] == 1
