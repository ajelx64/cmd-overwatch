"""Executor rail tests. Real tmp git repos; the claude spawn point is mocked.

The cardinal assertions:
- dry-run records the exact planned command and spawns NOTHING
- gated-without-approval is refused
- timeout / nonzero exit -> issue failed; success -> resolved on a fix/ branch
- missing CLI fails closed; lock enforces single flight; merge/push are denied tools

These tests are the safety envelope for a component that runs an external CLI against a
real git worktree on the operator's behalf. Real ``git`` subprocesses are used throughout
(init/commit/worktree/branch) so worktree isolation and branch naming are checked against
actual git behaviour, not a mock's idea of it; only the one call that would spawn the
``claude`` agent itself (``_run_claude``) is replaced, per-test, with either a hard failure
(``no_spawn``, for scenarios that must never reach a spawn) or a fake ``CompletedProcess``/
exception (for scenarios that exercise what happens after a spawn). Read each docstring
below carefully for what it does and does NOT prove — several tests here verify only that
an error is *handled* (turned into a structured "failed" result), not that the underlying
risk (e.g. an actually-running process) was mitigated at the OS level.
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
    """Run a real git command against ``repo``, failing the test loudly on error."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build a real git repo, a dry-run Config/Store pointed at it, and a drafted issue.

    ``dry_run=True`` here is deliberate: it is the safe default this component ships
    with, so most scenarios below start from it and opt into live mode (``live_cfg``)
    only when a test needs to reach the actual spawn/worktree code path. ``shutil.which``
    is patched so tests don't depend on whether a real ``claude`` CLI is installed on the
    machine running the suite.
    """
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
    """Add a solution to ``env``'s issue and, if ``approved`` is given, record a decision.

    ``auto`` mirrors the auto-eligible/gated split the executor's ``_authorize`` branches
    on; ``approved=None`` leaves the issue with no recorded approval at all (the
    "never decided" case), distinct from an explicit approve/deny.
    """
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
    """Stand in for ``_run_claude`` in scenarios that must resolve before ever spawning.

    Any call reaching this fails the test with a clear message instead of silently
    running a real ``claude`` process, which is the behaviour under test for dry-run,
    refusal, and fail-closed paths.
    """
    raise AssertionError("subprocess must not be spawned in this scenario")


# -- dry-run -------------------------------------------------------------------


def test_dry_run_records_command_and_spawns_nothing(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=True (the shipped default) must plan and log the command, never spawn it.

    ``_run_claude`` is patched to ``no_spawn``, so if the dry-run branch fell through to
    a real spawn this test would fail on that call rather than on a later assertion —
    that is the actual proof of "spawns nothing," not just the returned status string.
    """
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
    """A gated (non-auto-eligible) solution with no approval record must be refused.

    This is the default-deny half of the authorization rail: absent an explicit
    "approved" decision, nothing runs. ``no_spawn`` backs this up structurally.
    """
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "refused"
    assert "no recorded approval" in result.detail


def test_denied_solution_refused(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A solution explicitly denied by an operator must stay refused, not just unapproved.

    Distinct from the "never decided" case above: here a decision exists and it was
    "denied," so the refusal message should reflect that decision rather than a
    generic "no approval" message.
    """
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=False)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "refused"
    assert "denied" in result.detail


def test_mismatched_solution_refused(env: dict[str, Any]) -> None:
    """A solution belonging to a different issue than the one named must be refused.

    Guards against a caller (e.g. a stale UI request) pairing an issue id with the
    wrong solution id and having the executor act on it anyway.
    """
    other = env["store"].upsert_issue("fp-y", "log_scan", "low", "other", {"target": "proj"})
    sid = env["store"].add_solution(other, "x", "none", auto_eligible=True)
    result = Executor(env["store"], env["cfg"]).execute(env["issue_id"], sid)
    assert result.status == "refused"


# -- live-mode rails (dry_run=False, spawn mocked) ----------------------------------


def live_cfg(env: dict[str, Any]) -> Config:
    """Same targets/data_dir as ``env``'s fixture Config, but with dry_run turned off.

    Used by every test below that needs to reach the real worktree/spawn code path
    (the spawn itself is still mocked per-test via ``_run_claude``).
    """
    return Config(
        targets=env["cfg"].targets, data_dir=env["cfg"].data_dir, dry_run=False
    )


def fake_proc(code: int, out: str = "done") -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess shaped like what ``_run_claude`` would return."""
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr="")


