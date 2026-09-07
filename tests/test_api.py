"""Server API tests: health board, issues, the approve/deny write path,
double-decision rejection, and the loopback-binding invariant.

Covers ``server.py``'s FastAPI routes end-to-end via ``TestClient`` against a real
(tmp-path) SQLite-backed ``Store`` and a ``dry_run=True`` ``Config`` (matching the
shipped default), so approvals really persist and the executor really runs its
dry-run branch rather than being mocked out. ``server_mod.config``/``server_mod.store``
are monkeypatched per-test onto the module-level globals the routes read, which is how
each test gets an isolated database without restarting the app.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server as server_mod
from overwatch.config import Config, Target
from overwatch.store import Store


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a TestClient wired to a fresh tmp-path Store/Config pair.

    ``dry_run=True`` and a target repo path that is never actually initialized as a
    git repo are both deliberate: most tests here only need the HTTP/DB behavior,
    not a working executor run, so the executor's own "no usable target repo" /
    dry-run short-circuits keep these tests fast and isolated from git.
    """
    cfg = Config(
        targets=(Target(name="proj", repo=tmp_path / "repo"),),
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        dry_run=True,
    )
    store = Store(cfg.db_path)
    monkeypatch.setattr(server_mod, "config", cfg)
    monkeypatch.setattr(server_mod, "store", store)
    return TestClient(server_mod.app)


def seed_pending(client: TestClient, gated: bool = True) -> tuple[int, int]:
    """Seed one issue with one solution, optionally driving it to pending_approval.

    ``gated=True`` (the default) produces the common case tests exercise: an
    "uncertain" non-auto-eligible solution sitting in pending_approval, ready for a
    decision. ``gated=False`` produces an auto-eligible solution left in "drafted"
    instead, for tests that need a solution the approval endpoint should reject
    (issue not pending_approval).
    """
    store = server_mod.store
    assert store is not None
    issue_id = store.upsert_issue(
        "fp-api", "log_scan", "high", "proj/job: run failed with exit 1",
        {"target": "proj", "task": "job"},
    )
    sid = store.add_solution(
        issue_id, "## the plan", "uncertain" if gated else "none",
        auto_eligible=not gated, kind="investigate-fix",
    )
    store.set_issue_status(issue_id, "drafted")
    if gated:
        store.set_issue_status(issue_id, "pending_approval")
    return issue_id, sid


# -- reads ---------------------------------------------------------------------


def test_health_board_shape(client: TestClient) -> None:
    """The dashboard's summary endpoint must roll a pending, high-severity issue up
    into the counts and per-target tile a human glances at first.

    A wrong rollup here would show a clean board while something is actually waiting
    on the operator.
    """
    seed_pending(client)
    board = client.get("/api/health-board").json()
    assert board["dry_run"] is True
    assert board["pending_approvals"] == 1
    assert board["active_by_severity"] == {"high": 1}
    assert board["tiles"][0]["target"] == "proj"
    assert board["tiles"][0]["worst_severity"] == "high"


def test_issue_listing_and_detail(client: TestClient) -> None:
    """Filtered listing must return only matching issues, detail must join in its
    solutions with no approval recorded yet, and an unknown id must 404 rather than
    error or return an empty-shaped success body.
    """
    issue_id, sid = seed_pending(client)
    issues = client.get("/api/issues", params={"status": "pending_approval"}).json()
    assert [i["id"] for i in issues] == [issue_id]
    detail = client.get(f"/api/issues/{issue_id}").json()
    assert detail["issue"]["fingerprint"] == "fp-api"
    assert detail["solutions"][0]["id"] == sid
    assert detail["solutions"][0]["approval"] is None
    assert client.get("/api/issues/9999").status_code == 404


def test_pending_approvals_lists_undecided(client: TestClient) -> None:
    """The operator's approval queue must surface a gated issue awaiting a decision,
    joined with the specific solution it needs a decision on — this is the list a
    human actually acts from.
    """
    issue_id, sid = seed_pending(client)
    pending = client.get("/api/approvals/pending").json()
    assert len(pending) == 1
    assert pending[0]["issue"]["id"] == issue_id
    assert pending[0]["solution"]["id"] == sid


# -- approve / deny ----------------------------------------------------------------


