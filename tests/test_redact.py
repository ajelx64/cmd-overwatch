"""Redaction tests. All secrets here are synthetic fixtures, never real."""

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
