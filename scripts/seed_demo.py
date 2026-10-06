"""Seed a separate demo database — does not touch vk_platform.db.

Creates:
  - Login: demo / demo123  (admin role, full access)
  - 100 Railtel + 200 Hathway customers with dummy phones, bills, payments
  - Packages, mixed dues, expiry states, activity

Run:
  python scripts/seed_demo.py --reset
  python run_demo.py

Production DB (your real data) is never opened unless you pass --db explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DEMO_DB = PROJECT_DIR / "data" / "vk_platform_demo.db"

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

DEMO_NOTE = "DEMO seed — safe to delete"
DEMO_AREAS = ("Demo Town", "Sample Nagar", "Showcase Colony", "Trial Layout")
FIRST_NAMES = (
    "Ramesh", "Suresh", "Lakshmi", "Anita", "Rajesh", "Priya", "Kumar", "Deepa",
    "Manjunath", "Sunita", "Vijay", "Meena", "Prakash", "Kavitha", "Girish",
    "Shweta", "Harish", "Nandini", "Basavaraj", "Savita", "Mohan", "Rekha",
)
LAST_NAMES = (
    "Gowda", "Shetty", "Patil", "Reddy", "Naik", "Murthy", "Rao", "Kulkarni",
    "Sharma", "Singh", "Joshi", "Desai", "Bhat", "Hegde", "Kamath",
)
PAYMENT_MODES = ("cash", "upi", "bank")


def _setup_env(db_path: Path) -> None:
    os.environ["VK_PLATFORM_DB"] = str(db_path)
    os.environ["VK_PLATFORM_UPSTREAM_MODE"] = "simulate"
    os.environ["VK_PLATFORM_WORKER_ENABLED"] = "0"
    os.environ.setdefault("VK_PLATFORM_OPERATOR", "Demo Operator")


def _customer_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _ensure_demo_agent(conn, stamp: str) -> int:
    from app.auth import hash_password, permissions_for_role

    row = conn.execute("SELECT id FROM agents WHERE username = 'demo'").fetchone()
    if row:
        return int(row["id"])
    conn.execute(
        "INSERT INTO agents(name, username, password_hash, role, permissions, active, "
        "created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
        (
            "Demo User",
            "demo",
            hash_password("demo123"),
            "admin",
            json.dumps(permissions_for_role("admin")),
            stamp,
            stamp,
        ),
    )
    return int(conn.execute("SELECT id FROM agents WHERE username = 'demo'").fetchone()["id"])


def _ensure_packages(conn, stamp: str) -> dict[str, list[int]]:
    from app.money import to_paise

    catalog = {
        "railtel": [
            ("50Mbps Unlimited", "499", 30),
            ("FUP100Mbps-2Mbps 3.5TB", "699", 30),
            ("Kaveri-100Mbps", "600", 30),
            ("FUP50Mbps-5Mbps 600GB", "449", 30),
        ],
        "hathway": [
            ("HW KA GOLD KANNADA HD 30d", "380", 30),
            ("HW KA SILVER KANNADA HD 30d", "310", 30),
            ("HW KA GOLD TAMIL SD 30d", "320", 30),
            ("ADN All Channels HD", "499", 30),
        ],
    }
    ids: dict[str, list[int]] = {"railtel": [], "hathway": []}
    for provider, plans in catalog.items():
        for name, rupees, days in plans:
            row = conn.execute(
                "SELECT id FROM packages WHERE provider = ? AND name = ?",
                (provider, name),
            ).fetchone()
            if row:
                ids[provider].append(int(row["id"]))
                continue
            gst = 18.0 if provider == "railtel" else 0.0
            billing = "prepaid" if provider == "railtel" else "postpaid"
            pid = conn.execute(
                "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
                "gst_percentage, active, created_at) VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
                (provider, name, to_paise(rupees), days, billing, gst, stamp),
            ).lastrowid
            ids[provider].append(int(pid))
    return ids


def _seed_customer(
    conn,
    *,
    rng: random.Random,
    idx: int,
    provider: str,
    pkg_ids: list[int],
    upstream_id: str,
    card: str,
    stamp: str,
    demo_agent_id: int,
    today: date,
) -> None:
    from app import billing
    from app.db import log_activity
    from app.money import add_days, now_iso, to_paise

    code = f"DEMO-{'RT' if provider == 'railtel' else 'HW'}-{idx:03d}"
    phone = f"9100{idx:06d}"
    name = _customer_name(rng)
    area = rng.choice(DEMO_AREAS)
    pkg_id = rng.choice(pkg_ids)
    pkg = conn.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    amount = int(pkg["price_paise"] or to_paise("499"))
    pkg_name = pkg["name"]

    # Expiry mix for dashboard cards
    roll = idx % 20
    if roll < 3:
        expiry = add_days(today, -rng.randint(1, 14))
    elif roll < 5:
        expiry = add_days(today, rng.randint(0, 5))
    else:
        expiry = add_days(today, rng.randint(10, 45))

    cust_id = conn.execute(
        "INSERT INTO customers(code, name, phone, area, status, notes, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, 'active', ?, ?, ?)",
        (code, name, phone, area, DEMO_NOTE, stamp, stamp),
    ).lastrowid

    conn_id = conn.execute(
        "INSERT INTO connections(customer_id, provider, upstream_id, card_number, package_id, "
        "upstream_plan_name, status, billing_type, amount_paise, expiry_date, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (
            cust_id,
            provider,
            upstream_id,
            card,
            pkg_id,
            pkg_name,
            pkg["billing_type"],
            amount if rng.random() < 0.35 else 0,
            expiry.strftime("%Y-%m-%d"),
            stamp,
            stamp,
        ),
    ).lastrowid

    # 2–3 months of history
    months = rng.randint(2, 3)
    for m in range(months, 0, -1):
        period_end = add_days(today, -30 * (m - 1) - 1)
        period_start = add_days(period_end, -29)
        bill_id = billing.create_bill(
            conn,
            customer_id=cust_id,
            connection_id=conn_id,
            package_id=pkg_id,
            package_name=pkg_name,
            amount_paise=amount,
            period_start=period_start,
            period_end=period_end,
            source="cycle",
            amount_exclusive=(provider == "railtel"),
        )

        scenario = (idx + m) % 6
        if scenario == 0:
            continue  # leave unpaid
        if scenario in (1, 2, 3):
            pay = amount if scenario != 2 else int(amount * 0.6)
            paid_at = f"{period_end.strftime('%Y-%m-%d')} 11:{rng.randint(10,59):02d}:00"
            billing.record_payment(
                conn,
                customer_id=cust_id,
                connection_id=conn_id,
                amount_paise=pay,
                mode=rng.choice(PAYMENT_MODES),
                reference=f"DEMO{idx:04d}{m}",
                collected_agent_id=demo_agent_id,
                paid_at=paid_at,
                notes="Demo payment",
            )
        elif scenario == 4 and m == 1:
            billing.record_payment(
                conn,
                customer_id=cust_id,
                connection_id=conn_id,
                amount_paise=amount + to_paise(rng.choice(["50", "100", "150"])),
                mode="upi",
                collected_agent_id=demo_agent_id,
                paid_at=f"{today.strftime('%Y-%m-%d')} 09:30:00",
                notes="Demo advance",
            )

    if idx % 17 == 0:
        billing.create_bill(
            conn,
            customer_id=cust_id,
            connection_id=conn_id,
            package_id=pkg_id,
            package_name=pkg_name,
            amount_paise=amount,
            period_start=today,
            period_end=add_days(today, 29),
            source="manual",
            collect_later=True,
            followup_kind="renew",
            notes="Demo — renewed, not paid",
        )

    billing.reconcile_customer(conn, cust_id)
    if idx % 25 == 0:
        log_activity(
            conn,
            "demo_seed",
            f"Demo customer {name} seeded",
            customer_id=cust_id,
            actor="demo",
        )


def seed_demo_db(db_path: Path, *, reset: bool) -> dict:
    if db_path.resolve() != DEFAULT_DEMO_DB.resolve():
        raise SystemExit(
            f"Refusing to seed {db_path} — pass only the demo DB path:\n  {DEFAULT_DEMO_DB}"
        )

    if reset and db_path.is_file():
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    _setup_env(db_path)

    from app.db import init_db, log_activity, transaction
    from app.money import now_iso, today

    init_db()
    stamp = now_iso()
    today = today()
    rng = random.Random(42)

    with transaction() as conn:
        demo_agent_id = _ensure_demo_agent(conn, stamp)
        pkg_ids = _ensure_packages(conn, stamp)

        for i in range(1, 101):
            _seed_customer(
                conn,
                rng=rng,
                idx=i,
                provider="railtel",
                pkg_ids=pkg_ids["railtel"],
                upstream_id=f"ka.demo{i:03d}",
                card="",
                stamp=stamp,
                demo_agent_id=demo_agent_id,
                today=today,
            )

        for i in range(1, 201):
            stb = f"N7018888{i:04d}"
            card = f"T8888{i:06d}"
            _seed_customer(
                conn,
                rng=rng,
                idx=i,
                provider="hathway",
                pkg_ids=pkg_ids["hathway"],
                upstream_id=stb,
                card=card,
                stamp=stamp,
                demo_agent_id=demo_agent_id,
                today=today,
            )

        stats = {
            "customers": conn.execute(
                "SELECT COUNT(*) AS n FROM customers WHERE notes = ?", (DEMO_NOTE,)
            ).fetchone()["n"],
            "connections": conn.execute(
                "SELECT COUNT(*) AS n FROM connections cn JOIN customers c ON c.id = cn.customer_id "
                "WHERE c.notes = ?",
                (DEMO_NOTE,),
            ).fetchone()["n"],
            "bills": conn.execute("SELECT COUNT(*) AS n FROM bills").fetchone()["n"],
            "payments": conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"],
        }
        log_activity(conn, "demo_seed", f"Demo database ready — {stats['customers']} customers", actor="demo")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed isolated VK Platform demo database")
    parser.add_argument("--reset", action="store_true", help="Delete demo DB and recreate")
    parser.add_argument("--db", type=Path, default=DEFAULT_DEMO_DB, help="Demo DB path (default only)")
    args = parser.parse_args()

    stats = seed_demo_db(args.db, reset=args.reset)
    print(f"Demo database: {args.db}")
    print(f"  Customers : {stats['customers']}")
    print(f"  Connections: {stats['connections']}")
    print(f"  Bills     : {stats['bills']}")
    print(f"  Payments  : {stats['payments']}")
    print()
    print("Login:  demo / demo123")
    print("Start:  python run_demo.py     (port 8801 — live app stays on 8800)")
    print("URL:    http://127.0.0.1:8801/  (or http://YOUR-LAN-IP:8801/)")
    print()
    print("Your production database (vk_platform.db) was NOT modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