def test_approve_dry_run_records_audit_and_plan(client: TestClient) -> None:
    """Approving a gated solution must both persist an audit record (who/what decision)
    and dispatch to the executor, which — in this fixture's dry-run config — either
    plans-without-spawning or fails closed on the missing repo; either is acceptable
    here because this test is checking the approval/audit wiring, not executor behavior
    (that is ``test_executor.py``'s job).
    """
    issue_id, sid = seed_pending(client)
    resp = client.post(f"/api/approvals/{sid}/decision", json={"decision": "approved"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    # dry_run: executor records the plan and spawns nothing
    assert body["execution"]["status"] in ("dry-run", "failed")  # failed if repo missing
    store = server_mod.store
    assert store is not None
    approval = store.approval_for_solution(sid)
    assert approval is not None
    assert approval["decision"] == "approved"
    assert approval["decided_by"] == "localhost-operator"


def test_double_decision_rejected_with_409(client: TestClient) -> None:
    """A solution that already has a recorded decision must reject a second one with
    409, not silently overwrite the audit trail with a conflicting decision.
    """
    _, sid = seed_pending(client)
    client.post(f"/api/approvals/{sid}/decision", json={"decision": "approved"})
    resp = client.post(f"/api/approvals/{sid}/decision", json={"decision": "denied"})
    assert resp.status_code == 409
    assert "already has a decision" in resp.json()["detail"]


def test_deny_returns_issue_to_open(client: TestClient) -> None:
    """A plain denial must send the issue back to "open" (still actionable later) and
    record the denial in the audit trail, distinct from the terminal wontfix case below.
    """
    issue_id, sid = seed_pending(client)
    resp = client.post(f"/api/approvals/{sid}/decision", json={"decision": "denied"})
    assert resp.json()["issue_status"] == "open"
    store = server_mod.store
    assert store is not None
    issue = store.get_issue(issue_id)
    assert issue is not None and issue["status"] == "open"
    audit = store.approval_for_solution(sid)
    assert audit is not None and audit["decision"] == "denied"


def test_deny_wontfix_is_terminal(client: TestClient) -> None:
    """Denying with ``wontfix=True`` must move the issue to the terminal "wontfix"
    state instead of "open" — an operator explicitly closing an issue must not have
    it resurface as still-open work.
    """
    issue_id, sid = seed_pending(client)
    client.post(f"/api/approvals/{sid}/decision", json={"decision": "denied", "wontfix": True})
    store = server_mod.store
    assert store is not None
    issue = store.get_issue(issue_id)
    assert issue is not None and issue["status"] == "wontfix"


def test_decision_on_missing_solution_404(client: TestClient) -> None:
    """Deciding on a solution id that does not exist must 404, not 500 or silently
    no-op.
    """
    resp = client.post("/api/approvals/999/decision", json={"decision": "approved"})
    assert resp.status_code == 404


def test_decision_on_non_pending_issue_409(client: TestClient) -> None:
    """A solution whose issue was never put into pending_approval (here: auto-eligible,
    left in "drafted") must refuse a decision with 409 rather than accepting an
    approval that was never actually requested.
    """
    store = server_mod.store
    assert store is not None
    issue_id = store.upsert_issue("fp-open", "log_scan", "low", "x", {"target": "proj"})
    sid = store.add_solution(issue_id, "y", "none", auto_eligible=True)
    resp = client.post(f"/api/approvals/{sid}/decision", json={"decision": "approved"})
    assert resp.status_code == 409


def test_reexecute_refuses_unapproved_gated(client: TestClient) -> None:
    """The re-execute endpoint (used to retry after flipping dry_run off) must still
    go through the executor's own authorization check — it is not a bypass for a
    gated solution that was never approved.
    """
    _, sid = seed_pending(client)
    resp = client.post(f"/api/solutions/{sid}/execute")
    assert resp.json()["status"] == "refused"


# -- invariants ----------------------------------------------------------------------


def test_default_binding_is_loopback() -> None:
    """The shipped default must bind to loopback only, never a routable interface,
    without any explicit configuration — this dashboard is not meant to be exposed.
    """
    from overwatch.config import Config as Cfg

    assert Cfg().host == "127.0.0.1"


def test_aar_404_before_first_report(client: TestClient) -> None:
    """Before any after-action report has been generated, the endpoint must 404
    rather than return a null/empty-shaped 200 that could be mistaken for real data.
    """
    assert client.get("/api/aar/latest").status_code == 404


def test_aar_latest_includes_content_field(client: TestClient) -> None:
    """The latest-AAR endpoint must read the report file's actual content off disk
    and include it in the response, not just the DB record's metadata.
    """
    reports = server_mod.config.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    report_file = reports / "aar_test.md"
    report_file.write_text("# AAR\nAll good.", encoding="utf-8")
    store = server_mod.store
    assert store is not None
    store.add_aar_record("2026-06-06", str(report_file), "All good.")
    resp = client.get("/api/aar/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["report_date"] == "2026-06-06"
    assert "content" in body
    assert body["content"] == "# AAR\nAll good."


def test_aar_latest_refuses_path_outside_reports_dir(
    client: TestClient, tmp_path: Path
) -> None:
    """A DB record whose stored path points outside ``reports_dir`` must come back
    with ``content: None``, not the file's actual contents.

    # F18: a stored AAR path escaping reports_dir must not be read back — defends
    # against a tampered record turning this endpoint into arbitrary file read.
    """
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("TOP SECRET", encoding="utf-8")
    store = server_mod.store
    assert store is not None
    store.add_aar_record("2026-06-07", str(outside), "summary")
    resp = client.get("/api/aar/latest")
    assert resp.status_code == 200
    assert resp.json()["content"] is None


def test_event_ingest_still_works(client: TestClient) -> None:
    """The baseline happy path for the hook ingest endpoint: a well-formed event is
    accepted and counted, guarding against a regression breaking normal ingestion
    while the malformed-input tests below focus on the edge cases.
    """
    resp = client.post(
        "/event", json={"phase": "pre", "tool_name": "Read", "tool_input": {"file_path": "x"}}
    )
    assert resp.json() == {"status": "ok"}
    assert client.get("/health").json()["stored_events"] == 1


def test_event_non_dict_tool_input_does_not_crash(client: TestClient) -> None:
    """``tool_input`` arriving as a non-mapping must not crash the endpoint.

    # F17: tool_input arriving as a non-mapping (string/list/int/bool) from the
    # untrusted hook must never 500 the endpoint.
    """
    for bad in ("just-a-string", ["a", "b"], 42, True):
        resp = client.post(
            "/event", json={"phase": "pre", "tool_name": "Read", "tool_input": bad}
        )
        assert resp.status_code == 200, f"tool_input={bad!r} -> {resp.status_code}"


def test_event_task_create_non_dict_tool_input(client: TestClient) -> None:
    """The TaskCreate/Update branch has its own ``.get`` access on ``tool_input`` and
    needs the same non-dict guard as the general path above.

    # F17: the TaskCreate/Update branch also assumed a dict (.get) and crashed.
    """
    resp = client.post("/event", json={"tool_name": "TaskCreate", "tool_input": "oops"})
    assert resp.status_code == 200


# -- security regression: TransitionError must not escape as 500 ---------------


def test_reexecute_on_resolved_issue_returns_failed_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW security finding (not in issue #16): when the executor's
    set_issue_status(issue_id, "executing") raises TransitionError because the
    issue is already resolved, the exception must be caught inside the executor
    and returned as ExecutionResult("failed", ...) rather than propagating out
    of POST /api/solutions/{id}/execute as an unhandled 500.

    This test deliberately does NOT use the shared dry-run ``client`` fixture:
    that fixture sets ``dry_run=True`` and a target repo with no ``.git``, so
    execute() short-circuits at the "no usable target repo" / dry-run branches
    (executor.py:157-158, 182-194) long BEFORE the try-block at line 200 where
    the illegal transition is attempted — i.e. it would pass even with the bug.

    To genuinely reach the failing path we build a LIVE-mode config
    (``dry_run=False``) with a usable target repo. execute() only checks that
    ``(repo / ".git")`` exists before the try-block, and the illegal transition
    raises BEFORE any git command runs, so a bare ``.git`` dir is enough — the
    test stays fast and spawns nothing.

    Root cause: executor.py's except clause previously caught only
    (RuntimeError, OSError), not TransitionError (a ValueError subclass).
    Fix: added TransitionError to the catch in Executor.execute(). With the
    fix reverted, TestClient re-raises the TransitionError (red); with the fix,
    the endpoint returns 200 with a structured "failed" result (green).
    """
    import overwatch.solution.executor as executor_mod

    # --- Arrange ---
    # A usable target repo: execute() only needs (repo / ".git") to exist to get
    # past the repo pre-check; the illegal transition raises before `git worktree`.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    cfg = Config(
        targets=(Target(name="proj", repo=repo),),
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        dry_run=False,  # LIVE mode: required to fall through to the try-block
    )
    store = Store(cfg.db_path)
    monkeypatch.setattr(server_mod, "config", cfg)
    monkeypatch.setattr(server_mod, "store", store)
    # claude must look present, else execute() fails closed before the try-block.
    monkeypatch.setattr(executor_mod.shutil, "which", lambda _: "C:/fake/claude.exe")
    client = TestClient(server_mod.app)

    # Seed an issue and drive it all the way to RESOLVED so the executor's
    # set_issue_status(issue_id, "executing") is an illegal transition.
    issue_id = store.upsert_issue(
        "fp-reexec", "log_scan", "low", "proj/job: resolved issue",
        {"target": "proj", "task": "job"},
    )
    sid = store.add_solution(
        issue_id, "## fix", "none", auto_eligible=True, kind="investigate-fix"
    )
    store.set_issue_status(issue_id, "drafted")
    store.set_issue_status(issue_id, "executing")
    store.set_issue_status(issue_id, "resolved")

    # --- Act ---
    # Re-execute the resolved issue: the executor reaches the try-block and
    # attempts resolved -> executing. Without the fix the TransitionError
    # propagates (TestClient re-raises -> a 500); with the fix it is caught and
    # returned as a structured "failed" result that names the illegal transition.
    resp = client.post(f"/api/solutions/{sid}/execute")

    # --- Assert ---
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["status"] == "failed"
    assert "illegal transition" in body["detail"]
    store.close()
