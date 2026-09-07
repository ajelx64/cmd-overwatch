"""Redaction tests. All secrets here are synthetic fixtures, never real.

Covers ``overwatch.redact``: ``redact_text`` (the per-pattern substitution
pipeline) and ``redact_value`` (its recursive extension over dicts/lists/
tuples, plus the depth guard against hostile deeply-nested payloads). This
module is the boundary that keeps real credentials out of the store and the
dashboard, so treat every test's assertions as the literal ceiling of proven
coverage: a test that only checks a value is *absent* from the output proves
redaction happened, but not which pattern/label did it, and a passing test
for one scheme/prefix never implies siblings it doesn't exercise are covered.

Runtime-fixture convention: most secret-shaped values here are module-level
constants (fine, since ``# gitleaks:allow`` marks them as known-fake to the
scanner), but the modern Slack token test below assembles its token string
at call time from short pieces instead of writing it as one literal. That is
deliberate: this repository is public, and GitHub push protection plus a
gitleaks CI job scan literal secret-shaped strings in tracked source, `#
gitleaks:allow` or not, and a well-formed-looking token is exactly what those
scanners key on. Building it at runtime keeps no such literal in the diff.
"""

from typing import Any

import pytest

from overwatch.redact import redact_text, redact_value

FAKE_ANTHROPIC = "sk-ant-api03-aaaabbbbccccddddeeeeffff0000111122223333"  # gitleaks:allow
FAKE_GITHUB = "ghp_aaaabbbbccccddddeeeeffff000011112222"  # gitleaks:allow
FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"  # gitleaks:allow
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P"  # gitleaks:allow  # noqa: E501
FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


def test_anthropic_key_redacted() -> None:
    """Proves an ``sk-ant-...`` API key embedded mid-sentence is removed and
    labeled ``anthropic-key`` -- the strongest form of proof here (both the
    literal absence and the specific label are checked)."""
    out = redact_text(f"calling api with {FAKE_ANTHROPIC} now")
    assert FAKE_ANTHROPIC not in out
    assert "[REDACTED:anthropic-key]" in out


def test_github_token_redacted() -> None:
    """Proves a ``ghp_...`` token embedded in a bare ``user@host`` URL
    segment (no password separator, so the uri-credentials pattern does not
    apply here) is matched and labeled by the github-token pattern."""
    out = redact_text(f"git remote set-url https://{FAKE_GITHUB}@github.com/x/y")
    assert FAKE_GITHUB not in out
    assert "[REDACTED:github-token]" in out


def test_aws_key_redacted() -> None:
    """Proves an AKIA-prefixed AWS access key id is removed from the output.

    Only checks absence of the literal, not the ``[REDACTED:aws-access-key]``
    label -- so this test alone does not pin down which pattern matched it.
    """
    out = redact_text(f"export AWS_ACCESS_KEY_ID={FAKE_AWS}")
    assert FAKE_AWS not in out


def test_jwt_redacted() -> None:
    """Proves a three-segment ``eyJ...``-prefixed JWT is removed and
    labeled ``jwt``."""
    out = redact_text(f"Authorization header was {FAKE_JWT}")
    assert FAKE_JWT not in out
    assert "[REDACTED:jwt]" in out


def test_pem_block_redacted() -> None:
    """Proves a ``-----BEGIN ... PRIVATE KEY-----`` block's body content is
    removed and the whole block is labeled ``private-key`` -- checks the
    inner base64 line specifically, not just the header/footer markers."""
    out = redact_text(f"file contents:\n{FAKE_PEM}\ndone")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "[REDACTED:private-key]" in out


def test_bearer_redacted() -> None:
    """Proves the value following an ``Authorization: Bearer`` header is
    removed. Does not check for the ``bearer`` label, so this alone does not
    prove the bearer pattern (rather than some other pattern) matched it.
    """
    out = redact_text("curl -H 'Authorization: Bearer abc123def456ghi789jkl'")  # gitleaks:allow
    assert "abc123def456ghi789jkl" not in out


def test_password_assignment_keeps_key_redacts_value() -> None:
    """Proves a ``KEY=value`` assignment redacts only the value, keeps the
    key name visible (so an operator can still tell *which* secret leaked),
    and is labeled ``assignment``."""
    out = redact_text("DB_PASSWORD=hunter2secret")
    assert "hunter2secret" not in out
    assert "DB_PASSWORD" in out
    assert "[REDACTED:assignment]" in out


def test_api_key_colon_assignment() -> None:
    """Proves the ``key: value`` colon-separated assignment form (not just
    ``key=value``) also redacts the value and keeps the key. Does not check
    for the ``[REDACTED:assignment]`` label text itself."""
    out = redact_text("api_key: super-secret-value-9000")
    assert "super-secret-value-9000" not in out
    assert "api_key" in out


def test_discord_webhook_redacted() -> None:
    """Proves a full ``discord.com/api/webhooks/...`` URL is redacted whole
    and labeled ``discord-webhook``."""
    url = "https://discord.com/api/webhooks/1234567890/AbCdEfGh-secret_part"
    out = redact_text(f"posting to {url}")
    assert url not in out
    assert "[REDACTED:discord-webhook]" in out


def test_plain_text_untouched() -> None:
    """Negative-coverage test: ordinary text with no secret-shaped substring
    must pass through byte-for-byte unchanged -- proves no pattern here is
    trigger-happy enough to mangle a Windows path or a sentence about tests."""
    s = "Read file C:/projects/example/main.py and ran 12 tests"
    assert redact_text(s) == s


