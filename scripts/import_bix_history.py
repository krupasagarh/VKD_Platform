"""Import Bix bill/payment history lines into VK Platform.

These rows appear on each customer under **Bix history** (and Payments → Bix).
They are read-only — they do **not** change net due. For dues, use Bix update CSV
or ``scripts/align_all_bix.py``.

Typical daily run (extract from Bix, then import new lines):

    cd vk_platform
    python scripts/import_bix_history.py

One-time Bix login (if extract fails):

    python bix42_export/auto_extract.py --login
    (or set BIX_MOBILE and BIX_PASSWORD in the workspace .env)

If you already have ``vk_digital_history.db`` on disk and only need import:

    python scripts/import_bix_history.py --import-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import bix_history, bix_schedule  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_setting, log_activity, set_setting, transaction  # noqa: E402


def _archive_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract Bix balance history (optional) and import into the platform DB.",
    )
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="Do not call Bix; import from the history .db file only",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Import even when the archive file mtime did not change (duplicates are still skipped)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=None,
        help=f"History SQLite file (default: {settings.bix_history_db})",
    )
    args = parser.parse_args()

    history_path = Path(args.file) if args.file else bix_history.default_archive_path()
    print(f"Platform DB: {settings.db_path}")
    print(f"History archive: {history_path}")

    extract_result = None
    if not args.import_only:
        print("\n[1/2] Extracting balance history from Bix…")
        print(
            "  Headless browser — often 15–45 minutes for ~800 customers. "
            "Progress every 25 customers; first line may take 1–2 minutes.",
            flush=True,
        )
        extract_result = bix_schedule._run_server_extract()  # noqa: SLF001
        if extract_result.get("ok"):
            print(
                f"  OK — customers {extract_result.get('customers', '?')}, "
                f"new rows in archive {extract_result.get('new_rows', '?')}"
            )
        else:
            err = extract_result.get("error") or "extract failed"
            print(f"  Skipped: {err}", file=sys.stderr)
            if not history_path.is_file():
                print(
                    "\nNo archive file yet. Fix Bix login (see script header) or place "
                    f"vk_digital_history.db at:\n  {history_path}",
                    file=sys.stderr,
                )
                return 1
            print("  Continuing with existing archive file…")

    peek = bix_history.archive_peek(history_path)
    if not peek["exists"]:
        print(f"\nHistory file not found: {history_path}", file=sys.stderr)
        return 1

    print(
        f"\nArchive: {peek['customers']} Bix customer(s), {peek['txns']} transaction line(s)"
        + (f", {peek['first_at'][:10]} → {peek['last_at'][:10]}" if peek.get("first_at") else "")
    )

    step = "Import" if args.import_only else "[2/2] Import"
    print(f"\n{step} into platform…")

    with transaction() as conn:
        if not args.force:
            mtime = _archive_mtime(history_path)
            last = get_setting(conn, "bix_history_last_archive_mtime", "0") or "0"
            try:
                if float(last) >= mtime:
                    stats = bix_history.imported_stats(conn)
                    print(
                        "  Archive unchanged since last import — nothing to do.\n"
                        f"  In app: {stats['txns']} line(s), {stats['matched']} matched.\n"
                        "  Use --force to run import anyway, or re-extract without --import-only."
                    )
                    return 0
            except ValueError:
                pass

        summary = bix_history.import_archive(conn, history_path, actor="import_bix_history")
        set_setting(conn, "bix_history_last_archive_mtime", str(_archive_mtime(history_path)))
        log_activity(
            conn,
            "bix_history_import",
            (
                f"Bix history script — {summary['txns_new']} new line(s), "
                f"{summary['matched']} matched, {summary['unmatched']} unmatched"
            ),
            actor="import_bix_history",
            meta_json=json.dumps(summary),
        )

    print("\nDone:")
    print(json.dumps(summary, indent=2))
    if summary.get("txns_new"):
        print(f"\n  Added {summary['txns_new']} new history line(s) to the platform.")
    else:
        print("\n  No new lines (already imported).")
    if summary.get("unmatched"):
        print(
            f"\n  {summary['unmatched']} Bix customer(s) not linked — fix phone on VK customers "
            "to match Bix, then run this script again."
        )
    print("\nReminder: history is for lookup only. Net due still comes from Bix CSV align / collections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
