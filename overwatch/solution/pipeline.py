"""Issue → solution pipeline: draft, classify, route.

Called by the collector after each detection pass and by the server when an
approval lands. Routing by gate decision and kind:

- gated drafts        -> ``pending_approval`` (the dashboard queue)
- auto ``log-purge``  -> the scripted purge runner (wired in once it exists)
- auto ``report-only``-> nothing to execute; the issue surfaces in the AAR
- anything approved   -> the headless executor (which re-checks authorization)
"""

from __future__ import annotations

from typing import Any

from overwatch.config import Config
from overwatch.solution.drafter import draft_solution
from overwatch.solution.executor import ExecutionResult, Executor
from overwatch.store import Store


def draft_new_issues(store: Store, cfg: Config) -> list[tuple[int, int, bool]]:
    """Draft a solution for every open issue that has none yet.

    A single flat pass over currently-open issues — each iteration reads,
    drafts, writes, and moves on; there is no multi-phase structure to
    narrate with step banners.

    Args:
        store: Shared persistence layer.
        cfg: Loaded configuration; supplies operator-added gate patterns.

    Returns:
        ``(issue_id, solution_id, gated)`` tuples, one per newly drafted
        solution. Re-opened issues that already have an undecided solution
        are left alone (``store.solutions_for_issue`` is non-empty for them).
    """
    results: list[tuple[int, int, bool]] = []
    for issue in store.list_issues(status="open"):
        if store.solutions_for_issue(issue["id"]):
            continue
        draft = draft_solution(issue, cfg.extra_gate_patterns)
        sid = store.add_solution(
            issue["id"],
            draft.body_md,
            draft.decision.category,
            draft.auto_eligible,
            kind=draft.kind,
        )
        store.set_issue_status(issue["id"], "drafted")
        if draft.decision.gated:
            store.set_issue_status(issue["id"], "pending_approval")
        results.append((issue["id"], sid, draft.decision.gated))
    return results


def dispatch_solution(
    store: Store, cfg: Config, solution: dict[str, Any]
) -> ExecutionResult:
    """Route an authorized solution to its runner by kind.

    A single flat dispatch table keyed on ``kind`` — no multi-phase logic,
    so no step banners. Callers (the approval endpoint and the manual
    re-execute endpoint) are expected to have already recorded any required
    approval; ``Executor.execute`` re-checks authorization itself regardless.

    Args:
        store: Shared persistence layer, passed through to the executor.
        cfg: Loaded configuration, passed through to the executor.
        solution: Row dict from ``Store.get_solution``.

    Returns:
        The runner's :class:`ExecutionResult`. ``"report-only"`` solutions
        complete immediately with nothing to execute; ``"log-purge"``
        currently refuses (runner not implemented yet, and deliberately not
        routed to the git-worktree executor, which cannot act on a
        non-repo target); anything else goes to the headless :class:`Executor`.
    """
    kind = solution.get("kind", "investigate-fix")
    if kind == "report-only":
        return ExecutionResult(
            "completed", "report-only: surfaced in the daily report; nothing to execute"
        )
    if kind == "log-purge":
        # Scripted purge runner lands in a later phase; never fall through to
        # the headless executor for a non-repo action.
        return ExecutionResult(
            "refused", "log-purge runner not yet wired; deferring to a later release"
        )
    return Executor(store, cfg).execute(solution["issue_id"], solution["id"])
