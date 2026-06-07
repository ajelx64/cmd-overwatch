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

    Returns ``(issue_id, solution_id, gated)`` tuples. Re-opened issues that
    already have an undecided solution are left alone.
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
    """Route an authorized solution to its runner by kind."""
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
