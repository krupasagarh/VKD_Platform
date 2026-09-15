"""SmartPlay OTT package catalog and subscriber mapping.

Subscriptions are pulled from portal.smartplaytv.in and attached to an existing
household when the last 10 phone digits match. A second `ott` connection is added
next to ANT IPTV — it never overwrites the IPTV row.
"""
from __future__ import annotations

import re
import sqlite3

from .money import fmt_date, now_iso, parse_date, to_paise

# name, rupees for 30 days, validity days. Price 0 means unknown until collected.
OTT_CATALOG: tuple[tuple[str, str, int], ...] = (
    ("SMARTPLAY GOLD PACK_189", "189", 30),
    ("SMARTPLAY MAGIC PACK 109", "109", 30),
    ("SMARTPLAY MAGIC SPL", "0", 30),
    ("SMARTPLAY GOLD SPL_PB", "0", 30),
    ("SMARTPLAY GOLD PLUS SPL_PB", "0", 30),
    ("SMARTPLAY HUNGAMA E TV SPL_PB", "0", 30),
    ("SSLC GOLD E TV SPL_PB", "0", 30),
    ("Smartplay Magic ManaSky_189", "189", 30),
)


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def infer_pack_rupees(name: str) -> str:
    text = (name or "").strip()
    match = re.search(r"_(\d{2,4})$", text)
    if match:
        return match.group(1)
    match = re.search(r"(?:^|[\s-])(\d{2,4})$", text)
    if match:
        return match.group(1)
    return "0"


def ensure_ott_packages(conn: sqlite3.Connection) -> int:
    """Insert missing SmartPlay packs. Existing rows (price edits) are left alone."""
    stamp = now_iso()
    added = 0
    for name, rupees, days in OTT_CATALOG:
        exists = conn.execute(
            "SELECT id FROM packages WHERE provider = 'ott' AND name = ?",
            (name,),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, active, notes, created_at) "
            "VALUES('ott', ?, ?, ?, 'prepaid', 0, 1, ?, ?)",
            (name, to_paise(rupees), days, "SmartPlay OTT 30-day pack", stamp),
        )
        added += 1
    return added


def ensure_ott_package(conn: sqlite3.Connection, name: str) -> int | None:
    """Return the catalog id for this portal pack, inserting it if needed."""
    pack = (name or "").strip()
    if not pack:
        return None
    row = conn.execute(
        "SELECT id FROM packages WHERE provider = 'ott' AND lower(trim(name)) = lower(trim(?)) LIMIT 1",
        (pack,),
    ).fetchone()
    if row:
        return int(row["id"])
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
        "gst_percentage, active, notes, created_at) "
        "VALUES('ott', ?, ?, 30, 'prepaid', 0, 1, ?, ?)",
        (pack, to_paise(infer_pack_rupees(pack)), "SmartPlay OTT pack from portal", stamp),
    )
    return int(cursor.lastrowid)


def _find_customer_by_phone(conn: sqlite3.Connection, phone: str):
    last10 = _digits(phone)
    if len(last10) != 10:
        return None
    return conn.execute(
        "SELECT * FROM customers WHERE phone LIKE ? OR alt_phone LIKE ? "
        "ORDER BY id LIMIT 1",
        (f"%{last10}%", f"%{last10}%"),
    ).fetchone()


def subscribe_ott(
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
    account_id: str = "",
) -> dict:
    """Create or attach one SmartPlay OTT subscription. Raises ValueError on bad input."""
    name = (name or "").strip()
    pack = (pack or "").strip()
    mobile = _digits(phone)
    if len(mobile) != 10:
        raise ValueError("Enter a 10-digit mobile number.")
    if not name:
        raise ValueError("Enter the subscriber name.")

    stamp = now_iso()
    pkg_id = ensure_ott_package(conn, pack) if pack else None
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
                f"SmartPlay OTT · {(state or 'Karnataka').strip()}",
                stamp,
                stamp,
            ),
        )
        customer_id = int(cursor.lastrowid)
        created_customer = True
    else:
        customer_id = int(customer["id"])

    expiry_iso = fmt_date(parse_date(expiry)) if expiry else ""
    note = "SmartPlay OTT"
    if account_id:
        note = f"SmartPlay OTT acc {account_id}"

    existing = conn.execute(
        "SELECT id FROM connections WHERE provider = 'ott' AND upstream_id = ?",
        (mobile,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE connections SET package_id = COALESCE(?, package_id), "
            "expiry_date = COALESCE(NULLIF(?, ''), expiry_date), "
            "upstream_plan_name = COALESCE(NULLIF(?, ''), upstream_plan_name), "
            "billing_type = 'prepaid', status = 'active', notes = ?, "
            "last_synced_at = ?, updated_at = ? WHERE id = ?",
            (pkg_id, expiry_iso, pack, note, stamp, stamp, existing["id"]),
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
        "notes, last_synced_at, created_at, updated_at) "
        "VALUES(?, 'ott', ?, ?, 'active', 'prepaid', 0, ?, ?, ?, ?, ?, ?)",
        (
            customer_id,
            mobile,
            pkg_id,
            expiry_iso,
            pack,
            note,
            stamp,
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


def sync_smartplay_subscribers(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """Map portal subscribers onto customers by last-10 phone. Empty list is a no-op."""
    ensure_ott_packages(conn)
    created = linked = updated = skipped = 0
    for raw in rows or []:
        phone = _digits(str(raw.get("phone") or raw.get("username") or ""))
        name = (raw.get("name") or "").strip() or phone
        pack = (raw.get("package_name") or raw.get("plan_name") or "").strip()
        if len(phone) != 10:
            skipped += 1
            continue
        existed = _find_customer_by_phone(conn, phone)
        result = subscribe_ott(
            conn,
            name=name,
            phone=phone,
            pack=pack,
            expiry=str(raw.get("expiry") or ""),
            account_id=str(raw.get("account_id") or ""),
        )
        if result["created_customer"]:
            created += 1
        else:
            linked += 1
        if result["updated"] or existed:
            updated += 1
    return {
        "created": created,
        "linked": linked,
        "updated": updated,
        "skipped": skipped,
        "total": len(rows or []),
    }
