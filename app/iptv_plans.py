"""ANT IPTV package catalog and the current CRM subscription list.

Packs come from the IPTV Packages tab. Subscriptions come from My Subscriptions
and are attached to an existing household when the mobile matches, otherwise a
customer row is created so the IPTV page can track them.
"""
from __future__ import annotations

import re
import sqlite3

from .money import now_iso, to_paise

# name, rupees for 30 days, validity days
IPTV_CATALOG: tuple[tuple[str, str, int], ...] = (
    ("ADN All Channels HD", "250", 30),
    ("ADN All South HD", "177", 30),
    ("ADN All South SD", "154", 30),
    ("ADN Bengali Hindi HD", "142", 30),
    ("ADN Bengali Hindi SD", "142", 30),
    ("ADN Gujarathi Hindi HD", "166", 30),
    ("ADN Gujarathi Hindi SD", "142", 30),
    ("ADN Hindi HD", "142", 30),
    ("ADN Hindi SD", "117", 30),
    ("ADN Kannada HD", "142", 30),
    ("ADN Kannada Marathi HD", "166", 30),
    ("ADN Kannada Marathi SD", "142", 30),
    ("ADN Kannada SD", "117", 30),
    ("ADN Malayalam HD", "142", 30),
    ("ADN Malayalam SD", "117", 30),
    ("ADN Marathi Hindi HD", "166", 30),
    ("ADN Marathi Hindi SD", "142", 30),
    ("ADN Tamil HD", "142", 30),
    ("ADN Tamil SD", "117", 30),
    ("ADN Telugu HD", "142", 30),
    ("ADN Telugu Hindi HD", "166", 30),
    ("ADN Telugu Hindi SD", "142", 30),
    ("ADN Telugu SD", "117", 30),
    ("ALL FTA", "30", 30),
)

# name, mobile, pack, expiry YYYY-MM-DD
IPTV_SUBSCRIPTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("Lohith", "8884274803", "ADN Kannada SD", "2026-10-07"),
    ("Mahesh aruna", "9448326899", "ADN Kannada SD", "2026-10-07"),
    ("vijay kumar bv aruna", "9738850200", "ADN Kannada SD", "2026-10-07"),
    ("Anapurna aruna", "9482331367", "ADN Kannada SD", "2026-10-06"),
    ("Avinash", "9844001270", "ADN Kannada SD", "2026-10-06"),
    ("Honnarajayya", "9448979501", "ADN Kannada SD", "2026-10-06"),
    ("Dhanvik_aruna", "8105595138", "ADN Kannada SD", "2026-10-06"),
    ("Rekha", "8197161453", "ADN Kannada SD", "2026-09-16"),
    ("Shashi flower", "9916644439", "ADN Kannada SD", "2026-09-14"),
    ("Giriraj fertilizer prathibha", "8951885933", "ADN Kannada HD", "2026-10-04"),
    ("Ramya Ramu halepalya", "9141355097", "ADN Kannada SD", "2026-10-04"),
    ("Harsha byrate", "9741627555", "ADN All Channels HD", "2026-10-03"),
    ("Varsha_behind-tvs", "7483574613", "ADN Kannada SD", "2026-10-03"),
    ("Darshan", "9886526600", "ADN Kannada SD", "2026-09-29"),
    ("Halepalya Club", "9844929932", "ADN Kannada HD", "2026-09-24"),
    ("Raghu powerline road", "9902445382", "ADN Kannada HD", "2026-09-23"),
    ("Manjushree", "9738781278", "ADN Kannada SD", "2026-09-16"),
    ("Pavan Svp", "9535395424", "ADN Kannada HD", "2026-09-16"),
    ("Krupa-Bangalore Home", "9019563840", "ADN All Channels HD", "2026-09-12"),
    ("Harish Ganesh Theater", "9019469566", "ADN Kannada SD", "2026-09-11"),
)


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def ensure_iptv_packages(conn: sqlite3.Connection) -> int:
    """Insert missing ANT packs. Existing rows (price edits) are left alone."""
    stamp = now_iso()
    added = 0
    for name, rupees, days in IPTV_CATALOG:
        exists = conn.execute(
            "SELECT id FROM packages WHERE provider = 'iptv' AND name = ?",
            (name,),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, active, notes, created_at) "
            "VALUES('iptv', ?, ?, ?, 'prepaid', 0, 1, ?, ?)",
            (name, to_paise(rupees), days, "ANT IPTV 30-day pack", stamp),
        )
        added += 1
    return added


