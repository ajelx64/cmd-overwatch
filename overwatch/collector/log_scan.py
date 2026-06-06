"""Signal (b): scan target day-logs for failed runs.

Understands the common scheduled-runner block format::

    === 2026-01-05 06:30:01 -07:00  start <task>  (<command>) ===
    ...run output...
    === exit 0 @ 06:31:43 ===

Multiple blocks per file (day-logs are appended per run). Files that don't
use the block format degrade to a single whole-file block. Detected:

- non-zero ``exit N`` per block  -> high
- Python ``Traceback`` blocks    -> high (signature = final exception line)
- ``ERROR`` lines                -> medium (signature = normalized line)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from overwatch.config import Target
from overwatch.detect.rules import Finding, make_fingerprint, normalize_signature

SOURCE = "log_scan"

# Files untouched for longer than this are skipped; fingerprint dedup makes
# re-scanning the window harmless.
DEFAULT_SCAN_WINDOW_DAYS = 3

_START = re.compile(r"^=== .*?\bstart\s+(?P<task>\S+)\s*(?:\((?P<cmd>.*)\))?\s*===\s*$")
_EXIT = re.compile(r"^=== exit\s+(?P<code>-?\d+)\b.*===\s*$")
_ERROR_LINE = re.compile(r"\bERROR\b")
_TRACEBACK_START = re.compile(r"^Traceback \(most recent call last\):")
# Final line of a Python traceback: "SomeError: message" or bare "SomeError"
_EXC_LINE = re.compile(r"^(?P<exc>[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt))\b.*")


@dataclass
class RunBlock:
    """One delimited run inside a day-log."""

    task: str
    lines: list[str] = field(default_factory=list)
    exit_code: int | None = None  # None = block never terminated


def split_blocks(text: str, fallback_task: str) -> list[RunBlock]:
    """Split a day-log into run blocks; format-less files become one block."""
    blocks: list[RunBlock] = []
    current: RunBlock | None = None
    for line in text.splitlines():
        start = _START.match(line)
        if start:
            if current is not None:
                blocks.append(current)
            current = RunBlock(task=start.group("task"))
            continue
        exit_m = _EXIT.match(line)
        if exit_m and current is not None:
            current.exit_code = int(exit_m.group("code"))
            blocks.append(current)
            current = None
            continue
        if current is None:
            # Content outside any block: treat the file as format-less.
            current = RunBlock(task=fallback_task)
        current.lines.append(line)
    if current is not None and (current.lines or current.exit_code is None):
        blocks.append(current)
    return blocks


def _traceback_signature(lines: list[str]) -> list[str]:
    """Signatures of each traceback in the block (its final exception line)."""
    sigs: list[str] = []
    in_tb = False
    last_exc: str | None = None
    for line in lines:
        if _TRACEBACK_START.match(line):
            in_tb = True
            last_exc = None
            continue
        if in_tb:
            if _EXC_LINE.match(line):
                last_exc = line.strip()
                sigs.append(last_exc)
                in_tb = False
    if in_tb:  # truncated traceback at EOF
        sigs.append("Traceback (truncated)")
    return sigs


def scan_block(block: RunBlock, target_name: str, file_name: str) -> list[Finding]:
    findings: list[Finding] = []
    tail = [ln for ln in block.lines[-8:] if ln.strip()]

    if block.exit_code is not None and block.exit_code != 0:
        sig = normalize_signature(tail[-1]) if tail else ""
        findings.append(
            Finding(
                fingerprint=make_fingerprint(target_name, block.task, "exit", str(block.exit_code), sig),
                source=SOURCE,
                severity="high",
                title=f"{target_name}/{block.task}: run failed with exit {block.exit_code}",
                evidence={
                    "target": target_name,
                    "file": file_name,
                    "task": block.task,
                    "exit_code": block.exit_code,
                    "tail": tail,
                },
            )
        )

    for exc in _traceback_signature(block.lines):
        findings.append(
            Finding(
                fingerprint=make_fingerprint(target_name, block.task, "traceback", normalize_signature(exc)),
                source=SOURCE,
                severity="high",
                title=f"{target_name}/{block.task}: {exc[:80]}",
                evidence={
                    "target": target_name,
                    "file": file_name,
                    "task": block.task,
                    "exception": exc,
                },
            )
        )

    seen_sigs: set[str] = set()
    for line in block.lines:
        if _ERROR_LINE.search(line):
            sig = normalize_signature(line)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            findings.append(
                Finding(
                    fingerprint=make_fingerprint(target_name, block.task, "error-line", sig),
                    source=SOURCE,
                    severity="medium",
                    title=f"{target_name}/{block.task}: {line.strip()[:80]}",
                    evidence={
                        "target": target_name,
                        "file": file_name,
                        "task": block.task,
                        "line": line.strip(),
                    },
                )
            )
    return findings


def scan_target(
    target: Target, scan_window_days: int = DEFAULT_SCAN_WINDOW_DAYS
) -> list[Finding]:
    """Scan a target's log dir; missing/empty dirs yield no findings."""
    if target.log_dir is None or not target.log_dir.is_dir():
        return []
    cutoff = time.time() - scan_window_days * 86400
    findings: list[Finding] = []
    for path in sorted(target.log_dir.glob(target.log_glob)):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fallback = _fallback_task_name(path)
        for block in split_blocks(text, fallback):
            findings.extend(scan_block(block, target.name, path.name))
    return findings


def _fallback_task_name(path: Path) -> str:
    """``paper-session-2026-01-05.log`` -> ``paper-session``."""
    stem = path.stem
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", stem) or stem
