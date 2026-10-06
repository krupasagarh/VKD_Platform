#!/usr/bin/env python3
"""Probe WhatsApp registration for customers and save results on each row."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.db import init_db, transaction
from app import repo
from app.whatsapp_send import check_whatsapp_numbers_batch, close_whatsapp_session


def _normalize_status(probe: dict) -> str:
    status = str(probe.get("status") or "error").strip().lower()
    if status in ("yes", "no", "unknown", "error"):
        return status
    if status == "invalid_phone":
        return "error"
    return "error"


def _status_label(status: str) -> str:
    return {
        "yes": "ON WA",
        "no": "NO WA",
        "unknown": "UNKNOWN",
        "error": "ERROR",
    }.get(status, status.upper())


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe WhatsApp and update customers table")
    parser.add_argument("--limit", type=int, default=0, help="Max customers (0 = all)")
    parser.add_argument("--batch-size", type=int, default=25, help="Numbers per browser session")
    parser.add_argument("--skip-checked", action="store_true", help="Skip rows with whatsapp_status set")
    parser.add_argument("--force", action="store_true", help="Re-check even if already checked")
    parser.add_argument("--out", default="", help="Optional JSON summary path")
    args = parser.parse_args()

    init_db()
    skip_checked = args.skip_checked and not args.force
    batch_size = max(1, int(args.batch_size or 25))

    with transaction() as conn:
        rows = repo.list_customers_for_whatsapp_probe(
            conn,
            skip_checked=skip_checked,
            limit=max(0, int(args.limit or 0)),
        )
    customers = [dict(r) for r in rows]
    if not customers:
        print("No customers to probe.")
        return 0

    print(
        f"Probing {len(customers)} customer(s) in batches of {batch_size}...",
        flush=True,
    )
    print(
        "Stop VK Platform on port 8800 first if WhatsApp profile is locked.",
        flush=True,
    )

    summary = {"total": len(customers), "yes": 0, "no": 0, "unknown": 0, "error": 0}
    results: list[dict] = []
    started = time.time()
    exit_code = 0

    def save_probe(cust: dict, probe: dict) -> str:
        status = _normalize_status(probe)
        with transaction() as conn:
            repo.update_customer_whatsapp_status(conn, int(cust["id"]), status)
        if status in summary:
            summary[status] += 1
        entry = {
            "customer_id": cust["id"],
            "name": cust["name"],
            "phone": cust["phone"],
            "status": status,
            "detail": probe.get("detail") or probe.get("error") or "",
        }
        results.append(entry)
        print(
            f"  {cust['name']} ({cust['phone']}) -> {_status_label(status)}",
            flush=True,
        )
        return status

    try:
        for start in range(0, len(customers), batch_size):
            chunk = customers[start : start + batch_size]
            phones = [c["phone"] for c in chunk]
            batch_no = start // batch_size + 1
            batch_total = (len(customers) + batch_size - 1) // batch_size
            print(
                f"\nBatch {batch_no}/{batch_total} — {len(chunk)} number(s)...",
                flush=True,
            )

            def on_probe(idx: int, _wa_phone: str, probe: dict) -> None:
                save_probe(chunk[idx], probe)

            try:
                check_whatsapp_numbers_batch(phones, on_probe=on_probe)
            except KeyboardInterrupt:
                print("\nInterrupted — progress saved for completed numbers.", flush=True)
                exit_code = 130
                raise
            except Exception as exc:
                print(f"Batch failed: {exc}", flush=True)
                exit_code = 1
                for cust in chunk:
                    already = any(r["customer_id"] == cust["id"] for r in results)
                    if already:
                        continue
                    save_probe(
                        cust,
                        {
                            "status": "error",
                            "error": str(exc),
                            "phone": cust["phone"],
                        },
                    )

            if start + batch_size < len(customers):
                time.sleep(1.5)
    except KeyboardInterrupt:
        pass
    finally:
        close_whatsapp_session()

    elapsed = int(time.time() - started)
    print(f"\nDone in {elapsed // 60}m {elapsed % 60}s")
    print("Summary:", json.dumps(summary, indent=2), flush=True)

    with transaction() as conn:
        counts = repo.whatsapp_status_counts(conn)
    print("DB totals:", json.dumps(counts, indent=2), flush=True)

    payload = {"summary": summary, "db_totals": counts, "results": results}
    out_path = Path(args.out) if args.out else ROOT / "scripts" / "whatsapp_probe_all.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {out_path}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
