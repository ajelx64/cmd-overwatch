"""Store tests: persistence, issue lifecycle, append-only approvals, redaction at rest."""

from pathlib import Path

import pytest

from overwatch.store import Store, TransitionError


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


# -- events ----------------------------------------------------------------


def test_events_persist_across_reopen(tmp_path: Path) -> None:
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
    for i in range(10):
        store.add_event({"n": i})
    out = store.recent_events(limit=3)
    assert [e["n"] for e in out] == [7, 8, 9]


def test_fake_token_never_reaches_db_file(tmp_path: Path) -> None:
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
    a = store.upsert_issue("fp-1", "log_scan", "high", "job failed", {"exit": 1})
    b = store.upsert_issue("fp-1", "log_scan", "high", "job failed", {"exit": 1})
    assert a == b
    issue = store.get_issue(a)
    assert issue is not None
    assert issue["count"] == 2
    assert issue["status"] == "open"


def test_distinct_fingerprints_distinct_issues(store: Store) -> None:
    a = store.upsert_issue("fp-a", "log_scan", "high", "x")
    b = store.upsert_issue("fp-b", "log_scan", "low", "y")
    assert a != b
    assert len(store.list_issues()) == 2


def test_recurrence_reopens_resolved(store: Store) -> None:
    i = store.upsert_issue("fp-r", "sched", "medium", "task missed")
    store.set_issue_status(i, "resolved")
    store.upsert_issue("fp-r", "sched", "medium", "task missed")
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "open"
    assert issue["count"] == 2


def test_recurrence_never_reopens_wontfix(store: Store) -> None:
    i = store.upsert_issue("fp-w", "git", "low", "stale branch")
    store.set_issue_status(i, "drafted")
    store.set_issue_status(i, "pending_approval")
    store.set_issue_status(i, "wontfix")
    store.upsert_issue("fp-w", "git", "low", "stale branch")
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "wontfix"


def test_full_happy_path_lifecycle(store: Store) -> None:
    i = store.upsert_issue("fp-l", "log_scan", "high", "boom")
    for status in ("drafted", "pending_approval", "executing", "resolved"):
        store.set_issue_status(i, status)
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "resolved"


def test_illegal_transitions_rejected(store: Store) -> None:
    i = store.upsert_issue("fp-x", "log_scan", "high", "boom")
    with pytest.raises(TransitionError):
        store.set_issue_status(i, "executing")  # open -> executing skips drafting
    with pytest.raises(TransitionError):
        store.set_issue_status(i, "nonsense")
    with pytest.raises(TransitionError):
        store.set_issue_status(99999, "drafted")


def test_denied_returns_to_open(store: Store) -> None:
    i = store.upsert_issue("fp-d", "sched", "medium", "task disabled")
    store.set_issue_status(i, "drafted")
    store.set_issue_status(i, "pending_approval")
    store.set_issue_status(i, "open")
    issue = store.get_issue(i)
    assert issue is not None
    assert issue["status"] == "open"


def test_failed_can_be_redrafted(store: Store) -> None:
    i = store.upsert_issue("fp-f", "log_scan", "high", "boom")
    store.set_issue_status(i, "drafted")
    store.set_issue_status(i, "executing")
    store.set_issue_status(i, "failed")
    store.set_issue_status(i, "drafted")


def test_list_issues_filters_by_status(store: Store) -> None:
    a = store.upsert_issue("fp-1", "s", "high", "one")
    store.upsert_issue("fp-2", "s", "low", "two")
    store.set_issue_status(a, "drafted")
    assert [i["fingerprint"] for i in store.list_issues(status="open")] == ["fp-2"]
    assert [i["fingerprint"] for i in store.list_issues(status="drafted")] == ["fp-1"]


# -- solutions + approvals -----------------------------------------------------


def test_solution_round_trip(store: Store) -> None:
    i = store.upsert_issue("fp-s", "log_scan", "high", "boom")
    sid = store.add_solution(i, "## Fix\nrestart it", "none", auto_eligible=True)
    sol = store.get_solution(sid)
    assert sol is not None
    assert sol["issue_id"] == i
    assert sol["auto_eligible"] == 1
    assert store.solutions_for_issue(i)[0]["id"] == sid


def test_approval_audit_row(store: Store) -> None:
    i = store.upsert_issue("fp-ap", "sched", "high", "boom")
    sid = store.add_solution(i, "fix", "secrets", auto_eligible=False)
    store.record_approval(sid, i, "approved", "localhost-operator")
    rec = store.approval_for_solution(sid)
    assert rec is not None
    assert rec["decision"] == "approved"
    assert rec["decided_by"] == "localhost-operator"
    assert rec["decided_at"]


def test_double_decision_rejected(store: Store) -> None:
    i = store.upsert_issue("fp-dd", "sched", "high", "boom")
    sid = store.add_solution(i, "fix", "secrets", auto_eligible=False)
    store.record_approval(sid, i, "denied", "localhost-operator")
    with pytest.raises(ValueError, match="already has a decision"):
        store.record_approval(sid, i, "approved", "localhost-operator")


def test_invalid_decision_rejected(store: Store) -> None:
    i = store.upsert_issue("fp-iv", "sched", "high", "boom")
    sid = store.add_solution(i, "fix", "none", auto_eligible=True)
    with pytest.raises(ValueError, match="decision"):
        store.record_approval(sid, i, "maybe", "localhost-operator")


# -- aar / host health / purge audit -------------------------------------------


def test_aar_records(store: Store) -> None:
    store.add_aar_record("2026-01-01", "reports/daily/2026-01-01-aar.md", "all green")
    store.add_aar_record("2026-01-02", "reports/daily/2026-01-02-aar.md", "1 issue")
    latest = store.latest_aar()
    assert latest is not None
    assert latest["report_date"] == "2026-01-02"


def test_host_health_latest_per_metric(store: Store) -> None:
    store.add_host_health("disk_free_pct", "44", True)
    store.add_host_health("disk_free_pct", "9", False)
    store.add_host_health("backup_age_hours", "3", True)
    latest = store.latest_host_health()
    by_metric = {r["metric"]: r for r in latest}
    assert by_metric["disk_free_pct"]["value"] == "9"
    assert by_metric["disk_free_pct"]["healthy"] == 0
    assert by_metric["backup_age_hours"]["healthy"] == 1


def test_log_purge_audit(store: Store) -> None:
    store.add_log_purge_run("example-project", 4, 123456, dry_run=True)
    row = store._conn.execute("SELECT * FROM log_purge_runs").fetchone()
    assert row["files_deleted"] == 4
    assert row["dry_run"] == 1
