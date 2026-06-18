"""Server API tests: health board, issues, the approve/deny write path,
double-decision rejection, and the loopback-binding invariant."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server as server_mod
from overwatch.config import Config, Target
from overwatch.store import Store


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
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
    seed_pending(client)
    board = client.get("/api/health-board").json()
    assert board["dry_run"] is True
    assert board["pending_approvals"] == 1
    assert board["active_by_severity"] == {"high": 1}
    assert board["tiles"][0]["target"] == "proj"
    assert board["tiles"][0]["worst_severity"] == "high"


def test_issue_listing_and_detail(client: TestClient) -> None:
    issue_id, sid = seed_pending(client)
    issues = client.get("/api/issues", params={"status": "pending_approval"}).json()
    assert [i["id"] for i in issues] == [issue_id]
    detail = client.get(f"/api/issues/{issue_id}").json()
    assert detail["issue"]["fingerprint"] == "fp-api"
    assert detail["solutions"][0]["id"] == sid
    assert detail["solutions"][0]["approval"] is None
    assert client.get("/api/issues/9999").status_code == 404


def test_pending_approvals_lists_undecided(client: TestClient) -> None:
    issue_id, sid = seed_pending(client)
    pending = client.get("/api/approvals/pending").json()
    assert len(pending) == 1
    assert pending[0]["issue"]["id"] == issue_id
    assert pending[0]["solution"]["id"] == sid


# -- approve / deny ----------------------------------------------------------------


def test_approve_dry_run_records_audit_and_plan(client: TestClient) -> None:
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
    _, sid = seed_pending(client)
    client.post(f"/api/approvals/{sid}/decision", json={"decision": "approved"})
    resp = client.post(f"/api/approvals/{sid}/decision", json={"decision": "denied"})
    assert resp.status_code == 409
    assert "already has a decision" in resp.json()["detail"]


def test_deny_returns_issue_to_open(client: TestClient) -> None:
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
    issue_id, sid = seed_pending(client)
    client.post(f"/api/approvals/{sid}/decision", json={"decision": "denied", "wontfix": True})
    store = server_mod.store
    assert store is not None
    issue = store.get_issue(issue_id)
    assert issue is not None and issue["status"] == "wontfix"


def test_decision_on_missing_solution_404(client: TestClient) -> None:
    resp = client.post("/api/approvals/999/decision", json={"decision": "approved"})
    assert resp.status_code == 404


def test_decision_on_non_pending_issue_409(client: TestClient) -> None:
    store = server_mod.store
    assert store is not None
    issue_id = store.upsert_issue("fp-open", "log_scan", "low", "x", {"target": "proj"})
    sid = store.add_solution(issue_id, "y", "none", auto_eligible=True)
    resp = client.post(f"/api/approvals/{sid}/decision", json={"decision": "approved"})
    assert resp.status_code == 409


def test_reexecute_refuses_unapproved_gated(client: TestClient) -> None:
    _, sid = seed_pending(client)
    resp = client.post(f"/api/solutions/{sid}/execute")
    assert resp.json()["status"] == "refused"


# -- invariants ----------------------------------------------------------------------


def test_default_binding_is_loopback() -> None:
    from overwatch.config import Config as Cfg

    assert Cfg().host == "127.0.0.1"


def test_aar_404_before_first_report(client: TestClient) -> None:
    assert client.get("/api/aar/latest").status_code == 404


def test_aar_latest_includes_content_field(client: TestClient) -> None:
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
    # F18: a stored AAR path escaping reports_dir must not be read back — defends
    # against a tampered record turning this endpoint into arbitrary file read.
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
    resp = client.post(
        "/event", json={"phase": "pre", "tool_name": "Read", "tool_input": {"file_path": "x"}}
    )
    assert resp.json() == {"status": "ok"}
    assert client.get("/health").json()["stored_events"] == 1


def test_event_non_dict_tool_input_does_not_crash(client: TestClient) -> None:
    # F17: tool_input arriving as a non-mapping (string/list/int/bool) from the
    # untrusted hook must never 500 the endpoint.
    for bad in ("just-a-string", ["a", "b"], 42, True):
        resp = client.post(
            "/event", json={"phase": "pre", "tool_name": "Read", "tool_input": bad}
        )
        assert resp.status_code == 200, f"tool_input={bad!r} -> {resp.status_code}"


def test_event_task_create_non_dict_tool_input(client: TestClient) -> None:
    # F17: the TaskCreate/Update branch also assumed a dict (.get) and crashed.
    resp = client.post("/event", json={"tool_name": "TaskCreate", "tool_input": "oops"})
    assert resp.status_code == 200
