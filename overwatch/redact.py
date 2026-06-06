"""Secret and credential redaction.

Everything overwatch persists or transmits (events, transcripts, notification
payloads) passes through here first. Hook events carry raw ``tool_input`` /
``tool_response`` text which can contain tokens, keys, passwords, or whole
``.env`` files — so redaction is applied at the storage boundary
(:meth:`overwatch.store.Store.add_event`), not left to callers.

Ordering matters: specific, high-confidence patterns run before generic
assignment patterns so findings keep an informative label.
"""

from __future__ import annotations

import re
from typing import Any

# (label, compiled pattern) — applied in order.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("discord-webhook", re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S+")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        "assignment",
        re.compile(
            r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"auth|credential)s?)(\s*[=:]\s*)(?!\[REDACTED)([\"']?)[^\s\"',;]{4,}\3"
        ),
    ),
)

_ASSIGNMENT_LABEL = "assignment"


def redact_text(text: str) -> str:
    """Return ``text`` with anything secret-shaped replaced by ``[REDACTED:<label>]``."""
    for label, pattern in _PATTERNS:
        if label == _ASSIGNMENT_LABEL:
            # Keep the key name and separator; redact only the value.
            text = pattern.sub(rf"\1\2[REDACTED:{label}]", text)
        else:
            text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts, lists, and tuples.

    Non-string scalars pass through unchanged. Dict keys are left intact
    (keys are structural; values carry the secrets).
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v) for v in value)
    return value
