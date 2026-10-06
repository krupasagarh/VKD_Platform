"""Field inventory: orders in, stock on hand, technician use.

Admin records what was ordered and paid. Agents see counts and log when they
use a piece — new connection or a faulty replacement (with the customer).
On-hand is receipts minus use; it is meant to be a rough running total, not a
warehouse audit.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .db import log_activity
from .money import fmt_date, fmt_rupees, now_iso, parse_date, to_paise, today

KIND_NEW = "new_connection"
KIND_FAULTY = "faulty"
USAGE_KINDS = (
    (KIND_NEW, "New connection"),
    (KIND_FAULTY, "Faulty replacement"),
)
KIND_LABELS = dict(USAGE_KINDS)

UNITS = ("pcs", "m", "km", "rolls")
UNIT_LABELS = {"pcs": "pcs", "m": "m", "km": "km", "rolls": "rolls"}

# code, name, category, unit, provider_scope
CATALOG = (
    ("stb_hathway", "New STB (Hathway)", "Hathway", "pcs", "hathway"),
    ("coax", "Wire coaxial", "Hathway", "m", "hathway"),
    ("ont_dragonpath", "ONT Dragonpath", "Railtel ONT", "pcs", "railtel"),
    ("ont_tplink", "ONT New TP-Link", "Railtel ONT", "pcs", "railtel"),
    ("ont_new", "ONT New (Railtel)", "Railtel ONT", "pcs", "railtel"),
    ("fiber_4f", "Optical fiber 4F", "Fiber", "km", "railtel"),
    ("fiber_6f", "Optical fiber 6F", "Fiber", "km", "railtel"),
    ("coupler_50", "Coupler 50:50", "Couplers", "pcs", "railtel"),
    ("coupler_60", "Coupler 60:40", "Couplers", "pcs", "railtel"),
    ("coupler_70", "Coupler 70:30", "Couplers", "pcs", "railtel"),
    ("patch_blue", "Patch cord Blue", "Patch cords", "pcs", "railtel"),
    ("patch_green", "Patch cord Green", "Patch cords", "pcs", "railtel"),
    ("term_big", "Termination box Big", "Termination", "pcs", "railtel"),
    ("term_small", "Termination box Small", "Termination", "pcs", "railtel"),
    ("tape", "Tape rolls", "Consumables", "rolls", "both"),
)


class InventoryError(ValueError):
    pass


def _qty_places(unit: str) -> int:
    return 3 if unit in {"km", "m"} else 0


def parse_qty(raw, unit: str = "pcs") -> float:
    text = str(raw or "").strip().replace(",", "")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text:
        raise InventoryError("Enter a quantity.")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InventoryError("Quantity is not a number.") from exc
    if value <= 0:
        raise InventoryError("Quantity must be more than zero.")
    places = _qty_places(unit)
    quantized = value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)
    if places == 0 and quantized != value:
        raise InventoryError("This item is counted in whole pieces.")
    return float(quantized)


def format_qty(qty, unit: str = "pcs") -> str:
    try:
        value = Decimal(str(qty or 0))
    except InvalidOperation:
        value = Decimal("0")
    places = _qty_places(unit)
    quantized = value.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP)
    if places == 0:
        return str(int(quantized))
    text = format(quantized, "f").rstrip("0").rstrip(".")
    return text or "0"


def qty_label(qty, unit: str = "pcs") -> str:
    return f"{format_qty(qty, unit)} {UNIT_LABELS.get(unit, unit)}"


def step_for(unit: str) -> str:
    return "0.001" if unit == "km" else ("0.1" if unit == "m" else "1")


def can_receive(agent: dict | None) -> bool:
    if not agent or not agent.get("active"):
        return False
    return agent.get("role") == "admin"


def item_visible(item, scope: str | None) -> bool:
    if not scope:
        return True
    item_scope = (item["provider_scope"] if isinstance(item, sqlite3.Row) else item.get("provider_scope")) or "both"
    return item_scope in {scope, "both"}


def ensure_catalog(conn: sqlite3.Connection) -> None:
    for order, (code, name, category, unit, scope) in enumerate(CATALOG, start=1):
        conn.execute(
            "INSERT INTO inventory_items(code, name, category, unit, provider_scope, sort_order, active) "
            "VALUES(?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(code) DO UPDATE SET "
            "name = excluded.name, category = excluded.category, unit = excluded.unit, "
            "provider_scope = excluded.provider_scope, sort_order = excluded.sort_order, "
            "active = 1",
            (code, name, category, unit, scope, order),
        )


def _stock_sql() -> str:
    return (
        "SELECT i.*, "
        "COALESCE((SELECT SUM(r.qty) FROM inventory_receipts r WHERE r.item_id = i.id), 0) AS received_qty, "
        "COALESCE((SELECT SUM(r.amount_paise) FROM inventory_receipts r WHERE r.item_id = i.id), 0) AS paid_paise, "
        "COALESCE((SELECT SUM(u.qty) FROM inventory_usage u WHERE u.item_id = i.id), 0) AS used_qty, "
        "("
        "COALESCE((SELECT SUM(r.qty) FROM inventory_receipts r WHERE r.item_id = i.id), 0) - "
        "COALESCE((SELECT SUM(u.qty) FROM inventory_usage u WHERE u.item_id = i.id), 0)"
        ") AS on_hand "
        "FROM inventory_items i WHERE i.active = 1"
    )


def list_stock(conn: sqlite3.Connection, *, scope: str | None = None) -> list[dict]:
    rows = conn.execute(_stock_sql() + " ORDER BY i.sort_order, i.name COLLATE NOCASE").fetchall()
    items = []
    for row in rows:
        if not item_visible(row, scope):
            continue
        item = dict(row)
        item["on_hand"] = float(item["on_hand"] or 0)
        item["received_qty"] = float(item["received_qty"] or 0)
        item["used_qty"] = float(item["used_qty"] or 0)
        item["paid_paise"] = int(item["paid_paise"] or 0)
        item["on_hand_label"] = qty_label(item["on_hand"], item["unit"])
        item["received_label"] = qty_label(item["received_qty"], item["unit"])
        item["used_label"] = qty_label(item["used_qty"], item["unit"])
        item["step"] = step_for(item["unit"])
        items.append(item)
    return items


def grouped_stock(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: list[tuple[str, list[dict]]] = []
    by_name: dict[str, list[dict]] = {}
    for item in items:
        cat = item.get("category") or "Other"
        by_name.setdefault(cat, []).append(item)
        if cat not in [g[0] for g in groups]:
            groups.append((cat, by_name[cat]))
    return groups


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(_stock_sql() + " AND i.id = ?", (int(item_id),)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["on_hand"] = float(item["on_hand"] or 0)
    item["received_qty"] = float(item["received_qty"] or 0)
    item["used_qty"] = float(item["used_qty"] or 0)
    item["paid_paise"] = int(item["paid_paise"] or 0)
    item["on_hand_label"] = qty_label(item["on_hand"], item["unit"])
    item["received_label"] = qty_label(item["received_qty"], item["unit"])
    item["used_label"] = qty_label(item["used_qty"], item["unit"])
    item["step"] = step_for(item["unit"])
    return item


def receive(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    qty_raw: str,
    amount_raw: str,
    received_on: str,
    note: str,
    actor: str | None,
) -> dict:
    item = get_item(conn, item_id)
    if item is None:
        raise InventoryError("That item is not in the list.")
    qty = parse_qty(qty_raw, item["unit"])
    amount = to_paise(amount_raw)
    if amount < 0:
        raise InventoryError("Amount paid cannot be negative.")
    day = fmt_date(parse_date(received_on) or today())
    stamp = now_iso()
    conn.execute(
        "INSERT INTO inventory_receipts(item_id, qty, amount_paise, received_on, note, created_by, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (int(item_id), qty, amount, day, (note or "").strip(), actor or "", stamp),
    )
    log_activity(
        conn,
        "inventory_receive",
        f"Stock in: {qty_label(qty, item['unit'])} {item['name']}"
        + (f" (₹{fmt_rupees(amount)})" if amount else ""),
        actor=actor,
        meta_json=None,
    )
    return {"item": item, "qty": qty, "amount_paise": amount}


def use_item(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    qty_raw: str,
    kind: str,
    customer_id: int | None,
    note: str,
    agent: dict | None,
) -> dict:
    item = get_item(conn, item_id)
    if item is None:
        raise InventoryError("That item is not in the list.")
    qty = parse_qty(qty_raw, item["unit"])
    kind = (kind or "").strip()
    if kind not in KIND_LABELS:
        raise InventoryError("Choose new connection or faulty replacement.")
    cust_id = int(customer_id) if customer_id else None
    customer_name = ""
    if kind == KIND_FAULTY:
        if not cust_id:
            raise InventoryError("Pick the customer for a faulty replacement.")
        cust = conn.execute("SELECT id, name FROM customers WHERE id = ?", (cust_id,)).fetchone()
        if cust is None:
            raise InventoryError("That customer was not found.")
        customer_name = cust["name"] or ""
    elif cust_id:
        cust = conn.execute("SELECT id, name FROM customers WHERE id = ?", (cust_id,)).fetchone()
        if cust is None:
            cust_id = None
        else:
            customer_name = cust["name"] or ""
    if qty - 1e-9 > float(item["on_hand"] or 0):
        raise InventoryError(
            f"Only {item['on_hand_label']} of {item['name']} on hand."
        )
    actor = (agent or {}).get("name") or ""
    agent_id = (agent or {}).get("id")
    stamp = now_iso()
    day = fmt_date(today())
    conn.execute(
        "INSERT INTO inventory_usage(item_id, qty, kind, customer_id, note, used_on, agent_id, created_by, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(item_id),
            qty,
            kind,
            cust_id,
            (note or "").strip(),
            day,
            int(agent_id) if agent_id else None,
            actor,
            stamp,
        ),
    )
    who = customer_name or KIND_LABELS[kind]
    log_activity(
        conn,
        "inventory_use",
        f"Used {qty_label(qty, item['unit'])} {item['name']} · {KIND_LABELS[kind]}"
        + (f" · {customer_name}" if customer_name else ""),
        actor=actor,
        customer_id=cust_id,
        meta_json=None,
    )
    return {"item": item, "qty": qty, "kind": kind, "who": who}


def match_catalog_name(conn: sqlite3.Connection, raw: str) -> dict | None:
    needle = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if not needle:
        return None
    rows = conn.execute(
        "SELECT id, code, name, unit FROM inventory_items WHERE active = 1"
    ).fetchall()
    exact = [r for r in rows if (r["name"] or "").strip().lower() == needle or (r["code"] or "") == needle]
    if exact:
        return dict(exact[0])
    loose = [r for r in rows if needle in (r["name"] or "").lower() or needle in (r["code"] or "").lower()]
    if len(loose) == 1:
        return dict(loose[0])
    return None


def parse_receive_file(conn: sqlite3.Connection, raw: bytes) -> list[dict]:
    if not raw:
        raise InventoryError("The file was empty.")
    if raw[:2] == b"PK" or raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise InventoryError("Save the order as CSV (not an Excel workbook) and upload that.")
    text = raw.decode("utf-8-sig", errors="replace")
    first = next((line for line in text.splitlines() if line.strip()), "")
    dialect = csv.excel_tab if first.count("\t") > first.count(",") else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise InventoryError("That file has no header row. Use Item, Qty, Amount.")
    headers = {(h or "").strip().lower(): h for h in reader.fieldnames if h}

    def col(*aliases: str) -> str:
        for alias in aliases:
            if alias in headers:
                return headers[alias]
        return ""

    item_col = col("item", "name", "product", "material")
    qty_col = col("qty", "quantity", "qty.", "count")
    amt_col = col("amount", "paid", "amount paid", "price", "cost", "rs", "rupees")
    date_col = col("date", "received", "received on")
    note_col = col("note", "notes", "vendor", "remark")
    if not item_col or not qty_col:
        raise InventoryError("Need Item and Qty columns (Amount is optional).")
    rows = []
    for raw_row in reader:
        name = (raw_row.get(item_col) or "").strip()
        if not name:
            continue
        matched = match_catalog_name(conn, name)
        rows.append(
            {
                "sheet_name": name,
                "item_id": int(matched["id"]) if matched else None,
                "item_name": matched["name"] if matched else "",
                "unit": matched["unit"] if matched else "pcs",
                "qty_raw": (raw_row.get(qty_col) or "").strip(),
                "amount_raw": (raw_row.get(amt_col) or "").strip() if amt_col else "",
                "received_on": (raw_row.get(date_col) or "").strip() if date_col else "",
                "note": (raw_row.get(note_col) or "").strip() if note_col else "",
            }
        )
    if not rows:
        raise InventoryError("No item rows in that file.")
    return rows


def apply_receive_rows(
    conn: sqlite3.Connection, rows: list[dict], *, actor: str | None
) -> dict:
    ok = 0
    skipped = 0
    errors: list[str] = []
    for row in rows:
        if not row.get("item_id"):
            skipped += 1
            errors.append(f"{row.get('sheet_name') or 'Row'}: not in the item list")
            continue
        try:
            receive(
                conn,
                item_id=int(row["item_id"]),
                qty_raw=row.get("qty_raw") or "",
                amount_raw=row.get("amount_raw") or "",
                received_on=row.get("received_on") or "",
                note=row.get("note") or row.get("sheet_name") or "",
                actor=actor,
            )
            ok += 1
        except InventoryError as exc:
            skipped += 1
            errors.append(f"{row.get('sheet_name') or 'Row'}: {exc}")
    return {"ok": ok, "skipped": skipped, "errors": errors[:8]}


def recent_receipts(conn: sqlite3.Connection, *, limit: int = 20, item_id: int | None = None) -> list[dict]:
    where = "WHERE r.item_id = ?" if item_id else ""
    params: list = [int(item_id)] if item_id else []
    params.append(int(limit))
    rows = conn.execute(
        "SELECT r.*, i.name AS item_name, i.unit "
        "FROM inventory_receipts r JOIN inventory_items i ON i.id = r.item_id "
        f"{where} ORDER BY r.id DESC LIMIT ?",
        params,
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["qty_label"] = qty_label(item["qty"], item["unit"])
        out.append(item)
    return out


def recent_usage(
    conn: sqlite3.Connection, *, limit: int = 30, item_id: int | None = None, scope: str | None = None
) -> list[dict]:
    where = []
    params: list = []
    if item_id:
        where.append("u.item_id = ?")
        params.append(int(item_id))
    if scope in {"hathway", "railtel"}:
        where.append("i.provider_scope IN (?, 'both')")
        params.append(scope)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    rows = conn.execute(
        "SELECT u.*, i.name AS item_name, i.unit, c.name AS customer_name, c.code AS customer_code "
        "FROM inventory_usage u "
        "JOIN inventory_items i ON i.id = u.item_id "
        "LEFT JOIN customers c ON c.id = u.customer_id "
        f"{clause} ORDER BY u.id DESC LIMIT ?",
        params,
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["qty_label"] = qty_label(item["qty"], item["unit"])
        item["kind_label"] = KIND_LABELS.get(item["kind"] or "", item["kind"] or "")
        out.append(item)
    return out
