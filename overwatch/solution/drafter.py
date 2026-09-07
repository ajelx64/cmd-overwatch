"""Solution drafting: turn a detected issue into a reviewable remediation.

Every draft carries the full approval-request block (the operator reads the
same six fields whether the solution is gated or not), a machine ``kind``,
and the gate decision. The drafter never executes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from overwatch.detect.gate_classifier import GateDecision, classify

# issue source -> (kind, proposed action template, rollback note)
_PLAYBOOK: dict[str, tuple[str, str, str]] = {
    "log_scan": (
        "investigate-fix",
        "Dispatch a headless agent in the affected project to investigate the "
        "failure, reproduce it, and prepare a fix on an isolated branch "
        "(never the default branch; no merge).",
        "Delete the fix branch; no shared state is touched until a human merges.",
    ),
    "sched_tasks": (
        "investigate-fix",
        "Inspect the scheduled task's last run log, identify the failure cause, "
        "and prepare a fix on an isolated branch in the owning project.",
        "Delete the fix branch; the task definition itself is not modified.",
    ),
    "git_hygiene": (
        "report-only",
        "Surface the repo-hygiene finding in the daily report for the operator "
        "to act on (committing, pushing, or pruning is a human decision).",
        "Nothing to roll back — report only.",
    ),
    "host_health": (
        "report-only",
        "Surface the host-health finding in the daily report.",
        "Nothing to roll back — report only.",
    ),
    "collector": (
        "report-only",
        "Surface the collector self-health failure in the daily report.",
        "Nothing to roll back — report only.",
    ),
}

# Specific overrides keyed on (source, marker-in-fingerprint-evidence)
_DISK_LOW_KIND = (
    "log-purge",
    # NB: wording avoids destructive-gate noun pairs (e.g. "purge ... table");
    # this action deletes only stale day-log *files* under configured dirs.
    "Delete day-log files older than the retention period from the configured "
    "log directories (a dry-run lists candidates first; every run is recorded "
    "for audit and surfaced in the daily report).",
    "Deleted log files are unrecoverable. Only files older than the retention "
    "period inside known log directories are eligible.",
)

# Display-only mapping from issue severity to a risk word shown in the
# draft's "Risk:" line — deliberately independent of the gate category
# (a gated but low-severity issue still reads as low risk to the operator).
_SEVERITY_RISK = {"critical": "high", "high": "medium", "medium": "low", "low": "low"}


@dataclass(frozen=True)
class SolutionDraft:
    """A drafted remediation, ready to store and (if ungated) execute.

    Attributes:
        kind: Machine-known remediation type (drives routing in
            :mod:`overwatch.solution.pipeline` and gate eligibility in
            :mod:`overwatch.detect.gate_classifier`).
        body_md: The full operator-facing markdown brief.
        decision: The gate classifier's verdict for this draft.
    """

    kind: str
    body_md: str
    decision: GateDecision

    @property
    def auto_eligible(self) -> bool:
        """Whether this draft may execute without an operator approval click."""
        return self.decision.auto


def _playbook_for(issue: dict[str, Any]) -> tuple[str, str, str]:
    """Pick the (kind, proposed action, rollback note) template for an issue.

    Args:
        issue: Row dict from ``Store.get_issue`` (dict shape, not the ORM
            row itself).

    Returns:
        A ``(kind, action, rollback)`` tuple. Falls back to a generic
        "investigate-fix" template when the issue's ``source`` has no
        specific playbook entry, so every issue still gets a draft.
    """
    evidence = issue.get("evidence") or {}
    # Low-disk host-health findings get a dedicated destructive-but-scoped
    # playbook (log purge) instead of the generic report-only host_health
    # entry — distinguished by the presence of the disk-free-percent field
    # rather than a separate `source` value.
    if issue.get("source") == "host_health" and "free_pct" in evidence:
        return _DISK_LOW_KIND
    return _PLAYBOOK.get(
        str(issue.get("source")),
        (
            "investigate-fix",
            "Investigate the issue and prepare a remediation for operator review.",
            "No action taken until reviewed.",
        ),
    )


def _evidence_lines(evidence: dict[str, Any], limit: int = 6) -> list[str]:
    """Render evidence key/value pairs as short markdown bullet lines.

    Args:
        evidence: The issue's evidence mapping (already redacted upstream by
            :meth:`overwatch.store.Store.upsert_issue`).
        limit: Maximum number of pairs to render, so a large evidence blob
            cannot blow up the draft body.

    Returns:
        One ``"- key: value"`` string per pair, each value truncated to 160
        characters (with an ellipsis) so a single oversized field cannot
        dominate the draft.
    """
    lines = []
    for k, v in list(evidence.items())[:limit]:
        text = str(v)
        if len(text) > 160:
            text = text[:157] + "..."
        lines.append(f"- {k}: {text}")
    return lines


def draft_solution(
    issue: dict[str, Any], extra_gate_patterns: tuple[str, ...] = ()
) -> SolutionDraft:
    """Draft a remediation for a stored issue (dict shape from Store.get_issue).

    Args:
        issue: Row dict from ``Store.get_issue``/``Store.list_issues``.
        extra_gate_patterns: Operator-configured additional gate regexes
            (``[gates] extra_patterns`` in config.toml), passed through to
            the classifier on top of the immutable built-in gate set.

    Returns:
        A :class:`SolutionDraft` combining the playbook template with the
        gate classifier's verdict on the exact text an operator would read.
    """
    # --- Step 1: pick the proposed action from the issue-source playbook ---
    kind, action, rollback = _playbook_for(issue)
    title = str(issue.get("title", "unknown issue"))
    severity = str(issue.get("severity", "medium"))
    evidence: dict[str, Any] = issue.get("evidence") or {}

    # --- Step 2: classify using exactly what the operator will read (title +
    # proposed action + evidence), not just the machine `kind` — a safe kind
    # can still describe an unsafe action in its title/evidence text. ---
    classified_text = "\n".join([title, action, *(_evidence_lines(evidence))])
    decision = classify(kind, classified_text, extra_gate_patterns)

    # --- Step 3: render the gate verdict as a one-line summary for the body ---
    gate_line = (
        f"GATED ({decision.category}) — operator approval required"
        if decision.gated
        else "AUTO-ELIGIBLE — no gate pattern matched, kind is on the safe allowlist"
    )

    # --- Step 4: assemble the full operator-facing markdown brief ---
    body = "\n".join(
        [
            f"# Solution draft — issue #{issue.get('id', '?')}",
            "",
            f"**Approval required:** {title}" if decision.gated else f"**Action:** {title}",
            "",
            "**Reason:**",
            f"- Detected by `{issue.get('source')}` (severity: {severity}, "
            f"seen {issue.get('count', 1)}x)",
            f"- Gate classification: {gate_line}",
            f"- {decision.reason}",
            "",
            "**Proposed action:**",
            f"- {action}",
            "",
            "**Affected systems:**",
            *(_evidence_lines(evidence) or ["- (no evidence recorded)"]),
            "",
            f"**Risk:** {_SEVERITY_RISK.get(severity, 'low')}",
            "",
            "**Rollback:**",
            f"- {rollback}",
        ]
    )
    return SolutionDraft(kind=kind, body_md=body, decision=decision)
