#!/usr/bin/env python3
"""Fetch My Subscribers from Railtel dealer accounts and import into VK Platform."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(WORKSPACE / "railtel_debugger" / "vk_agent"))
sys.path.insert(0, str(WORKSPACE / "railtel_debugger"))

from app.db import init_db, log_activity, transaction  # noqa: E402
from app.money import now_iso, today  # noqa: E402
from app.railtel_sync import import_portal_subscribers, summarize_subscriber_rows  # noqa: E402
from app.upstream.providers import _load  # noqa: E402

ACCOUNT_LABELS = {
    "kalpataru": "KALPATARU FIBER NET",
    "speedfirst": "SPEEDFIRST BROADBAND",
    "default": "VK DIGITAL",
}


def fetch_account(account_id: str) -> dict:
    check = _load("portal", "check_railtel_sync_subscribers")
    print(f"\n=== Fetching {ACCOUNT_LABELS.get(account_id, account_id)} ({account_id}) ===", flush=True)
    result = check(account_id=account_id)
    if not result.get("success"):
        return {
            "account_id": account_id,
            "ok": False,
            "error": result.get("error") or "Portal fetch failed",
            "rows": [],
        }
    rows = list(result.get("subscribers") or [])
    summary = summarize_subscriber_rows(rows, on_date=today())
    print(
        f"  {len(rows)} subscriber(s) — active {summary['active']}, expired {summary['expired']}",
        flush=True,
    )
    return {
        "account_id": account_id,
        "ok": True,
        "message": result.get("message") or "",
        "rows": rows,
        "summary": summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Railtel My Subscribers for dealer accounts")
    parser.add_argument(
        "--accounts",
        default="kalpataru,speedfirst",
        help="Comma-separated account ids from railwire_accounts.json",
    )
    parser.add_argument("--out-dir", default=str(ROOT / "scripts" / "railtel_imports"))
    args = parser.parse_args()

    account_ids = [a.strip() for a in args.accounts.split(",") if a.strip()]
    if not account_ids:
        print("No accounts specified.", file=sys.stderr)
        return 1

    init_db()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso()
    report = {"fetched_at": stamp, "accounts": []}

    for account_id in account_ids:
        fetched = fetch_account(account_id)
        payload_path = out_dir / f"{account_id}_mysubscribers.json"
        payload_path.write_text(
            json.dumps(
                {
                    "account_id": account_id,
                    "fetched_at": stamp,
                    "ok": fetched["ok"],
                    "error": fetched.get("error"),
                    "summary": fetched.get("summary"),
                    "subscribers": fetched.get("rows") or [],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"  Saved scrape: {payload_path}", flush=True)

        import_stats = None
        if fetched["ok"] and fetched.get("rows"):
            with transaction() as conn:
                import_stats = import_portal_subscribers(
                    conn,
                    fetched["rows"],
                    stamp,
                    portal_account_id=account_id,
                )
                log_activity(
                    conn,
                    "railtel_import",
                    f"Imported {import_stats.get('created_connections', 0)} new Railtel connection(s) "
                    f"from {ACCOUNT_LABELS.get(account_id, account_id)} "
                    f"({import_stats.get('updated', 0)} updated)",
                    meta_json=json.dumps({"account_id": account_id, **import_stats}),
                )
            print(f"  Import: {json.dumps(import_stats)}", flush=True)

        report["accounts"].append({**fetched, "import": import_stats, "json_path": str(payload_path)})

    report_path = out_dir / "import_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport: {report_path}", flush=True)

    failed = [a for a in report["accounts"] if not a.get("ok")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
