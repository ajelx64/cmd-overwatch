"""AAR entrypoint: ``python -m overwatch.aar [--config path] [--date YYYY-MM-DD]``."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from overwatch.aar.generator import generate
from overwatch.config import load_config
from overwatch.store import Store


def main(argv: list[str] | None = None) -> int:
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
            from overwatch.notify import send

            send(cfg.notify, record["summary"], path, dry_run=cfg.dry_run)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
