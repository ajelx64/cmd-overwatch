"""Redaction tests. All secrets here are synthetic fixtures, never real."""

from typing import Any

import pytest

from overwatch.redact import redact_text, redact_value

FAKE_ANTHROPIC = "sk-ant-api03-aaaabbbbccccddddeeeeffff0000111122223333"  # gitleaks:allow
FAKE_GITHUB = "ghp_aaaabbbbccccddddeeeeffff000011112222"  # gitleaks:allow
FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"  # gitleaks:allow
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P"  # gitleaks:allow  # noqa: E501
FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"


def test_anthropic_key_redacted() -> None:
    out = redact_text(f"calling api with {FAKE_ANTHROPIC} now")
    assert FAKE_ANTHROPIC not in out
    assert "[REDACTED:anthropic-key]" in out


def test_github_token_redacted() -> None:
    out = redact_text(f"git remote set-url https://{FAKE_GITHUB}@github.com/x/y")
    assert FAKE_GITHUB not in out
    assert "[REDACTED:github-token]" in out


def test_aws_key_redacted() -> None:
    out = redact_text(f"export AWS_ACCESS_KEY_ID={FAKE_AWS}")
    assert FAKE_AWS not in out


def test_jwt_redacted() -> None:
    out = redact_text(f"Authorization header was {FAKE_JWT}")
    assert FAKE_JWT not in out
    assert "[REDACTED:jwt]" in out


def test_pem_block_redacted() -> None:
    out = redact_text(f"file contents:\n{FAKE_PEM}\ndone")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "[REDACTED:private-key]" in out


def test_bearer_redacted() -> None:
    out = redact_text("curl -H 'Authorization: Bearer abc123def456ghi789jkl'")  # gitleaks:allow
    assert "abc123def456ghi789jkl" not in out


def test_password_assignment_keeps_key_redacts_value() -> None:
    out = redact_text("DB_PASSWORD=hunter2secret")
    assert "hunter2secret" not in out
    assert "DB_PASSWORD" in out
    assert "[REDACTED:assignment]" in out


def test_api_key_colon_assignment() -> None:
    out = redact_text("api_key: super-secret-value-9000")
    assert "super-secret-value-9000" not in out
    assert "api_key" in out


def test_discord_webhook_redacted() -> None:
    url = "https://discord.com/api/webhooks/1234567890/AbCdEfGh-secret_part"
    out = redact_text(f"posting to {url}")
    assert url not in out
    assert "[REDACTED:discord-webhook]" in out


def test_plain_text_untouched() -> None:
    s = "Read file C:/projects/example/main.py and ran 12 tests"
    assert redact_text(s) == s


def test_idempotent() -> None:
    once = redact_text(f"key {FAKE_ANTHROPIC}")
    assert redact_text(once) == once


def test_redact_value_recurses_containers() -> None:
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
    assert redact_value(42) == 42
    assert redact_value(None) is None
    assert redact_value(3.14) == 3.14


# -- audit regression: redaction gaps (F1, F2, F4) and recursion bound (F16) ----


def test_assignment_value_with_separators_fully_redacted() -> None:
    # F1: a value containing commas must be redacted whole, not truncated at the
    # first separator (which previously left the tail in cleartext).
    out = redact_text("password=a1b2c3d4,e5f6g7h8,i9j0k1l2")
    assert "e5f6g7h8" not in out
    assert "i9j0k1l2" not in out
    assert "[REDACTED:assignment]" in out


def test_quoted_assignment_value_with_spaces_redacted() -> None:
    # F1: a quoted value with internal spaces must be fully redacted.
    out = redact_text('client_secret = "abcd efgh ijkl mnop"')
    assert "efgh" not in out
    assert "mnop" not in out
    assert "[REDACTED:assignment]" in out


def test_connection_string_password_redacted() -> None:
    # F2: scheme://user:password@host — redact the password, keep scheme/host.
    out = redact_text("DATABASE_URL=postgres://appuser:s3cr3tPassw0rd@db.internal:5432/app")
    assert "s3cr3tPassw0rd" not in out
    assert "[REDACTED:uri-credentials]" in out
    assert "postgres://" in out
    assert "db.internal" in out


def test_mongodb_uri_password_redacted() -> None:
    # F2: a second scheme to prove the rule is not postgres-specific.
    out = redact_text("mongodb://admin:TopSecretValue9000@cluster0.example.net/prod")
    assert "TopSecretValue9000" not in out


@pytest.mark.parametrize("prefix", ["xapp-1", "xoxe-1", "xoxc-2"])
def test_modern_slack_token_prefixes_redacted(prefix: str) -> None:
    # F4: xapp-/xoxe-/xoxc- carry real privilege and were previously missed.
    # Tokens are assembled at runtime so the source holds no literal secret
    # (keeps GitHub push protection / secret scanners from flagging the fixture).
    token = f"{prefix}-{'ab12cd34ef' * 3}"
    out = redact_text(f"posting via {token}")
    assert token not in out
    assert "[REDACTED:slack-token]" in out


def test_redact_value_bounds_deep_recursion() -> None:
    # F16: a hostile deeply-nested payload must not exhaust the stack
    # (RecursionError -> 500 on the unauthenticated /event endpoint).
    payload: Any = "leaf"
    for _ in range(3000):
        payload = {"next": payload}
    clean = redact_value(payload)  # must return without raising RecursionError
    assert clean is not None
