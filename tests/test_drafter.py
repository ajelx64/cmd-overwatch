"""Tests for ``overwatch.solution.drafter``: playbook selection per issue
source, the disk-low override, approval-block rendering, and gate-decision
wiring through to the rendered markdown.

``issue()`` builds a minimal stored-issue dict (the shape draft_solution()
expects from Store.get_issue) with overridable fields, so each test states
only what differs from the default log_scan/high-severity baseline.
"""

from typing import Any

from overwatch.solution.drafter import draft_solution

# Five of the six fixed fields every draft must render, gated or not (see the
# module docstring); the sixth, the Approval-required/Action header, differs
# by gate outcome and is asserted separately per test below.
APPROVAL_FIELDS = ("**Reason:**", "**Proposed action:**", "**Affected systems:**",
                   "**Risk:**", "**Rollback:**")


def issue(**overrides: Any) -> dict[str, Any]:
    """Build a minimal stored-issue dict, overriding only what a test needs.

    Default source is log_scan/high so the baseline exercises the generic
    "investigate-fix" playbook path most tests aren't specifically about.
    """
    base: dict[str, Any] = {
        "id": 7,
        "source": "log_scan",
        "severity": "high",
        "title": "proj/sync-job: run failed with exit 1",
        "count": 3,
        "evidence": {"target": "proj", "task": "sync-job", "exit_code": 1},
    }
    base.update(overrides)
    return base


def test_log_scan_issue_drafts_investigate_fix_gated() -> None:
    """A log-scan issue drafts as investigate-fix and gates by default, since
    investigate-fix is never in SAFE_KINDS — and the full approval block
    still renders even though nothing else about this draft is unusual.
    """
    draft = draft_solution(issue())
    assert draft.kind == "investigate-fix"
    assert draft.decision.gated  # free-form fixes default to gated
    assert not draft.auto_eligible
    assert "**Approval required:**" in draft.body_md
    for field in APPROVAL_FIELDS:
        assert field in draft.body_md


def test_disk_low_issue_drafts_auto_log_purge() -> None:
    """A host_health issue carrying "free_pct" evidence is special-cased to
    the log-purge playbook (instead of the generic host_health report-only
    one), and log-purge is on SAFE_KINDS so it drafts auto-eligible.

    "free_pct" is the exact marker ``_playbook_for()`` checks for — this pins
    that the override actually fires rather than falling through to the
    generic host_health playbook.
    """
    draft = draft_solution(
        issue(
            source="host_health",
            severity="medium",
            title="host: disk free 12.0% (threshold 15%)",
            evidence={"free_pct": 12.0, "free_gb": 50.1, "total_gb": 500.0},
        )
    )
    assert draft.kind == "log-purge"
    assert draft.auto_eligible
    assert "**Action:**" in draft.body_md
    assert "**Approval required:**" not in draft.body_md


def test_git_hygiene_issue_is_report_only_auto() -> None:
    """A plain git-hygiene issue drafts report-only and auto-eligible —
    git-hygiene findings never route to an execute-capable playbook.
    """
    draft = draft_solution(
        issue(
            source="git_hygiene",
            severity="medium",
            title="proj: 2 commit(s) not on any remote",
            evidence={"target": "proj", "commits": ["abc fix", "def feat"]},
        )
    )
    assert draft.kind == "report-only"
    assert draft.auto_eligible


def test_secret_flavored_evidence_gates_even_report_only() -> None:
    """Even a report-only (SAFE_KINDS) draft gates when its evidence text
    itself looks secret-shaped (here, a .env-named file in a dirty-files list).

    Confirms the gate scan covers evidence, not just title/action text — a
    report-only kind is not a blanket exemption from the gate check.
    """
    draft = draft_solution(
        issue(
            source="git_hygiene",
            title="proj: 1 uncommitted change(s) idle for 30h",
            evidence={"dirty_files": [".env.production", "notes.md"]},
        )
    )
    assert draft.decision.gated
    assert draft.decision.category == "secrets"


def test_unknown_source_defaults_to_gated_investigation() -> None:
    """An issue source with no playbook entry falls back to the generic
    investigate-fix playbook, gated like any other free-form fix.
    """
    draft = draft_solution(issue(source="mystery", title="something odd"))
    assert draft.kind == "investigate-fix"
    assert draft.decision.gated


def test_extra_gate_patterns_flow_through() -> None:
    """``extra_gate_patterns`` passed to draft_solution() reach classify()
    and can gate text no built-in pattern would catch.
    """
    draft = draft_solution(
        issue(
            source="git_hygiene",
            title="proj: kubernetes manifest drift detected",
            evidence={},
        ),
        extra_gate_patterns=("kubernetes",),
    )
    assert draft.decision.gated
    assert draft.decision.category == "custom"


def test_long_evidence_values_truncated() -> None:
    """An oversized evidence value is truncated (with an ellipsis marker) in
    the rendered draft rather than dumping the full 500-char string.

    Keeps the approval-request block operator-readable regardless of how
    much raw evidence a collector attaches.
    """
    draft = draft_solution(issue(evidence={"tail": "x" * 500}))
    assert "..." in draft.body_md
    assert "x" * 200 not in draft.body_md
