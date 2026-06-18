"""Executor rail tests. Real tmp git repos; the claude spawn point is mocked.

The cardinal assertions:
- dry-run records the exact planned command and spawns NOTHING
- gated-without-approval is refused
- timeout / nonzero exit -> issue failed; success -> resolved on a fix/ branch
- missing CLI fails closed; lock enforces single flight; merge/push are denied tools
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

import overwatch.solution.executor as executor_mod
from overwatch.config import Config, Target
from overwatch.solution.executor import (
    DISALLOWED_TOOLS,
    Executor,
    ExecutorLock,
)
from overwatch.store import Store


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@example.invalid")
    _run(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "init")

    cfg = Config(
        targets=(Target(name="proj", repo=repo),),
        data_dir=tmp_path / "data",
        dry_run=True,
    )
    store = Store(cfg.db_path)
    issue_id = store.upsert_issue(
        "fp-x", "log_scan", "high", "proj/sync-job: run failed with exit 1",
        {"target": "proj", "task": "sync-job"},
    )
    store.set_issue_status(issue_id, "drafted")
    monkeypatch.setattr(executor_mod.shutil, "which", lambda _: "C:/fake/claude.exe")
    return {"cfg": cfg, "store": store, "issue_id": issue_id, "repo": repo}


def add_solution(env: dict[str, Any], auto: bool, approved: bool | None = None) -> int:
    sid = env["store"].add_solution(
        env["issue_id"], "## fix it", "uncertain" if not auto else "none", auto_eligible=auto
    )
    if approved is not None:
        env["store"].set_issue_status(env["issue_id"], "pending_approval")
        env["store"].record_approval(
            sid, env["issue_id"], "approved" if approved else "denied", "localhost-operator"
        )
    return sid


def no_spawn(*a: Any, **k: Any) -> None:
    raise AssertionError("subprocess must not be spawned in this scenario")


# -- dry-run -------------------------------------------------------------------


def test_dry_run_records_command_and_spawns_nothing(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "dry-run"
    assert result.command is not None
    assert result.command[1] == "-p"
    assert result.branch is not None and result.branch.startswith("fix/")
    assert result.transcript_path is not None and result.transcript_path.exists()
    text = result.transcript_path.read_text(encoding="utf-8")
    assert "DRY-RUN" in text and "claude" in text.lower()


# -- authorization ----------------------------------------------------------------


def test_gated_without_approval_refused(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "refused"
    assert "no recorded approval" in result.detail


def test_denied_solution_refused(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=False)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "refused"
    assert "denied" in result.detail


def test_mismatched_solution_refused(env: dict[str, Any]) -> None:
    other = env["store"].upsert_issue("fp-y", "log_scan", "low", "other", {"target": "proj"})
    sid = env["store"].add_solution(other, "x", "none", auto_eligible=True)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "refused"


# -- live-mode rails (dry_run=False, spawn mocked) ----------------------------------


def live_cfg(env: dict[str, Any]) -> Config:
    return Config(
        targets=env["cfg"].targets, data_dir=env["cfg"].data_dir, dry_run=False
    )


def fake_proc(code: int, out: str = "done") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def test_success_resolves_issue_on_fix_branch(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def spawn(command: list[str], cwd: Path, timeout_s: int) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = cwd
        return fake_proc(0)

    monkeypatch.setattr(executor_mod, "_run_claude", spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)
    assert result.status == "completed"
    issue = env["store"].get_issue(env["issue_id"])
    assert issue is not None and issue["status"] == "resolved"
    # executed inside the dedicated worktree, not the operator checkout
    assert "worktrees" in str(captured["cwd"])
    # branch in the worktree is the fix branch
    head = subprocess.run(
        ["git", "-C", str(captured["cwd"]), "branch", "--show-current"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert head.startswith("fix/")


def test_nonzero_exit_fails_issue(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_mod, "_run_claude", lambda *a, **k: fake_proc(2, "boom"))
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)
    assert result.status == "failed"
    issue = env["store"].get_issue(env["issue_id"])
    assert issue is not None and issue["status"] == "failed"


def test_timeout_kills_and_fails(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def spawn(command: list[str], cwd: Path, timeout_s: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout_s)

    monkeypatch.setattr(executor_mod, "_run_claude", spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid, timeout_s=5)
    assert result.status == "failed"
    assert "timed out" in result.detail
    issue = env["store"].get_issue(env["issue_id"])
    assert issue is not None and issue["status"] == "failed"


def test_missing_cli_fails_closed(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)
    assert result.status == "failed"
    assert "failing closed" in result.detail


def test_lock_enforces_single_flight(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = live_cfg(env)
    assert ExecutorLock(cfg.data_dir).acquire()  # someone else is executing
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], cfg).execute(env["issue_id"], sid)
    assert result.status == "refused"
    assert "in flight" in result.detail


def test_transcript_is_redacted(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    fake = "ghp_aaaabbbbccccddddeeeeffff000011112222"  # gitleaks:allow
    monkeypatch.setattr(
        executor_mod, "_run_claude", lambda *a, **k: fake_proc(0, f"pushed with {fake}")
    )
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)
    assert result.transcript_path is not None
    text = result.transcript_path.read_text(encoding="utf-8")
    assert fake not in text
    assert "[REDACTED:github-token]" in text


def test_merge_and_push_are_denied_tools() -> None:
    assert "git push" in DISALLOWED_TOOLS
    assert "git merge" in DISALLOWED_TOOLS
    assert "gh:" in DISALLOWED_TOOLS


def test_issue_without_repo_target_fails(env: dict[str, Any]) -> None:
    iid = env["store"].upsert_issue("fp-norepo", "host_health", "low", "host: thing", {})
    env["store"].set_issue_status(iid, "drafted")
    sid = env["store"].add_solution(iid, "x", "none", auto_eligible=True)
    result = Executor(env["store"], live_cfg(env)).execute(iid, sid)
    assert result.status == "failed"
    assert "no usable target repo" in result.detail


def test_lock_acquire_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # F15: even if the existence pre-check is bypassed (the TOCTOU window between
    # checking and writing), the atomic O_EXCL create must block a second holder.
    lock = ExecutorLock(tmp_path)
    assert lock.acquire() is True
    monkeypatch.setattr(Path, "exists", lambda self: False)  # simulate the race window
    assert ExecutorLock(tmp_path).acquire() is False
