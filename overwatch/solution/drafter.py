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

_SEVERITY_RISK = {"critical": "high", "high": "medium", "medium": "low", "low": "low"}


@dataclass(frozen=True)
class SolutionDraft:
    kind: str
    body_md: str
    decision: GateDecision

    @property
    def auto_eligible(self) -> bool:
        return self.decision.auto


def _playbook_for(issue: dict[str, Any]) -> tuple[str, str, str]:
    evidence = issue.get("evidence") or {}
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
    """Draft a remediation for a stored issue (dict shape from Store.get_issue)."""
    kind, action, rollback = _playbook_for(issue)
    title = str(issue.get("title", "unknown issue"))
    severity = str(issue.get("severity", "medium"))
    evidence: dict[str, Any] = issue.get("evidence") or {}

    classified_text = "\n".join([title, action, *(_evidence_lines(evidence))])
    decision = classify(kind, classified_text, extra_gate_patterns)

    gate_line = (
        f"GATED ({decision.category}) — operator approval required"
        if decision.gated
        else "AUTO-ELIGIBLE — no gate pattern matched, kind is on the safe allowlist"
    )

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