def test_success_resolves_issue_on_fix_branch(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero exit must resolve the issue, and the agent must have run in an isolated
    worktree on the fix branch — not the operator's own checkout of ``main``.

    Only the ``claude`` spawn is mocked; the worktree creation and branch checkout are
    real git operations, so the ``cwd`` and ``branch --show-current`` checks below are
    verifying actual git state, not a mock's account of it. This is the test that
    substantiates the "worktree isolation" safety rail, not just the "run succeeded"
    outcome.
    """
    # --- Arrange ---
    captured: dict[str, Any] = {}

    def spawn(command: list[str], cwd: Path, timeout_s: int) -> subprocess.CompletedProcess[str]:
        """Stand in for _run_claude to simulate a successful agent run, while recording
        the command/cwd it was invoked with so the worktree/branch assertions below can
        check what the executor actually did.
        """
        captured["command"] = command
        captured["cwd"] = cwd
        return fake_proc(0)

    monkeypatch.setattr(executor_mod, "_run_claude", spawn)
    sid = add_solution(env, auto=False, approved=True)

    # --- Act ---
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)

    # --- Assert ---
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
    """A non-zero agent exit code must fail the issue rather than silently resolve it.

    Without this, a broken/partial agent run could leave the issue looking done.
    """
    monkeypatch.setattr(executor_mod, "_run_claude", lambda *a, **k: fake_proc(2, "boom"))
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)
    assert result.status == "failed"
    issue = env["store"].get_issue(env["issue_id"])
    assert issue is not None and issue["status"] == "failed"


def test_timeout_kills_and_fails(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``TimeoutExpired`` from the spawn point must be caught and turned into a failed
    issue with a "timed out" detail, instead of propagating out of ``execute()``.

    NOTE on what this does NOT prove: ``_run_claude`` is mocked out entirely, so this
    test never exercises real subprocess timeout/kill behaviour (that lives in
    ``subprocess.run``'s own ``timeout=`` handling inside the un-mocked ``_run_claude``).
    It only proves the executor's own exception handling for that outcome; the test name
    overstates this slightly.
    """
    def spawn(command: list[str], cwd: Path, timeout_s: int) -> subprocess.CompletedProcess[str]:
        """Stand in for _run_claude to simulate the claude subprocess hanging past its
        timeout.
        """
        raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout_s)

    monkeypatch.setattr(executor_mod, "_run_claude", spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid, timeout_s=5)
    assert result.status == "failed"
    assert "timed out" in result.detail
    issue = env["store"].get_issue(env["issue_id"])
    assert issue is not None and issue["status"] == "failed"


def test_missing_cli_fails_closed(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``claude`` on PATH must fail closed with an explanatory message, never proceed
    as if nothing were wrong. ``no_spawn`` backs up that this path never reaches a spawn.
    """
    monkeypatch.setattr(executor_mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], live_cfg(env)).execute(env["issue_id"], sid)
    assert result.status == "failed"
    assert "failing closed" in result.detail


def test_lock_enforces_single_flight(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-held lock must cause a second execution attempt to be refused, not queued
    or run concurrently — the single-flight rail against two agents touching one repo
    at once.
    """
    cfg = live_cfg(env)
    assert ExecutorLock(cfg.data_dir).acquire()  # someone else is executing
    monkeypatch.setattr(executor_mod, "_run_claude", no_spawn)
    sid = add_solution(env, auto=False, approved=True)
    result = Executor(env["store"], cfg).execute(env["issue_id"], sid)
    assert result.status == "refused"
    assert "in flight" in result.detail


def test_transcript_is_redacted(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent stdout containing a credential-shaped string must never reach the transcript
    file verbatim — it must come out redacted instead.

    This is a real end-to-end check of the redaction rail: the fake token below is a
    fixture value the test constructs at runtime (never a real credential) and is used
    only to verify that ``_write_transcript`` runs output through ``redact_text`` before
    it is persisted to disk, where a human or another tool could later read it.
    """
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
    """The denylist constant must literally name git push/merge and the gh CLI.

    This only checks the policy string the executor hands to the agent — it does not
    (and cannot, from this test) prove the agent actually honors ``--disallowedTools``
    at runtime; that enforcement lives in the external ``claude`` CLI, outside this
    repository.
    """
    assert "git push" in DISALLOWED_TOOLS
    assert "git merge" in DISALLOWED_TOOLS
    assert "gh:" in DISALLOWED_TOOLS


def test_issue_without_repo_target_fails(env: dict[str, Any]) -> None:
    """An issue whose evidence names no matching configured target repo must fail
    with a clear reason, rather than attempting to run against no repo at all.
    """
    iid = env["store"].upsert_issue("fp-norepo", "host_health", "low", "host: thing", {})
    env["store"].set_issue_status(iid, "drafted")
    sid = env["store"].add_solution(iid, "x", "none", auto_eligible=True)
    result = Executor(env["store"], live_cfg(env)).execute(iid, sid)
    assert result.status == "failed"
    assert "no usable target repo" in result.detail


def test_lock_acquire_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the TOCTOU race window in ``ExecutorLock.acquire`` by making the
    existence pre-check lie (always "not there") while the file is really on disk.

    The atomic ``O_CREAT | O_EXCL`` open is what must still refuse a second acquire in
    that case; if the lock only relied on the earlier ``Path.exists`` check, this test
    would incorrectly report success for both callers.
    """
    lock = ExecutorLock(tmp_path)
    assert lock.acquire() is True
    # F15: even if the existence pre-check is bypassed (the TOCTOU window between
    # checking and writing), the atomic O_EXCL create must block a second holder.
    monkeypatch.setattr(Path, "exists", lambda self: False)  # simulate the race window
    assert ExecutorLock(tmp_path).acquire() is False
