"""Shared detection vocabulary: findings, fingerprints, persistence.

Collectors are pure — they read signals and return :class:`Finding` lists.
:func:`persist_findings` is the only bridge into the store, so every
detector stays testable offline with synthetic fixtures.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from overwatch.store import Store

SEVERITIES = ("critical", "high", "medium", "low")

_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Finding:
    """One detected problem, ready to upsert as an issue."""

    fingerprint: str
    source: str
    severity: str
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}; got {self.severity!r}")


def normalize_signature(line: str, max_len: int = 120) -> str:
    """Collapse a log line into a stable signature.

    Digits become ``#`` and whitespace collapses, so timestamps, durations,
    and counters don't fragment one recurring problem into many issues.
    """
    sig = _DIGITS.sub("#", line.strip().lower())
    sig = _WS.sub(" ", sig)
    return sig[:max_len]


def make_fingerprint(*parts: str) -> str:
    """Deterministic fingerprint over signature parts."""
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:24]


def persist_findings(store: Store, findings: list[Finding]) -> list[int]:
    """Upsert findings as issues; returns issue ids (deduped per fingerprint)."""
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
