"""AAR entrypoint: ``python -m overwatch.aar [--config path] [--date YYYY-MM-DD]``.

Thin CLI wrapper around :func:`overwatch.aar.generator.generate`; this is the
only place in the AAR flow that touches ``argparse``, opens/closes a
:class:`overwatch.store.Store` connection, and (optionally) triggers
notification delivery via :mod:`overwatch.notify`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from overwatch.aar.generator import generate
from overwatch.config import load_config
from overwatch.store import Store


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args, generate today's (or a given day's) AAR, and exit.

    Args:
        argv: Argument list to parse in place of ``sys.argv[1:]``; ``None``
            uses the real process arguments. Accepting an explicit list is
            what lets tests invoke this without spawning a subprocess.

    Returns:
        Process exit code. Always ``0`` — a failure to generate or notify
        would raise, not return a nonzero code, since there is no partial
        "generated but not notified" state worth distinguishing here.
    """
    # --- Step 1: parse CLI arguments ---
    parser = argparse.ArgumentParser(prog="overwatch.aar", description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--date", default=None, help="report date (default: today)")
    parser.add_argument(
        "--notify",
        action="store_true",
        default=False,
        help="send notifications after generating the report",
    )
    args = parser.parse_args(argv)

    # --- Step 2: generate the report and, if requested, notify ---
    cfg = load_config(args.config)
    report_date = date.fromisoformat(args.date) if args.date else None
    store = Store(cfg.db_path)
    try:
        path = generate(store, cfg, report_date)
        record = store.latest_aar()
        print(f"[aar] written: {path}")
        if record:
            print(f"[aar] summary: {record['summary']}")
        if args.notify and record:
            # Imported here (not at module scope) so the notify package —
            # and its SMTP/HTTP dependencies — is only ever loaded on the
            # `--notify` path.
            from overwatch.notify import send

            send(cfg.notify, record["summary"], path, dry_run=cfg.dry_run)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
