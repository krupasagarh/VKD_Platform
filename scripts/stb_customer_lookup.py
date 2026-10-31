"""Look up VK Platform customers for a list of Hathway STB / VC ids.

Reads ids from a TXT file (one per line; commas/tabs/spaces also split), matches each
against connections.upstream_id (STB, N...) and connections.card_number (VC, T...), and
writes a CSV with the customer's details plus every STB that customer has.

    python scripts/stb_customer_lookup.py --file stb_list.txt
    python scripts/stb_customer_lookup.py N70152400472 N70130897096

Read-only: the database is opened with mode=ro.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT / "data" / "vk_platform.db"

COLUMNS = [
    "input_id",
    "found",
    "matched_on",
    "customer_code",
    "customer_name",
    "phone",
    "alt_phone",
    "area",
    "sub_area",
    "address",
    "customer_status",
    "stb_id",
    "vc_number",
    "stb_status",
    "pack",
    "expiry_date",
    "stb_count",
    "all_stbs",
    "platform_link",
]


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").replace("\ufeff", "")).upper()


def _read_ids(path: Path | None, extra: list[str]) -> list[str]:
    raw: list[str] = []
    if path:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.extend(re.split(r"[,\t;\s]+", line))
    for item in extra:
        raw.extend(re.split(r"[,\t;\s]+", item))
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        key = _norm(item)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Customer details for a list of STB / VC ids")
    parser.add_argument("ids", nargs="*", help="STB / VC ids (optional if --file is given)")
    parser.add_argument("--file", type=Path, help="TXT file with one STB / VC id per line")
    parser.add_argument("--out", type=Path, default=Path("stb_customer_details.csv"))
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    ids = _read_ids(args.file, args.ids)
    if not ids:
        parser.error("give STB ids on the command line or with --file")

    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    hits = conn.execute(
        "SELECT cn.id, cn.customer_id, upper(trim(COALESCE(cn.upstream_id, ''))) AS stb, "
        "       upper(trim(COALESCE(cn.card_number, ''))) AS vc, cn.status, cn.expiry_date, "
        "       COALESCE(NULLIF(cn.hathway_pack_name, ''), NULLIF(cn.upstream_plan_name, ''), p.name, '') AS pack "
        "FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.provider = 'hathway'"
    ).fetchall()
    by_id: dict[str, list[tuple[str, sqlite3.Row]]] = {}
    by_customer: dict[int, list[sqlite3.Row]] = {}
    for row in hits:
        by_customer.setdefault(int(row["customer_id"]), []).append(row)
        if row["stb"]:
            by_id.setdefault(row["stb"], []).append(("STB", row))
        if row["vc"]:
            by_id.setdefault(row["vc"], []).append(("VC", row))

    customers = {
        int(r["id"]): r
        for r in conn.execute(
            "SELECT id, code, name, phone, alt_phone, area, sub_area, address, status FROM customers"
        )
    }

    found = 0
    with args.out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for key in ids:
            matches = by_id.get(key) or []
            if not matches:
                writer.writerow({"input_id": key, "found": "no"})
                print(f"{key}: NOT FOUND")
                continue
            found += 1
            for matched_on, cn in matches:
                cust = customers.get(int(cn["customer_id"]))
                boxes = by_customer.get(int(cn["customer_id"]), [])
                all_stbs = ", ".join(
                    f"{b['stb'] or b['vc']} ({b['status']})" for b in boxes
                )
                record = {
                    "input_id": key,
                    "found": "yes",
                    "matched_on": matched_on,
                    "customer_code": cust["code"] if cust else "",
                    "customer_name": cust["name"] if cust else "",
                    "phone": cust["phone"] if cust else "",
                    "alt_phone": cust["alt_phone"] if cust else "",
                    "area": cust["area"] if cust else "",
                    "sub_area": cust["sub_area"] if cust else "",
                    "address": (cust["address"] or "").replace("\n", " ") if cust else "",
                    "customer_status": cust["status"] if cust else "",
                    "stb_id": cn["stb"],
                    "vc_number": cn["vc"],
                    "stb_status": cn["status"],
                    "pack": cn["pack"],
                    "expiry_date": cn["expiry_date"] or "",
                    "stb_count": len(boxes),
                    "all_stbs": all_stbs,
                    "platform_link": f"https://vkdigital.tipturbroadband.in/customers/{cn['customer_id']}",
                }
                writer.writerow(record)
                print(
                    f"{key}: {record['customer_name']} | {record['phone']} | {record['sub_area'] or record['area']}"
                    f" | {cn['status']} | {len(boxes)} STB(s)"
                )

    print(f"\n{found} of {len(ids)} ids found. CSV: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
