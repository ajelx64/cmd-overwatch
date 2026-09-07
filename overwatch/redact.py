"""Secret and credential redaction.

Everything overwatch persists or transmits (events, transcripts, notification
payloads) passes through here first. Hook events carry raw ``tool_input`` /
``tool_response`` text which can contain tokens, keys, passwords, or whole
``.env`` files — so redaction is applied at the storage boundary
(:meth:`overwatch.store.Store.add_event`, :meth:`overwatch.store.Store.upsert_issue`,
:meth:`overwatch.store.Store.add_solution`) — not every ``overwatch.store`` write path;
``record_approval`` and ``set_issue_status`` persist operator decisions and status
strings, not attacker-reachable hook text, and write without redaction (see the "NOT
covered" list below). The other caller is the solution executor's transcript writer
(``overwatch.solution.executor.Executor._write_transcript``), which redacts raw
``claude`` subprocess stdout/stderr (or a dry-run plan) before it is written to disk —
this is that text's first and only redaction pass, not a second one. The executor has
no notification path of its own; the only place a notification is sent is
``aar/__main__.py`` calling ``overwatch.notify.send``.

Ordering matters: specific, high-confidence patterns run before generic
assignment patterns so findings keep an informative label.

WHAT THIS MODULE COVERS — and, just as important, what it does NOT:

Matched and replaced with ``[REDACTED:<label>]`` (see ``_PATTERNS`` below for the
exact shape of each): PEM-format private key blocks; Anthropic API keys; OpenAI
API keys (including the project-scoped variant); AWS access key IDs (the
``AKIA``/``ASIA`` prefix — see the caveat below); the GitHub classic token
family and GitHub fine-grained personal access tokens; Slack tokens
(bot/user/app-level/etc.); Discord webhook URLs; JSON Web Tokens; ``Bearer
<token>`` header values; the password segment of a
``scheme://user:password@host`` URI; and the value half of a ``key = value`` or
``key: value`` pair whose key name contains one of a fixed list of
credential-sounding words (password, secret, token, api key, access key, auth,
credential — see the ``assignment`` pattern for the exact list).

NOT covered by this module — do not assume these are safe just because they
passed through here:
- Any secret that doesn't match one of the specific shapes above and isn't
  assigned via a recognized keyword. There is no generic high-entropy-string
  detector; free-form secrets pasted as prose slip through untouched.
- The AWS *secret* access key that is normally paired with an access key ID.
  Only the key ID (``AKIA``/``ASIA``-prefixed) is matched; the secret key has no
  distinguishing prefix and would require a much riskier generic pattern.
- Credential-bearing key names outside the fixed keyword list used by the
  ``assignment`` pattern (an environment variable that doesn't contain one of
  those words will not be caught by that pattern).
- General PII (names, SSNs, card numbers, phone numbers, addresses, emails).
  This module redacts secrets/credentials, not personal data.
- Non-string containers such as ``bytes`` passed to :func:`redact_value` — only
  ``str``, ``dict``, ``list``, and ``tuple`` are inspected; other types
  (including ``bytes``) pass through unchanged.
- An ``assignment`` value that is both unquoted and contains whitespace. The value
  group matches a quoted string OR a run of 4+ non-whitespace characters, so an
  unquoted value is truncated at its first space and, when that leading token is
  under 4 characters, missed entirely — ``password = my secret pass`` and
  ``api_key: ab`` are NOT redacted at all; quoting the value
  (``password = "my secret pass"``) makes it redactable. Measured, not theoretical:
  ``redact_text('password = mysecretvalue with more words')`` returns
  ``'password = [REDACTED:assignment] with more words'`` — only the first token is
  caught, and the rest of the sentence is left in the clear.
"""

from __future__ import annotations

import re
from typing import Any

# (label, compiled pattern) — applied in order.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # DOTALL lets `.` span the newlines inside a dumped PEM block, so the whole
        # multi-line key body is consumed, not just the BEGIN line. The trailing
        # alternation falls back to end-of-string so a key truncated mid-transcript
        # (no END line yet) is still redacted rather than left in the clear.
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    # Checked ahead of the broader OpenAI-shaped pattern below: this prefix is a
    # strict subset of it, so if this ran second every Anthropic key would already
    # have been consumed and mislabeled "openai-key" by the time it got here.
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    # Only the access key ID is distinguishable by prefix; the paired AWS secret
    # access key has no recognizable shape and is deliberately not matched here
    # (see the module docstring's "NOT covered" section).
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # Covers the classic token family sharing this prefix scheme (personal,
    # OAuth, user-to-server, server-to-server, refresh).
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    # Fine-grained PATs use a distinct prefix from the classic family above.
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    # Modern Slack prefixes too: xapp- (app-level) and xoxe-/xoxc- (refresh/config).
    ("slack-token", re.compile(r"\b(?:xox[baprsec]|xapp)-[A-Za-z0-9-]{10,}\b")),
    # The webhook URL itself is the credential — anyone holding it can post to the
    # channel — so the whole URL is redacted rather than trying to isolate a token.
    ("discord-webhook", re.compile(r"https://discord(?:app)?\.com/api/webhooks/\S+")),
    # A JWT's first segment is the base64url encoding of a JSON object, which
    # always opens with the two characters `{"` — that's what encodes to the
    # fixed `eyJ` lead-in this pattern anchors on.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")),
    # Bearer tokens are carried as raw header text, not `key=value` syntax, so
    # they need their own pattern rather than falling under "assignment" below.
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
    """Replace every secret-shaped substring of ``text`` with a redaction marker.

    Applies each entry in :data:`_PATTERNS` in order (see the module docstring
    for the full list of what is and is not matched); a single flat pass, not
    multiple phases.

    Args:
        text: Arbitrary text that may contain credential-shaped substrings.

    Returns:
        ``text`` with each match replaced by ``[REDACTED:<label>]`` (or, for
        patterns listed in :data:`_KEEP_GROUPS`, with only the credential
        portion of the match replaced and the surrounding text kept).
    """
    for label, pattern in _PATTERNS:
        repl = _KEEP_GROUPS.get(label, f"[REDACTED:{label}]")
        text = pattern.sub(repl, text)
    return text


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings inside dicts, lists, and tuples.

    Non-string scalars pass through unchanged. Dict keys are left intact
    (keys are structural; values carry the secrets). Recursion is bounded at
    ``_MAX_DEPTH`` so a hostile deeply-nested payload cannot exhaust the stack.

    Args:
        value: Arbitrary JSON-like data (hook payloads decode to nested
            ``dict``/``list``/``str``/scalar structures).
        _depth: Current recursion depth; internal use only, callers should
            not pass this.

    Returns:
        A structurally-identical copy of ``value`` with every ``str`` run
        through :func:`redact_text`, or the sentinel ``"[REDACTED:max-depth]"``
        in place of any non-string branch encountered past ``_MAX_DEPTH``.
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
