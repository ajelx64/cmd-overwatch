"""Log scanner tests over synthetic day-log fixtures (block format mirrors
common scheduled-runner logs: ``=== <ts> start <task> (<cmd>) ===`` ...
``=== exit N @ HH:MM ===``)."""

from pathlib import Path

from overwatch.collector.log_scan import scan_target, split_blocks
from overwatch.config import Target
from overwatch.detect.rules import persist_findings
from overwatch.store import Store

CLEAN = """\
=== 2026-01-05 06:30:01 -07:00  start nightly-job  (python run.py) ===
processed 120 items
all good
=== exit 0 @ 06:31:43 ===
"""

FAILED_EXIT = """\
=== 2026-01-05 07:00:01 -07:00  start sync-job  (python sync.py) ===
connecting...
fatal: could not resolve host
=== exit 1 @ 07:00:05 ===
"""

TRACEBACK = """\
=== 2026-01-05 08:00:00 -07:00  start etl  (python etl.py) ===
loading config
Traceback (most recent call last):
  File "etl.py", line 10, in <module>
    main()
  File "etl.py", line 7, in main
    data[key]
KeyError: 'symbols'
=== exit 1 @ 08:00:02 ===
"""

ERROR_LINES = """\
=== 2026-01-05 09:00:00 -07:00  start fetcher  (python fetch.py) ===
INFO: starting
ERROR: upstream timeout after 30s
ERROR: upstream timeout after 31s
ERROR: disk quota exceeded
=== exit 0 @ 09:00:40 ===
"""

UNTERMINATED = """\
=== 2026-01-05 10:00:00 -07:00  start long-job  (python long.py) ===
still working...
"""

FORMATLESS = """\
2026-01-05 11:00:00 INFO booted
2026-01-05 11:00:01 ERROR cannot open database
"""


def make_target(tmp_path: Path, **files: str) -> Target:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for name, content in files.items():
        (log_dir / f"{name}.log").write_text(content, encoding="utf-8")
    return Target(name="synthetic", log_dir=log_dir)


def test_clean_log_yields_nothing(tmp_path: Path) -> None:
    assert scan_target(make_target(tmp_path, **{"nightly-job-2026-01-05": CLEAN})) == []


def test_nonzero_exit_detected(tmp_path: Path) -> None:
    findings = scan_target(make_target(tmp_path, **{"sync-job-2026-01-05": FAILED_EXIT}))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "high"
    assert "exit 1" in f.title
    assert f.evidence["task"] == "sync-job"
    assert "fatal: could not resolve host" in f.evidence["tail"]


def test_traceback_detected_with_exception_signature(tmp_path: Path) -> None:
    findings = scan_target(make_target(tmp_path, **{"etl-2026-01-05": TRACEBACK}))
    kinds = sorted(f.title for f in findings)
    # one for exit 1, one for the KeyError traceback
    assert len(findings) == 2
    assert any("KeyError: 'symbols'" in t for t in kinds)


def test_error_lines_dedupe_on_normalized_signature(tmp_path: Path) -> None:
    findings = scan_target(make_target(tmp_path, **{"fetcher-2026-01-05": ERROR_LINES}))
    # two timeout lines differ only in digits -> one finding; quota -> another
    assert len(findings) == 2
    assert all(f.severity == "medium" for f in findings)


def test_unterminated_block_no_false_exit_finding(tmp_path: Path) -> None:
    findings = scan_target(make_target(tmp_path, **{"long-job-2026-01-05": UNTERMINATED}))
    assert findings == []  # still running is not a failure


def test_formatless_file_falls_back_to_whole_file(tmp_path: Path) -> None:
    findings = scan_target(make_target(tmp_path, **{"daemon-2026-01-05": FORMATLESS}))
    assert len(findings) == 1
    assert findings[0].evidence["task"] == "daemon"


def test_multiple_runs_per_file(tmp_path: Path) -> None:
    findings = scan_target(
        make_target(tmp_path, **{"sync-job-2026-01-05": CLEAN + FAILED_EXIT + FAILED_EXIT})
    )
    # identical failures share a fingerprint
    assert len({f.fingerprint for f in findings}) == 1


def test_missing_log_dir_yields_nothing(tmp_path: Path) -> None:
    t = Target(name="ghost", log_dir=tmp_path / "nope")
    assert scan_target(t) == []


def test_old_files_skipped(tmp_path: Path) -> None:
    import os
    import time

    target = make_target(tmp_path, **{"sync-job-2025-10-01": FAILED_EXIT})
    old = time.time() - 30 * 86400
    assert target.log_dir is not None
    os.utime(target.log_dir / "sync-job-2025-10-01.log", (old, old))
    assert scan_target(target) == []


def test_rescan_does_not_duplicate_issues(tmp_path: Path) -> None:
    target = make_target(tmp_path, **{"sync-job-2026-01-05": FAILED_EXIT})
    store = Store(tmp_path / "t.db")
    ids1 = persist_findings(store, scan_target(target))
    ids2 = persist_findings(store, scan_target(target))
    assert ids1 == ids2
    issues = store.list_issues()
    assert len(issues) == 1
    assert issues[0]["count"] == 2  # recurrence bumped, not duplicated


def test_block_splitting_handles_glued_runs() -> None:
    blocks = split_blocks(CLEAN + FAILED_EXIT, "fallback")
    assert [b.task for b in blocks] == ["nightly-job", "sync-job"]
    assert [b.exit_code for b in blocks] == [0, 1]
