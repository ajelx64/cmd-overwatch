"""Tests for ``overwatch.detect.gate_classifier`` — the auto-vs-approval gate.

Covers: every built-in gate category fires on a representative trigger phrase,
a gate match always overrides an allowlisted "safe kind" (the never-AUTO
guarantee that is this module's whole reason to exist), unknown or free-form
remediation kinds default to gated, operator-configured extra patterns can
only add gates (never remove built-ins), and the auth-network pattern's
word-boundary behaviour (must not fire on the unrelated word "author").

No fixtures beyond ``CATEGORY_SAMPLES`` below — a plain dict of natural-
language sentences, one per category, standing in for text a real drafted
solution would contain.
"""

import pytest

from overwatch.detect.gate_classifier import BUILT_IN_GATES, SAFE_KINDS, classify

# One representative trigger phrase per built-in category, each annotated with
# the real-world action it exists to stop an agent from doing unattended.
CATEGORY_SAMPLES = {
    # Blocks anything with a direct financial consequence (charges, refunds,
    # price/plan changes) from executing without a human decision.
    "money": "update the subscription pricing for the storefront",
    # Blocks making content or a deploy publicly visible without review.
    "publishing": "deploy the landing page and publish the release notes",
    # Blocks unattended handling of customer/personal data (PII, GDPR scope).
    "customer-data": "export customer records for the migration",
    # Blocks acting on or exposing credentials, keys, tokens, or passwords.
    "secrets": "rotate the API key in the .env file",
    # Blocks landing changes on the protected branch or rewriting history.
    "main-merge": "merge the hotfix into main",
    # Blocks unrecoverable deletes (branches, tags, databases, backups, repos).
    "destructive": "delete the stale branches and drop the old database",
    # Blocks loosening auth or network exposure (e.g. binding to 0.0.0.0).
    "auth-network": "bind the server to 0.0.0.0 for remote access",
    # Blocks installing a persistent background service/task unattended.
    "service-install": "register a scheduled task to run nightly",
    # Blocks unattended edits to legally significant documents/posture.
    "legal": "update the privacy policy wording",
}


@pytest.mark.parametrize("category", sorted(BUILT_IN_GATES))
def test_every_builtin_category_has_a_positive_case(category: str) -> None:
    """Every built-in gate category has at least one phrase that trips it.

    Guards against a category's regex silently going dead (e.g. after an
    edit) while the category name still exists in BUILT_IN_GATES — that would
    let its whole class of risky action slip through ungated with nothing
    here to fail.
    """
    decision = classify("investigate-fix", CATEGORY_SAMPLES[category])
    assert decision.gated
    assert decision.category == category
    assert decision.matched


@pytest.mark.parametrize("kind", sorted(SAFE_KINDS))
@pytest.mark.parametrize("category", sorted(BUILT_IN_GATES))
def test_gate_keyword_always_wins_over_safe_kind(kind: str, category: str) -> None:
    """THE guard: a gate match can never yield AUTO, even for allowlisted kinds.

    Cross-product of every safe kind against every gate category: pins the
    precedence order in classify() (gate patterns checked before SAFE_KINDS).
    Without this, e.g. a "log-purge"-kind draft whose text mentions dropping a
    database could auto-run just because its kind is normally safe.
    """
    decision = classify(kind, CATEGORY_SAMPLES[category])
    assert decision.gated, f"{category!r} text must gate even with safe kind {kind!r}"


def test_safe_kind_clean_text_is_auto() -> None:
    """A safe-kind remediation with no gate-triggering text is auto-eligible.

    The baseline the never-AUTO guard above is tested against: without this
    passing, the SAFE_KINDS allowlist would be provably inert.
    """
    decision = classify("log-purge", "remove day-logs older than 30 days from the log directory")
    # NB: "remove ... logs" must not trip 'destructive' (that's branches/dbs/backups)
    assert decision.auto
    assert decision.category == "none"


def test_unknown_kind_defaults_to_gated() -> None:
    """A remediation kind absent from SAFE_KINDS defaults to gated ("uncertain").

    Guards the fail-safe default: a new remediation kind introduced later
    without being explicitly allowlisted must not start running unattended.
    """
    decision = classify("novel-remediation", "tidy up some files")
    assert decision.gated
    assert decision.category == "uncertain"


def test_free_text_fix_defaults_to_gated() -> None:
    """A free-form investigate-fix kind gates even when its text matches no
    built-in category — it is deliberately never in SAFE_KINDS (per the
    module docstring's "drafted code fix" example of a novel remediation).
    """
    decision = classify("investigate-fix", "fix the failing parser in the data pipeline")
    assert decision.gated
    assert decision.category == "uncertain"


def test_extra_patterns_extend_gates() -> None:
    """Operator-configured ``extra_patterns`` can add a working gate category,
    independent of the built-ins.
    """
    decision = classify("log-purge", "run terraform apply on the cluster", ("terraform",))
    assert decision.gated
    assert decision.category == "custom"


def test_extra_patterns_cannot_ungate() -> None:
    """Config can only add patterns; built-ins fire regardless of extras."""
    decision = classify("log-purge", "rotate the password", ("something-else",))
    assert decision.gated
    assert decision.category == "secrets"


def test_auth_does_not_match_author() -> None:
    """The auth-network regex must not fire on the unrelated word "author".

    Guards against a false-positive gate: matching is deliberately narrow
    (auth/authn/authz/authenticate*/authorize*) so ordinary report text
    naming a commit's author isn't mistaken for an auth-control change.
    """
    decision = classify("report-only", "surface the commit author in the report")
    assert decision.auto


def test_evidence_with_token_keyword_gates() -> None:
    """Secret-shaped vocabulary anywhere in the supplied text gates, not just
    in a title field.

    classify() is given the full haystack a real draft would contain (title
    plus evidence excerpts) — an error message merely quoting a bearer token
    must still trip the secrets gate.
    """
    decision = classify("task-restart", "restart task; last error: invalid bearer token")
    assert decision.gated
    assert decision.category == "secrets"