def test_idempotent() -> None:
    """Proves running redact_text on already-redacted output is a no-op --
    the ``[REDACTED:...]`` placeholder itself must never be re-matched and
    mangled by a second pass (events can pass through redaction more than
    once as they move between store methods)."""
    once = redact_text(f"key {FAKE_ANTHROPIC}")
    assert redact_text(once) == once


def test_redact_value_recurses_containers() -> None:
    """Proves redact_value walks into a nested dict containing a further
    dict, and a list containing both a plain string and a nested dict,
    redacting a distinct secret shape (github token, AWS key, anthropic key)
    at each depth, while non-string scalar values (count, ok) are left
    exactly as-is rather than stringified or dropped."""
    payload = {
        "tool_input": {"command": f"deploy --token {FAKE_GITHUB}"},
        "items": [f"a {FAKE_AWS} b", {"deep": FAKE_ANTHROPIC}],
        "count": 7,
        "ok": True,
    }
    clean = redact_value(payload)
    flat = repr(clean)
    assert FAKE_GITHUB not in flat
    assert FAKE_AWS not in flat
    assert FAKE_ANTHROPIC not in flat
    assert clean["count"] == 7
    assert clean["ok"] is True


def test_non_string_scalars_pass_through() -> None:
    """Proves int/None/float values passed directly to redact_value (not
    nested in a container) are returned unchanged."""
    assert redact_value(42) == 42
    assert redact_value(None) is None
    assert redact_value(3.14) == 3.14


# -- audit regression: redaction gaps (F1, F2, F4) and recursion bound (F16) ----


def test_assignment_value_with_separators_fully_redacted() -> None:
    """Proves an unquoted assignment value containing commas is redacted in
    full -- both segments after the first comma are checked absent, so a
    regression that truncates the match at the first separator would fail
    this test even though a naive "is the prefix gone" check would pass."""
    # F1: a value containing commas must be redacted whole, not truncated at the
    # first separator (which previously left the tail in cleartext).
    out = redact_text("password=a1b2c3d4,e5f6g7h8,i9j0k1l2")
    assert "e5f6g7h8" not in out
    assert "i9j0k1l2" not in out
    assert "[REDACTED:assignment]" in out


def test_quoted_assignment_value_with_spaces_redacted() -> None:
    """Proves a double-quoted assignment value containing internal spaces
    is redacted in full, including the words after the first space --
    the unquoted-value alternative in the pattern stops at whitespace, so
    this specifically exercises the quoted-span alternative instead."""
    # F1: a quoted value with internal spaces must be fully redacted.
    out = redact_text('client_secret = "abcd efgh ijkl mnop"')
    assert "efgh" not in out
    assert "mnop" not in out
    assert "[REDACTED:assignment]" in out


def test_connection_string_password_redacted() -> None:
    """Proves the inline password segment of a ``scheme://user:password@host``
    URI is redacted while the scheme and host remain visible -- the
    uri-credentials pattern replaces only the password group, by design, so
    an operator can still see which service the credential belonged to."""
    # F2: scheme://user:password@host — redact the password, keep scheme/host.
    out = redact_text("DATABASE_URL=postgres://appuser:s3cr3tPassw0rd@db.internal:5432/app")
    assert "s3cr3tPassw0rd" not in out
    assert "[REDACTED:uri-credentials]" in out
    assert "postgres://" in out
    assert "db.internal" in out


def test_mongodb_uri_password_redacted() -> None:
    """Proves the same uri-credentials redaction applies to a ``mongodb://``
    URI, not only ``postgres://`` -- the pattern matches any URI scheme
    shape, not a hardcoded list. Only checks the password's absence, not
    the label or that scheme/host survive (those are covered by the
    postgres case above)."""
    # F2: a second scheme to prove the rule is not postgres-specific.
    out = redact_text("mongodb://admin:TopSecretValue9000@cluster0.example.net/prod")
    assert "TopSecretValue9000" not in out


@pytest.mark.parametrize("prefix", ["xapp-1", "xoxe-1", "xoxc-2"])
def test_modern_slack_token_prefixes_redacted(prefix: str) -> None:
    """Proves each of the app-level (xapp-) and refresh/config (xoxe-/xoxc-)
    Slack token prefixes -- not just the older xox[baprs]- forms -- is
    matched and labeled ``slack-token``. See the module docstring for why
    the token is built from string pieces here instead of one literal."""
    # F4: xapp-/xoxe-/xoxc- carry real privilege and were previously missed.
    # Tokens are assembled at runtime so the source holds no literal secret
    # (keeps GitHub push protection / secret scanners from flagging the fixture).
    token = f"{prefix}-{'ab12cd34ef' * 3}"
    out = redact_text(f"posting via {token}")
    assert token not in out
    assert "[REDACTED:slack-token]" in out


def test_redact_value_bounds_deep_recursion() -> None:
    """Proves redact_value returns (does not raise RecursionError) on a
    3000-level-deep nested dict, which exceeds the interpreter's default
    recursion limit. Only checks the call completes and returns a non-None
    value -- it does not assert what the truncated structure looks like
    below the depth guard, so this is proof of the availability guarantee,
    not of redaction correctness at extreme depth."""
    # F16: a hostile deeply-nested payload must not exhaust the stack
    # (RecursionError -> 500 on the unauthenticated /event endpoint).
    payload: Any = "leaf"
    for _ in range(3000):
        payload = {"next": payload}
    clean = redact_value(payload)  # must return without raising RecursionError
    assert clean is not None