def _find_customer_by_phone(conn: sqlite3.Connection, phone: str):
    last10 = _digits(phone)
    if len(last10) != 10:
        return None
    return conn.execute(
        "SELECT * FROM customers WHERE phone LIKE ? OR alt_phone LIKE ? "
        "ORDER BY id LIMIT 1",
        (f"%{last10}%", f"%{last10}%"),
    ).fetchone()


def _package_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM packages WHERE provider = 'iptv' AND lower(name) = lower(?) LIMIT 1",
        (name,),
    ).fetchone()
    return int(row["id"]) if row else None


def subscribe_iptv(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    pack: str,
    expiry: str = "",
    address: str = "",
    city: str = "Tiptur",
    state: str = "Karnataka",
    pincode: str = "572201",
) -> dict:
    """Create or attach one ANT IPTV subscription. Raises ValueError on bad input."""
    name = (name or "").strip()
    pack = (pack or "").strip()
    mobile = _digits(phone)
    if len(mobile) != 10:
        raise ValueError("Enter a 10-digit mobile number.")
    if not name:
        raise ValueError("Enter the subscriber name.")
    if not pack:
        raise ValueError("Pick an IPTV package.")
    pkg_id = _package_id(conn, pack)
    if pkg_id is None:
        raise ValueError(f"No IPTV pack is named '{pack}'. Add it under Plans first.")

    stamp = now_iso()
    customer = _find_customer_by_phone(conn, mobile)
    created_customer = False
    if customer is None:
        cursor = conn.execute(
            "INSERT INTO customers(name, phone, address, area, pincode, status, notes, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                name,
                mobile,
                (address or "").strip(),
                (city or "Tiptur").strip() or "Tiptur",
                (pincode or "").strip() or "572201",
                f"ANT IPTV · {(state or 'Karnataka').strip()}",
                stamp,
                stamp,
            ),
        )
        customer_id = int(cursor.lastrowid)
        created_customer = True
    else:
        customer_id = int(customer["id"])
        if address or city or pincode:
            conn.execute(
                "UPDATE customers SET address = COALESCE(NULLIF(?, ''), address), "
                "area = COALESCE(NULLIF(?, ''), area), "
                "pincode = COALESCE(NULLIF(?, ''), pincode), updated_at = ? WHERE id = ?",
                (
                    (address or "").strip(),
                    (city or "").strip(),
                    (pincode or "").strip(),
                    stamp,
                    customer_id,
                ),
            )

    existing = conn.execute(
        "SELECT id FROM connections WHERE provider = 'iptv' AND upstream_id = ?",
        (mobile,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE connections SET package_id = ?, expiry_date = COALESCE(NULLIF(?, ''), expiry_date), "
            "upstream_plan_name = ?, billing_type = 'prepaid', status = 'active', updated_at = ? "
            "WHERE id = ?",
            (pkg_id, (expiry or "").strip(), pack, stamp, existing["id"]),
        )
        return {
            "customer_id": customer_id,
            "connection_id": int(existing["id"]),
            "created_customer": created_customer,
            "updated": True,
        }

    cursor = conn.execute(
        "INSERT INTO connections(customer_id, provider, upstream_id, package_id, "
        "status, billing_type, amount_paise, expiry_date, upstream_plan_name, "
        "notes, created_at, updated_at) "
        "VALUES(?, 'iptv', ?, ?, 'active', 'prepaid', 0, ?, ?, ?, ?, ?)",
        (
            customer_id,
            mobile,
            pkg_id,
            (expiry or "").strip(),
            pack,
            "ANT IPTV subscription",
            stamp,
            stamp,
        ),
    )
    return {
        "customer_id": customer_id,
        "connection_id": int(cursor.lastrowid),
        "created_customer": created_customer,
        "updated": False,
    }


def ensure_iptv_subscriptions(conn: sqlite3.Connection) -> dict:
    """Attach the ANT CRM list. Skipped on empty/smoke databases."""
    households = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
    if int(households or 0) < 10:
        return {"created": 0, "linked": 0, "updated": 0, "skipped": "empty-db"}

    created = linked = updated = 0
    for name, mobile, pack, expiry in IPTV_SUBSCRIPTIONS:
        existed = _find_customer_by_phone(conn, mobile)
        result = subscribe_iptv(conn, name=name, phone=mobile, pack=pack, expiry=expiry)
        if result["created_customer"]:
            created += 1
        else:
            linked += 1
        if result["updated"] or existed:
            updated += 1
    return {"created": created, "linked": linked, "updated": updated}
