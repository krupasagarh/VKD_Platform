"""Align dues for households already on the platform with the Bix master export.

Does not create customers and does not add or remove Hathway boxes. Live boxes
come from the Hathway dashboard.

Usage:
    python scripts/align_all_bix.py --dry-run
    python scripts/align_all_bix.py --apply
    python scripts/align_all_bix.py --apply --backup
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import bix_sync  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import transaction  # noqa: E402

BIX_ACCOUNTS = bix_sync.master_accounts_path()


def _names_differ(bix_name: str, platform_name: str) -> bool:
    a = (bix_name or "").split("(")[0].strip().lower()
    b = (platform_name or "").split("(")[0].strip().lower()
    if not a or not b:
        return False
    return a not in b and b not in a and a[:6] != b[:6]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    parser.add_argument("--apply", action="store_true", help="Apply alignment to the platform DB")
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Copy vk_platform.db to vk_platform.db.bak before applying",
    )
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("Pass --dry-run or --apply")

    if not BIX_ACCOUNTS.is_file():
        print(f"Bix accounts file not found: {BIX_ACCOUNTS}", file=sys.stderr)
        return 1

    items = bix_sync.load_master_items()
    print(f"Loaded {len(items)} Bix households from {BIX_ACCOUNTS.name}")

    with transaction() as conn:
        preview = bix_sync.preview(conn, items)
        creates = [r for r in preview if r["action"] == "create"]
        renames = [r for r in preview if r["customer_id"] and _names_differ(r["name"], r["platform_name"])]
        missing_stb = []
        for row in preview:
            if not row.get("customer_id"):
                continue
            have = {
                (r["upstream_id"] or "").upper()
                for r in conn.execute(
                    "SELECT upstream_id FROM connections WHERE customer_id = ?",
                    (row["customer_id"],),
                )
            }
            want = {s.upper() for s in (row.get("stbs") or [])}
            if want - have:
                missing_stb.append(row)

        print(
            f"Preview: {len(creates)} to create, {len(renames)} name fixes, "
            f"{len(missing_stb)} missing STB(s), "
            f"{sum(1 for r in preview if r['action'] == 'adjust')} due adjustment(s)"
        )

        if args.dry_run:
            if creates:
                print("\nWould create:")
                for row in creates:
                    print(f"  {row['code']:8} {row['name'][:40]:40} {row['phone']} {row['stbs']}")
            if renames:
                print("\nWould rename (first 30):")
                for row in renames[:30]:
                    print(
                        f"  {row['code']:8} Bix: {row['name'][:32]:32} "
                        f"<- Platform: {row['platform_name'][:32]}"
                    )
                if len(renames) > 30:
                    print(f"  ... and {len(renames) - 30} more")
            if missing_stb:
                print("\nBix lists boxes the platform does not have (not added — Hathway dashboard is the box list):")
                for row in missing_stb[:20]:
                    print(f"  {row['code']:8} {row['name'][:30]:30} {row['stbs']}")
            return 0

        if args.backup:
            backup = settings.db_path.with_suffix(".db.bak")
            shutil.copy2(settings.db_path, backup)
            print(f"Backup written to {backup}")

        summary = bix_sync.apply_preview(
            conn, preview, create_missing=True, actor="align_all_bix",
        )
        print("Applied:", json.dumps(summary, indent=2))

        kk = conn.execute(
            "SELECT id, name, code, phone, status FROM customers WHERE upper(code) = 'KK-1'"
        ).fetchone()
        if kk:
            stbs = [
                r["upstream_id"]
                for r in conn.execute(
                    "SELECT upstream_id FROM connections WHERE customer_id = ?", (kk["id"],)
                )
            ]
            print(f"KK-1 check: {dict(kk)} STBs={stbs}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
