"""Gate classifier tests — including the structural never-AUTO guard."""

import pytest

from overwatch.detect.gate_classifier import BUILT_IN_GATES, SAFE_KINDS, classify

# One representative trigger phrase per built-in category.
CATEGORY_SAMPLES = {
    "money": "update the subscription pricing for the storefront",
    "publishing": "deploy the landing page and publish the release notes",
    "customer-data": "export customer records for the migration",
    "secrets": "rotate the API key in the .env file",
    "main-merge": "merge the hotfix into main",
    "destructive": "delete the stale branches and drop the old database",
    "auth-network": "bind the server to 0.0.0.0 for remote access",
    "service-install": "register a scheduled task to run nightly",
    "legal": "update the privacy policy wording",
}


@pytest.mark.parametrize("category", sorted(BUILT_IN_GATES))
def test_every_builtin_category_has_a_positive_case(category: str) -> None:
    decision = classify("investigate-fix", CATEGORY_SAMPLES[category])
    assert decision.gated
    assert decision.category == category
    assert decision.matched


@pytest.mark.parametrize("kind", sorted(SAFE_KINDS))
@pytest.mark.parametrize("category", sorted(BUILT_IN_GATES))
def test_gate_keyword_always_wins_over_safe_kind(kind: str, category: str) -> None:
    """THE guard: a gate match can never yield AUTO, even for allowlisted kinds."""
    decision = classify(kind, CATEGORY_SAMPLES[category])
    assert decision.gated, f"{category!r} text must gate even with safe kind {kind!r}"


def test_safe_kind_clean_text_is_auto() -> None:
    decision = classify("log-purge", "remove day-logs older than 30 days from the log directory")
    # NB: "remove ... logs" must not trip 'destructive' (that's branches/dbs/backups)
    assert decision.auto
    assert decision.category == "none"


def test_unknown_kind_defaults_to_gated() -> None:
    decision = classify("novel-remediation", "tidy up some files")
    assert decision.gated
    assert decision.category == "uncertain"


def test_free_text_fix_defaults_to_gated() -> None:
    decision = classify("investigate-fix", "fix the failing parser in the data pipeline")
    assert decision.gated
    assert decision.category == "uncertain"


def test_extra_patterns_extend_gates() -> None:
    decision = classify("log-purge", "run terraform apply on the cluster", ("terraform",))
    assert decision.gated
    assert decision.category == "custom"


def test_extra_patterns_cannot_ungate() -> None:
    """Config can only add patterns; built-ins fire regardless of extras."""
    decision = classify("log-purge", "rotate the password", ("something-else",))
    assert decision.gated
    assert decision.category == "secrets"


def test_auth_does_not_match_author() -> None:
    decision = classify("report-only", "surface the commit author in the report")
    assert decision.auto


def test_evidence_with_token_keyword_gates() -> None:
    decision = classify("task-restart", "restart task; last error: invalid bearer token")
    assert decision.gated
    assert decision.category == "secrets"
