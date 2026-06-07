"""Drafter tests: playbook routing, approval-block rendering, gate wiring."""

from typing import Any

from overwatch.solution.drafter import draft_solution

APPROVAL_FIELDS = ("**Reason:**", "**Proposed action:**", "**Affected systems:**",
                   "**Risk:**", "**Rollback:**")


def issue(**overrides: Any) -> dict[str, Any]:
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
    draft = draft_solution(issue())
    assert draft.kind == "investigate-fix"
    assert draft.decision.gated  # free-form fixes default to gated
    assert not draft.auto_eligible
    assert "**Approval required:**" in draft.body_md
    for field in APPROVAL_FIELDS:
        assert field in draft.body_md


def test_disk_low_issue_drafts_auto_log_purge() -> None:
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
    draft = draft_solution(issue(source="mystery", title="something odd"))
    assert draft.kind == "investigate-fix"
    assert draft.decision.gated


def test_extra_gate_patterns_flow_through() -> None:
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
    draft = draft_solution(issue(evidence={"tail": "x" * 500}))
    assert "..." in draft.body_md
    assert "x" * 200 not in draft.body_md
