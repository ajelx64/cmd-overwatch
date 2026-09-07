"""Shared detection vocabulary: findings, fingerprints, persistence.

Collectors are pure — they read signals and return :class:`Finding` lists.
:func:`persist_findings` is the only bridge into the store, so every
detector stays testable offline with synthetic fixtures.

Used by every collector under ``overwatch.collector`` (scheduled-task,
git-hygiene, host-health, and log-scan checks) to build and dedupe their
findings before they reach :class:`overwatch.store.Store`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from overwatch.store import Store

# Ordered worst-to-best; also the whitelist enforced by Finding.__post_init__
# so a collector typo (e.g. "hi" instead of "high") fails loudly at creation
# time rather than silently sorting wrong or breaking a severity-ordered UI.
SEVERITIES = ("critical", "high", "medium", "low")

# Used by normalize_signature to fold variable parts of a log line (any run of
# digits, any run of whitespace) down to a stable shape.
_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Finding:
    """One detected problem, ready to upsert as an issue.

    Attributes:
        fingerprint: Stable identifier (see :func:`make_fingerprint`) used to
            dedupe recurring findings into one issue instead of many.
        source: Name of the collector that produced this finding.
        severity: One of :data:`SEVERITIES`.
        title: Human-readable summary shown to the operator.
        evidence: Arbitrary supporting detail (e.g. the matched line, a
            target name); stored as-is and redacted at the storage boundary.
    """

    fingerprint: str
    source: str
    severity: str
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject an out-of-vocabulary severity at construction time.

        Raises:
            ValueError: If ``severity`` is not one of :data:`SEVERITIES`.
        """
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}; got {self.severity!r}")


def normalize_signature(line: str, max_len: int = 120) -> str:
    """Collapse a log line into a stable signature.

    Digits become ``#`` and whitespace collapses, so timestamps, durations,
    and counters don't fragment one recurring problem into many issues.

    Args:
        line: Raw log line (or similar free text) to normalize.
        max_len: Truncation length. 120 is a rule-of-thumb cap that keeps a
            signature readable in a fingerprint/issue title while still long
            enough to distinguish genuinely different log lines.

    Returns:
        The lowercased, digit-folded, whitespace-collapsed signature,
        truncated to ``max_len`` characters.
    """
    sig = _DIGITS.sub("#", line.strip().lower())
    sig = _WS.sub(" ", sig)
    return sig[:max_len]


def make_fingerprint(*parts: str) -> str:
    """Deterministic fingerprint over signature parts.

    Args:
        *parts: Strings to combine (e.g. source name plus a normalized
            signature); order matters since it changes the joined string.

    Returns:
        A 24-character hex digest. Joined with a unit-separator (``\\x1f``)
        rather than a printable delimiter so a part that happens to contain
        that delimiter itself can't be crafted to collide with a different
        split of the same parts. Truncated to 24 chars — short enough for a
        compact issue key, long enough that accidental collisions across the
        expected finding volume are not a practical concern.
    """
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:24]


def persist_findings(store: Store, findings: list[Finding]) -> list[int]:
    """Upsert findings as issues; returns issue ids (deduped per fingerprint).

    A single flat pass over ``findings``: skip a fingerprint already seen in
    this same batch (a collector can legitimately emit the same finding
    more than once per run), otherwise upsert it.

    Args:
        store: Destination store; ``upsert_issue`` handles the actual
            insert-vs-update-existing-issue decision.
        findings: Findings from one or more collector runs, not yet deduped.

    Returns:
        The issue id for each distinct fingerprint, in first-seen order.
    """
    seen: dict[str, int] = {}
    for f in findings:
        if f.fingerprint in seen:
            continue
        seen[f.fingerprint] = store.upsert_issue(
            fingerprint=f.fingerprint,
            source=f.source,
            severity=f.severity,
            title=f.title,
            evidence=f.evidence,
        )
    return list(seen.values())
