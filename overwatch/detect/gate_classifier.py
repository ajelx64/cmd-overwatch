"""Gate classification: decide whether a proposed remediation may run
automatically or must wait for explicit operator approval.

Called by the solution drafter (``overwatch.solution.drafter``) once it has
assembled a proposed remediation's kind and human-readable text; the resulting
:class:`GateDecision` determines whether that proposal is queued for approval
or allowed to execute unattended. Depends only on the standard library —
kept dependency-free so it stays trivially unit-testable against fixture text.

Policy (fails safe):

1. If the proposal text matches ANY gate pattern — built-in or operator-added —
   it is GATED. A match always wins, regardless of remediation kind.
2. Otherwise, it is AUTO only when its ``kind`` is in the explicit
   :data:`SAFE_KINDS` allowlist of machine-known remediations.
3. Everything else is GATED as ``uncertain``.

The built-in gated set is immutable: operator config can only ADD patterns
(``[gates] extra_patterns``), never remove these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType

# Remediation kinds allowed to run without an approval click. Deliberately
# tiny; anything novel or free-form (e.g. a drafted code fix) is not here.
SAFE_KINDS = frozenset({"log-purge", "task-restart", "report-only"})

UNCERTAIN = "uncertain"

# Immutable built-in gate categories -> patterns (case-insensitive).
#
# Several categories below pair a verb with a nearby noun using a
# `[^.\n]{0,40}` gap (e.g. "merge ... main", "install ... service"). 40 chars
# is a deliberately short window — long enough to span "the changes onto" but
# short enough that the verb and noun almost certainly describe the same
# action rather than two unrelated clauses later in the same paragraph; the
# excluded characters are literally just `.` and `\n`, so the gap stops at a
# period or newline but NOT at `!` or `?` — "sentence boundary" overstates
# what the character class actually excludes.
BUILT_IN_GATES: MappingProxyType[str, tuple[str, ...]] = MappingProxyType(
    {
        "money": (
            r"\b(payment|invoice|refund|pricing|subscription|purchase|billing|payout)\b",
        ),
        "publishing": (
            r"\b(publish|release|deploy)\b",
            r"\bmake\s+\w+\s+public\b",
            r"\b(social|advertis\w*|newsletter)\b",
        ),
        "customer-data": (
            r"\b(customer|client)\s+(data|record|message|email)s?\b",
            r"\b(pii|personal\s+data|gdpr)\b",
        ),
        "secrets": (
            r"\b(secret|credential|api[_\s-]?key|token|password|passphrase)s?\b",
            r"\.env\b",
            r"\bprivate\s+key\b",
        ),
        "main-merge": (
            r"\b(merge|push|commit)\b[^.\n]{0,40}\b(main|master|production)\b",
            r"\bforce[\s-]?push\b",
        ),
        "destructive": (
            r"\b(delete|drop|remove|purge|prune)\b[^.\n]{0,40}"
            r"\b(branch|tag|database|db|backup|snapshot|table|repo)s?\b",
            r"\brm\s+-rf\b",
            r"\bfilter-repo\b",
        ),
        "auth-network": (
            # match auth/authn/authz/authenticate*/authorize* — but not "author"
            r"\b(auth|authn|authz|authenticat\w*|authoriz\w*|authoris\w*)\b",
            r"\b(firewall|open\s+port|expose|bind)\b[^.\n]{0,40}\b(network|internet|0\.0\.0\.0)\b",
            r"\b0\.0\.0\.0\b",
            r"\bpermission\s+(mode|set|widen)\w*\b",
        ),
        "service-install": (
            r"\b(install|register|enable)\b[^.\n]{0,40}"
            r"\b(service|scheduled\s+task|daemon|startup)\b",
            r"\b(schtasks|register-scheduledtask|systemd)\b",
        ),
        "legal": (
            r"\b(terms\s+of\s+service|privacy\s+policy|license\s+change|compliance|warranty)\b",
        ),
    }
)


@dataclass(frozen=True)
class GateDecision:
    """Outcome of classifying one proposed remediation.

    Attributes:
        gated: True if the proposal must wait for operator approval; False
            if it may run unattended.
        category: Which gate category matched (a key of :data:`BUILT_IN_GATES`
            or ``"custom"``), ``"uncertain"`` if nothing matched but the kind
            wasn't on the safe allowlist either, or ``"none"`` when auto.
        matched: The specific regex patterns that fired, for the operator's
            audit trail — empty when the decision was ``"none"`` or
            ``"uncertain"`` (nothing matched in either case).
        reason: Human-readable explanation shown alongside the decision.
    """

    gated: bool
    category: str  # gate category, "uncertain", or "none" (auto)
    matched: tuple[str, ...] = ()  # patterns that fired
    reason: str = ""

    @property
    def auto(self) -> bool:
        """Return True if this proposal may run without operator approval."""
        return not self.gated


def _match_patterns(text: str, patterns: tuple[str, ...]) -> list[str]:
    """Return every pattern in ``patterns`` that matches somewhere in ``text``."""
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


def classify(
    kind: str,
    text: str,
    extra_patterns: tuple[str, ...] = (),
) -> GateDecision:
    """Classify a proposed remediation.

    ``kind`` is the machine-known remediation type; ``text`` is everything the
    operator would read (title, diagnosis, proposed action, evidence excerpts).

    Args:
        kind: Machine-known remediation type (checked against
            :data:`SAFE_KINDS` only if no gate pattern matches).
        text: Human-readable proposal text to scan for gate patterns.
        extra_patterns: Operator-configured patterns (``[gates]
            extra_patterns``) checked after the immutable built-in set.

    Returns:
        The resulting :class:`GateDecision`.
    """
    # --- Step 1: build one haystack so patterns can match across kind and text ---
    # (e.g. a gate phrase split between the remediation's kind label and its
    # free-text description still matches as a single string).
    haystack = f"{kind}\n{text}"

    # --- Step 2: built-in gates always win, checked before anything else ---
    for category, patterns in BUILT_IN_GATES.items():
        hits = _match_patterns(haystack, patterns)
        if hits:
            return GateDecision(
                gated=True,
                category=category,
                matched=tuple(hits),
                reason=f"matched built-in gate '{category}'",
            )

    # --- Step 3: operator-added patterns get their own category label ---
    extra_hits = _match_patterns(haystack, extra_patterns)
    if extra_hits:
        return GateDecision(
            gated=True,
            category="custom",
            matched=tuple(extra_hits),
            reason="matched operator-configured gate pattern",
        )

    # --- Step 4: no pattern matched — fall back to the safe-kind allowlist ---
    if kind in SAFE_KINDS:
        return GateDecision(
            gated=False,
            category="none",
            reason=f"kind '{kind}' is on the safe allowlist and no gate pattern matched",
        )

    # --- Step 5: fail safe — anything novel or unrecognized stays gated ---
    return GateDecision(
        gated=True,
        category=UNCERTAIN,
        reason=f"kind '{kind}' is not on the safe allowlist — defaulting to gated",
    )
