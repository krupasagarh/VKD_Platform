#!/usr/bin/env python3
"""Import Railtel Subscribers-report CSV into VK Platform."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import init_db, log_activity, transaction  # noqa: E402
from app.money import fmt_date, now_iso, parse_date, today  # noqa: E402
from app.railtel_sync import import_portal_subscribers  # noqa: E402
from app.upstream.providers import RAILTEL_ID_RE  # noqa: E402

ACCOUNT_LABELS = {
    "default": "VK DIGITAL",
    "kalpataru": "KALPATARU FIBER NET",
    "speedfirst": "SPEEDFIRST BROADBAND",
}


def _norm_phone(value: str) -> str:
    import re

    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[-10:]
    return digits if len(digits) == 10 else ""


def _read_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    text = path.read_text(encoding="utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return rows

    handle = io.StringIO("\n".join(lines))
    reader = csv.DictReader(handle)
    if not reader.fieldnames:
        return rows
    reader.fieldnames = [(h or "").strip() for h in reader.fieldnames]
    for raw in reader:
        row = {
            (k or "").strip(): (v or "").strip() if v is not None else ""
            for k, v in raw.items()
            if k
        }
        if row.get("username"):
            rows.append(row)
    return rows


def _csv_to_portal_rows(rows: list[dict]) -> list[dict]:
    ref = today()
    out: list[dict] = []
    for row in rows:
        username = (row.get("username") or "").strip()
        if not username:
            continue
        if not RAILTEL_ID_RE.match(username) and not username.replace("_", "").replace(".", "").isalnum():
            # Keep non ka.* logins (e.g. SJC_Kishor) — Railtel allows various username shapes.
            pass
        expiry_raw = (row.get("expiry") or "").strip()
        expiry_dt = None
        if expiry_raw:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    expiry_dt = datetime.strptime(expiry_raw[:19], fmt).date()
                    break
                except ValueError:
                    continue
        sub_status = (row.get("sub_status") or "").strip().lower()
        status_flag = (row.get("status") or "").strip()
        is_red = sub_status == "inactive" or status_flag == "0"
        if expiry_dt is not None and expiry_dt < ref:
            is_red = True
        package = (row.get("packagename") or "").strip()
        # CSV expiry is the monthly cycle; x3/x6/x10/x12 term end only comes from Subscriber Details.
        is_term = bool(re.search(r"\sx(3|6|10|12)\b", package, re.I))
        first = (row.get("firstname") or "").strip()
        last = (row.get("lastname") or "").strip()
        name = " ".join(x for x in (first, last) if x).strip()
        out.append(
            {
                "username": username,
                "name": name or username,
                "mobile": _norm_phone(row.get("mobileno") or ""),
                "email": (row.get("email") or "").strip(),
                "address": (row.get("address") or "").strip(),
                "package": package,
                "subscriber_id": str(row.get("subscriberid") or "").strip(),
                "renewal_date": fmt_date(expiry_dt) if expiry_dt else "",
                "subscription_expiry": fmt_date(expiry_dt) if expiry_dt and not is_term else "",
                "status": "inactive" if is_red else "active",
                "is_red": is_red,
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Railtel Subscribers-report CSV")
    parser.add_argument("csv", type=Path, help="Path to Subscribers-report CSV")
    parser.add_argument(
        "--account",
        required=True,
        choices=sorted(ACCOUNT_LABELS.keys()),
        help="Railtel dealer account id (railwire_accounts.json)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = args.csv.expanduser().resolve()
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    raw_rows = _read_csv(path)
    portal_rows = _csv_to_portal_rows(raw_rows)
    if not portal_rows:
        print("No subscriber rows found in CSV.", file=sys.stderr)
        return 1

    label = ACCOUNT_LABELS.get(args.account, args.account)
    print(f"CSV: {path.name} — {len(portal_rows)} subscriber(s) → {label} ({args.account})")

    if args.dry_run:
        print(json.dumps({"rows": len(portal_rows), "sample": portal_rows[:3]}, indent=2))
        return 0

    init_db()
    stamp = now_iso()
    with transaction() as conn:
        stats = import_portal_subscribers(
            conn,
            portal_rows,
            stamp,
            portal_account_id=args.account,
        )
        log_activity(
            conn,
            "railtel_csv_import",
            f"CSV import for {label}: {stats.get('created_connections', 0)} new, "
            f"{stats.get('updated', 0)} updated, {stats.get('matched', 0)} matched",
            meta_json=json.dumps({"account_id": args.account, "csv": str(path), **stats}),
        )

    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
