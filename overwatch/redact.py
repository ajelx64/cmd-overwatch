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
    # Modern Slack prefixes too: xapp- (app-level) and xoxe-/xoxc- (refresh/config).
    ("slack-token", re.compile(r"\b(?:xox[baprsec]|xapp)-[A-Za-z0-9-]{10,}\b")),
    ("discord-webhook", re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S+")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        # scheme://user:password@host — redact only the inline password segment.
        # Brackets are excluded so an already-redacted token is never re-matched.
        "uri-credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:@/\s\[\]]+:)([^@/\s\[\]]+)(@)"),
    ),
    (
        # key = value / key: value. The value is captured whole — a quoted span
        # (spaces allowed) or an unquoted run up to whitespace/shell separators —
        # so secrets containing commas/quotes/`@` are not truncated and leaked.
        "assignment",
        re.compile(
            r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"auth|credential)s?)(\s*[=:]\s*)(?!\[REDACTED)"
            r"(\"[^\"]*\"|'[^']*'|[^\s;&|]{4,})"
        ),
    ),
)

# Patterns whose replacement keeps surrounding groups instead of replacing the
# whole match (key/separator for assignments, scheme/user/`@` for URIs).
_KEEP_GROUPS: dict[str, str] = {
    "assignment": r"\1\2[REDACTED:assignment]",
    "uri-credentials": r"\1[REDACTED:uri-credentials]\3",
}

# Recursion guard for :func:`redact_value`: hostile deeply-nested payloads
# (e.g. via the /event hook) must not exhaust the interpreter stack.
_MAX_DEPTH = 64


def redact_text(text: str) -> str:
    """Return ``text`` with anything secret-shaped replaced by ``[REDACTED:<label>]``."""
    for label, pattern in _PATTERNS:
        repl = _KEEP_GROUPS.get(label, f"[REDACTED:{label}]")
        text = pattern.sub(repl, text)
    return text


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside dicts, lists, and tuples.

    Non-string scalars pass through unchanged. Dict keys are left intact
    (keys are structural; values carry the secrets). Recursion is bounded at
    ``_MAX_DEPTH`` so a hostile deeply-nested payload cannot exhaust the stack.
    """
    if _depth >= _MAX_DEPTH:
        # Refuse to descend further rather than risk a RecursionError; the
        # remaining structure is dropped to a sentinel (never recursed into).
        return redact_text(value) if isinstance(value, str) else "[REDACTED:max-depth]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_value(v, _depth + 1) for v in value)
    return value
