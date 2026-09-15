"""Load the existing VK Digital data into VK Platform.

Sources (all produced by cableway_automation/data_sync):

    railtel_plans_catalog.csv          -> Railtel plans (term price + validity computed)
    cableway_packages_hathway_only.csv -> Hathway TV bouquets
    cableway_hathway_generated_v2.csv  -> Hathway customers + their STBs, grouped by customer code
    railtel_customers.csv              -> Railtel broadband accounts

A Railtel account is attached to the Hathway customer with the same phone number
when there is one, so a household that takes both TV and broadband becomes a
single customer with two connections instead of two separate records.

Usage:
    python scripts/import_seed_data.py --reset
    python scripts/import_seed_data.py --dry-run
    python scripts/import_seed_data.py --skip-balances
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import billing  # noqa: E402
from app.config import CABLEWAY_DATA_DIR  # noqa: E402
from app.db import init_db, log_activity, transaction  # noqa: E402
from app.money import fmt_date, now_iso, parse_date, to_paise  # noqa: E402
from app.plans import price_plan, validity_from_name  # noqa: E402
from app.upstream.providers import RAILTEL_ID_RE  # noqa: E402

RAILTEL_CATALOG = "railtel_plans_catalog.csv"
HATHWAY_PACKAGES = "cableway_packages_hathway_only.csv"
HATHWAY_CUSTOMERS = "cableway_hathway_generated_v2.csv"
RAILTEL_CUSTOMERS = "railtel_customers.csv"
PLANS_EXPORT = "cableway_plans_export.csv"

# provider_category in the CableWay plan export -> our provider key
EXPORT_PROVIDERS = {"railwire": "railtel", "hathway connect": "hathway"}

TABLES_TO_RESET = (
    "bill_payments",
    "payments",
    "bills",
    "upstream_jobs",
    "connections",
    "customers",
    "packages",
    "activity_log",
)


def read_csv(path: Path) -> list[dict]:
    """Read a CSV, tolerating a BOM, leading blank lines and padded headers."""
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle if line.strip()]
    if not lines:
        return []
    reader = csv.DictReader(lines)
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items()})
    return rows


def norm_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #

def import_packages(conn, data_dir: Path) -> dict:
    stamp = now_iso()
    counts = {"railtel": 0, "hathway": 0}

    for row in read_csv(data_dir / RAILTEL_CATALOG):
        name = row.get("Plan Name") or ""
        if not name:
            continue
        pricing = price_plan(name, row.get("Amount") or "0")
        conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, active, notes, created_at) "
            "VALUES('railtel', ?, ?, ?, 'prepaid', 18, 1, ?, ?) "
            "ON CONFLICT(provider, name) DO UPDATE SET "
            "price_paise = excluded.price_paise, validity_days = excluded.validity_days, "
            "gst_percentage = excluded.gst_percentage, "
            "notes = excluded.notes",
            (name, pricing["price_paise"], pricing["validity_days"], pricing["description"], stamp),
        )
        counts["railtel"] += 1

    for row in read_csv(data_dir / HATHWAY_PACKAGES):
        name = row.get("name") or ""
        if not name:
            continue
        try:
            gst = float(row.get("gst_percentage") or 0)
        except ValueError:
            gst = 0.0
        channels = row.get("channel_count") or ""
        conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, channel_count, active, notes, created_at) "
            "VALUES('hathway', ?, ?, 30, 'postpaid', ?, ?, 1, ?, ?) "
            "ON CONFLICT(provider, name) DO UPDATE SET "
            "price_paise = excluded.price_paise, gst_percentage = excluded.gst_percentage",
            (
                name,
                to_paise(row.get("monthly_price") or "0"),
                gst,
                int(channels) if channels.isdigit() else None,
                row.get("description") or "",
                stamp,
            ),
        )
        counts["hathway"] += 1

    counts["from_plan_export"] = import_plans_export(conn, data_dir)
    return counts


def import_plans_export(conn, data_dir: Path) -> dict:
    """Fill gaps from the plan catalog exported out of CableWay.

    Its prices are already term totals, so they are taken as-is; only the validity
    is derived from the plan name. Existing rows win, because the Railtel catalog
    import above computes term pricing from first principles.
    """
    stamp = now_iso()
    added = {"railtel": 0, "hathway": 0}

    for row in read_csv(data_dir / PLANS_EXPORT):
        provider = EXPORT_PROVIDERS.get((row.get("provider_category") or "").strip().lower())
        name = (row.get("plan_name") or "").strip()
        if not provider or not name:
            continue
        price_paise = to_paise(row.get("monthly_price") or "0")
        if price_paise <= 0:
            continue
        try:
            gst = float(row.get("gst_percentage") or 0)
        except ValueError:
            gst = 0.0

        cursor = conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, upstream_plan_code, active, notes, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(provider, name) DO NOTHING",
            (
                provider,
                name,
                price_paise,
                validity_from_name(name) if provider == "railtel" else 30,
                "prepaid" if provider == "railtel" else "postpaid",
                gst,
                (row.get("plan_code") or "").strip() or None,
                "From CableWay plan export",
                stamp,
            ),
        )
        if cursor.rowcount:
            added[provider] += 1

    return added


def package_index(conn) -> dict[tuple[str, str], int]:
    rows = conn.execute("SELECT id, provider, name FROM packages").fetchall()
    return {(r["provider"], (r["name"] or "").strip().lower()): int(r["id"]) for r in rows}


# --------------------------------------------------------------------------- #
# Hathway customers + STBs
# --------------------------------------------------------------------------- #

def import_hathway(conn, data_dir: Path, packages: dict) -> dict:
    rows = read_csv(data_dir / HATHWAY_CUSTOMERS)
    stamp = now_iso()

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        stb = (row.get("settop_box_number") or "").strip()
        if not stb:
            continue
        key = (row.get("customer_id") or "").strip() or f"stb:{stb}"
        grouped.setdefault(key, []).append(row)

    customers = 0
    misrouted = 0
    connections = 0
    skipped_stbs = 0
    opening: list[tuple[int, int, str]] = []
    phone_to_customer: dict[str, int] = {}

    for code, group in grouped.items():
        first = group[0]
        name = first.get("name") or code
        phone = norm_phone(first.get("phone") or "")

        cursor = conn.execute(
            "INSERT INTO customers(code, name, phone, alt_phone, address, area, sub_area, "
            "pincode, status, notes, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                code if not code.startswith("stb:") else None,
                name,
                phone,
                phone,
                first.get("address") or "",
                first.get("area_name") or "Tiptur",
                first.get("sub_area_name") or "",
                first.get("pincode") or "572201",
                (first.get("connection_status") or "active").strip().lower() or "active",
                "Imported from Bix + Hathway report",
                stamp,
                stamp,
            ),
        )
        customer_id = int(cursor.lastrowid)
        customers += 1
        if phone:
            phone_to_customer.setdefault(phone, customer_id)

        balance = sum(to_paise(r.get("balance_amount") or "0") for r in group)
        if balance > 0:
            opening.append((customer_id, balance, name))

        for row in group:
            stb = (row.get("settop_box_number") or "").strip()
            plan_name = (row.get("package_name") or "").strip()
            # The Bix export keeps a handful of Railtel broadband logins in the set-top
            # box column. File those against the provider that can actually serve them,
            # otherwise portal actions would drive the wrong portal.
            provider = "railtel" if RAILTEL_ID_RE.match(stb) else "hathway"
            if provider == "railtel":
                misrouted += 1
            package_id = packages.get((provider, plan_name.lower()))
            try:
                conn.execute(
                    "INSERT INTO connections(customer_id, provider, upstream_id, card_number, "
                    "package_id, status, billing_type, amount_paise, expiry_date, "
                    "upstream_plan_name, notes, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        customer_id,
                        provider,
                        stb,
                        (row.get("card_number") or "").strip(),
                        package_id,
                        (row.get("connection_status") or "active").strip().lower() or "active",
                        billing.billing_type_for(provider),
                        to_paise(row.get("plan_amount") or "0"),
                        fmt_date(parse_date(row.get("expiry_date"))),
                        plan_name,
                        row.get("notes") or "",
                        stamp,
                        stamp,
                    ),
                )
                connections += 1
            except Exception:  # duplicate STB across source rows
                skipped_stbs += 1

    return {
        "customers": customers,
        "connections": connections,
        "railtel_ids_rerouted": misrouted,
        "duplicate_stbs_skipped": skipped_stbs,
        "opening_balances": opening,
        "phone_to_customer": phone_to_customer,
    }


# --------------------------------------------------------------------------- #
# Railtel broadband accounts
# --------------------------------------------------------------------------- #

def import_railtel(conn, data_dir: Path, packages: dict, phone_to_customer: dict[str, int]) -> dict:
    rows = read_csv(data_dir / RAILTEL_CUSTOMERS)
    stamp = now_iso()

    attached = 0
    created = 0
    connections = 0
    skipped = 0

    for row in rows:
        internet_id = (row.get("internet_id") or "").strip()
        if not internet_id:
            skipped += 1
            continue

        phone = norm_phone(row.get("phone_normalized") or "")
        customer_id = phone_to_customer.get(phone) if phone else None

        if customer_id:
            attached += 1
        else:
            cursor = conn.execute(
                "INSERT INTO customers(code, name, phone, alt_phone, email, address, area, "
                "pincode, status, notes, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'Tiptur', '572201', ?, ?, ?, ?)",
                (
                    internet_id,
                    row.get("customer_name") or internet_id,
                    phone,
                    phone,
                    row.get("email") or "",
                    row.get("address") or "",
                    (row.get("status") or "active").strip().lower() or "active",
                    "Imported from Railtel subscriber report",
                    stamp,
                    stamp,
                ),
            )
            customer_id = int(cursor.lastrowid)
            created += 1
            if phone:
                phone_to_customer.setdefault(phone, customer_id)

        plan_name = (row.get("plan_name") or "").strip()
        package_id = packages.get(("railtel", plan_name.lower()))
        # Railtel is billed straight off the plan price; no per-customer override on import.
        amount = 0
        try:
            conn.execute(
                "INSERT INTO connections(customer_id, provider, upstream_id, package_id, status, "
                "billing_type, amount_paise, expiry_date, upstream_plan_name, notes, "
                "created_at, updated_at) "
                "VALUES(?, 'railtel', ?, ?, ?, 'prepaid', ?, ?, ?, ?, ?, ?)",
                (
                    customer_id,
                    internet_id,
                    package_id,
                    (row.get("status") or "active").strip().lower() or "active",
                    amount,
                    fmt_date(parse_date(row.get("expiry_date"))),
                    plan_name,
                    f"Railtel subscriber {row.get('subscriber_id') or ''}".strip(),
                    stamp,
                    stamp,
                ),
            )
            connections += 1
        except Exception:
            skipped += 1

    return {
        "attached_to_existing_customer": attached,
        "new_customers": created,
        "connections": connections,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------- #
# Opening balances
# --------------------------------------------------------------------------- #

def import_opening_balances(conn, opening: list[tuple[int, int, str]]) -> dict:
    total = 0
    for customer_id, amount_paise, _name in opening:
        billing.create_bill(
            conn,
            customer_id=customer_id,
            connection_id=None,
            package_id=None,
            package_name="Opening balance (carried from Bix)",
            amount_paise=amount_paise,
            period_start=None,
            period_end=None,
            source="opening",
            gst_percentage=0,
        )
        billing.reconcile_customer(conn, customer_id)
        total += amount_paise
    return {"customers": len(opening), "amount_paise": total}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Seed VK Platform from the existing CSV exports")
    parser.add_argument("--data-dir", default=str(CABLEWAY_DATA_DIR))
    parser.add_argument("--reset", action="store_true", help="empty all tables first")
    parser.add_argument("--skip-balances", action="store_true", help="do not create opening-balance bills")
    parser.add_argument("--dry-run", action="store_true", help="roll back instead of committing")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        raise SystemExit(f"Data directory not found: {data_dir}")

    init_db()
    summary: dict = {"data_dir": str(data_dir)}

    class Rollback(Exception):
        pass

    try:
        with transaction() as conn:
            if args.reset:
                for table in TABLES_TO_RESET:
                    conn.execute(f"DELETE FROM {table}")
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('customers','connections','packages','bills','payments','upstream_jobs',"
                    " 'bill_payments','activity_log')"
                )
                summary["reset"] = True

            summary["packages"] = import_packages(conn, data_dir)
            packages = package_index(conn)

            hathway = import_hathway(conn, data_dir, packages)
            phone_map = hathway.pop("phone_to_customer")
            opening = hathway.pop("opening_balances")
            summary["hathway"] = hathway

            summary["railtel"] = import_railtel(conn, data_dir, packages, phone_map)

            if args.skip_balances:
                summary["opening_balances"] = "skipped"
            else:
                summary["opening_balances"] = import_opening_balances(conn, opening)

            summary["totals"] = {
                "customers": conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"],
                "connections": conn.execute("SELECT COUNT(*) AS n FROM connections").fetchone()["n"],
                "packages": conn.execute("SELECT COUNT(*) AS n FROM packages").fetchone()["n"],
                "bills": conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()["n"],
            }
            log_activity(
                conn,
                "seed_import",
                f"Imported {summary['totals']['customers']} customers and "
                f"{summary['totals']['connections']} connections from CSV exports",
            )

            if args.dry_run:
                raise Rollback
    except Rollback:
        summary["dry_run"] = "rolled back, nothing saved"

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
