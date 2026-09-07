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

Invoked by the collector entrypoint (``overwatch.collector.__main__``) once per
configured target. Depends on ``overwatch.config.Target`` for where to look and
``overwatch.detect.rules`` for the ``Finding``/fingerprint vocabulary shared by
every collector.
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

# Matches the scheduled-runner block header shown in the module docstring; the
# command in parentheses is optional because some runners omit it.
_START = re.compile(r"^=== .*?\bstart\s+(?P<task>\S+)\s*(?:\((?P<cmd>.*)\))?\s*===\s*$")
# Matches the matching block footer; anything between a start and this line
# (or EOF) belongs to that run.
_EXIT = re.compile(r"^=== exit\s+(?P<code>-?\d+)\b.*===\s*$")
# Deliberately loose (word-boundary only): treated as a medium-severity signal
# per occurrence, so false positives just add noise rather than mask a failure.
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
    """Split a day-log into run blocks; format-less files become one block.

    Single flat pass over the lines: each line is dispatched by kind (block
    start, block end, or plain content) and folded into the block currently
    being built. There is no multi-stage pipeline here to narrate in steps.

    Args:
        text: Full contents of one day-log file.
        fallback_task: Task name to use for content that never appears inside
            a recognized start/exit block (i.e. the file doesn't use the
            scheduled-runner format described in the module docstring).

    Returns:
        The run blocks found, in file order. A block whose ``=== exit ===``
        footer never appears (truncated log, or no-format file) is still
        included with ``exit_code`` left as ``None``.
    """
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
    # Flush a trailing block that never hit an exit line (truncated log), but
    # don't emit an empty placeholder block for a file that ended cleanly.
    if current is not None and (current.lines or current.exit_code is None):
        blocks.append(current)
    return blocks


def _traceback_signature(lines: list[str]) -> list[str]:
    """Signatures of each traceback in the block (its final exception line).

    Single pass with a boolean "inside a traceback" flag — there is only one
    phase (scan and match), so no step banners are added here.

    Args:
        lines: Lines belonging to one run block, in file order.

    Returns:
        One string per traceback found: normally the stripped exception line
        (e.g. ``"ValueError: bad input"``), or the literal string
        ``"Traceback (truncated)"`` if the block ends mid-traceback (log
        rotated or process killed before the exception line was written).
    """
    sigs: list[str] = []
    in_tb = False
    for line in lines:
        if _TRACEBACK_START.match(line):
            in_tb = True
            continue
        if in_tb and _EXC_LINE.match(line):
            sigs.append(line.strip())
            in_tb = False
    if in_tb:  # truncated traceback at EOF
        sigs.append("Traceback (truncated)")
    return sigs


def scan_block(block: RunBlock, target_name: str, file_name: str) -> list[Finding]:
    """Detect the three signal shapes (failed exit, traceback, error line) in a block.

    Args:
        block: One parsed run block from :func:`split_blocks`.
        target_name: Name of the target the log belongs to (used in
            fingerprints and titles so findings from different targets never
            collide).
        file_name: Name of the log file the block came from, for evidence
            only.

    Returns:
        Zero or more findings; a block can trigger several signals at once
        (e.g. a non-zero exit whose tail also contains ``ERROR`` lines).
    """
    findings: list[Finding] = []
    # Last few non-blank lines: enough context for a human to recognize the
    # failure without embedding the whole (potentially large) block.
    tail = [ln for ln in block.lines[-8:] if ln.strip()]

    # --- Step 1: non-zero exit code -> high severity ---
    if block.exit_code is not None and block.exit_code != 0:
        # Fold the tail's last line into the fingerprint so repeated failures
        # with the same tail dedupe into one issue instead of one per run.
        sig = normalize_signature(tail[-1]) if tail else ""
        code = str(block.exit_code)
        findings.append(
            Finding(
                fingerprint=make_fingerprint(target_name, block.task, "exit", code, sig),
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

    # --- Step 2: Python tracebacks -> high severity, one finding per traceback ---
    for exc in _traceback_signature(block.lines):
        exc_sig = normalize_signature(exc)
        findings.append(
            Finding(
                fingerprint=make_fingerprint(target_name, block.task, "traceback", exc_sig),
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

    # --- Step 3: ERROR lines -> medium severity, deduped within this block ---
    seen_sigs: set[str] = set()
    for line in block.lines:
        if _ERROR_LINE.search(line):
            sig = normalize_signature(line)
            if sig in seen_sigs:
                # Same normalized error already recorded for this block —
                # skip so one noisy loop doesn't produce dozens of findings.
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
    """Scan a target's log dir; missing/empty dirs yield no findings.

    Single flat pass: for each matching file, skip it if stale or unreadable,
    then split and scan its blocks. There is one loop with no distinct
    sequential stages, so no step banners are added here.

    Args:
        target: Target configuration naming the log directory and glob.
        scan_window_days: How many days back to look, by file modification
            time. Fingerprint-based dedup (see ``make_fingerprint``) makes it
            safe to re-scan the same window on every pass, so this only needs
            to be wide enough to not miss a slow-to-notice failure.

    Returns:
        Findings from every in-window, readable file under the target's log
        directory.
    """
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
            # File vanished, permissions changed, or a transient I/O error —
            # treat it the same as "nothing to report" rather than aborting
            # the whole target.
            continue
        fallback = _fallback_task_name(path)
        for block in split_blocks(text, fallback):
            findings.extend(scan_block(block, target.name, path.name))
    return findings


def _fallback_task_name(path: Path) -> str:
    """Derive a task name for a log file that has no ``start`` line to name it.

    Args:
        path: Path to the log file, e.g. ``paper-session-2026-01-05.log``.

    Returns:
        The file stem with a trailing ``-YYYY-MM-DD`` date stripped, e.g.
        ``paper-session``. If the stem is nothing but that date (stripping it
        would leave an empty string), the untouched stem is returned instead
        (``or stem``) so callers never receive an empty task name.
    """
    stem = path.stem
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", stem) or stem
