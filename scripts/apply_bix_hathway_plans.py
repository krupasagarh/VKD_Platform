"""Apply Bix household plan amount to Hathway customers on the platform.

Plan Amount in Bix is per customer row (household), NOT multiplied by STB count.
When one Bix row lists several boxes, that single Plan Amount is used once.

  python scripts/apply_bix_hathway_plans.py --dry-run
  python scripts/apply_bix_hathway_plans.py --file "C:\\path\\Customer_Export_....csv"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import bix_sync
from app.db import transaction


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Bix household plan amounts for Hathway customers")
    parser.add_argument(
        "--file",
        type=Path,
        help="Bix Customer export (.csv / .xls). Defaults to cableway bix_accounts.csv",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing",
    )
    args = parser.parse_args()

    source = args.file or bix_sync.master_accounts_path()
    if not source.is_file():
        print(f"Bix file not found: {source}")
        return 1

    with transaction() as conn:
        preview = bix_sync.plan_amounts_from_bix_file(conn, source)
        summary = bix_sync.apply_bix_hathway_plan_amounts(
            conn,
            source_path=source,
            dry_run=args.dry_run,
        )

    print(f"Bix source: {source}")
    print(f"Households matched on platform: {len(preview)}")
    mode = "DRY RUN" if args.dry_run else "APPLIED"
    print(f"\n{mode}")
    print(f"  Updated   : {summary['updated']}")
    print(f"  Unchanged : {summary['unchanged']}")

    if summary["samples"]:
        print("\nSample updates:")
        for row in summary["samples"]:
            stb_note = f", {row['stbs']} STB" if row.get("stbs") else ""
            print(
                f"  {row['code']} · {row['name']} — "
                f"Rs {row['amount_rupees']:.2f}{stb_note} · {row['plan_name']}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
