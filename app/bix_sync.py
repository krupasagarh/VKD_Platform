"""Read a Bix Customer_details export and bring this platform's dues in line.

Agents still collect in Bix until this app is the daily tool. A fresh export is
the source of truth for what each household owes: we match the customer, then
post a bill or a credit so net due equals Bix Due Amount. Name, phone and
locality are refreshed from the same file. Nothing is fetched from the Bix
website — only the file you upload.
"""
from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from . import billing
from .db import log_activity
from .money import now_iso, to_paise

# More specific aliases first. Bare "id" / "balance" / "active" are last so they
# do not steal "Balance Amount" or "Active/Inactive" from a Bix Customer Export.
COLUMN_ALIASES = {
    "bix_customer_id": ("customer id", "customer_id", "bix_customer_id", "id"),
    "customer_name": ("customer name", "customer_name", "bill name", "bill_name", "name"),
    "phone": ("mobile no", "mobile number", "mobile1", "mobile", "phone"),
    "stb_number": (
        "settop box number", "set top box number", "settop_box_number",
        "stb number", "stb_number", "stb",
    ),
    "card_number": ("card number", "vc number", "vc_number", "vc"),
    "status": ("active/inactive", "status", "active"),
    "balance": (
        "balance amount", "due amount", "outstanding amount",
        "outstanding", "dues", "balance",
    ),
    "locality": ("sub area", "sub_area", "locality", "area"),
    "city": ("billing address", "address", "city", "location"),
    "monthly_rent": ("plan amount", "monthly rent", "monthly_rent", "rent"),
    "remarks": ("remarks", "notes", "extra stbs", "products"),
    "customer_code": ("customer code", "membership number"),
}

_PLATFORM_CODE = re.compile(r"\b([A-Z]{1,4}-\d+)\b", re.I)
# Bix prints household ids as (GO-CN-24) — two letters, then the local code.
_PAREN_CODE = re.compile(r"\(([A-Z]{2,4}-[A-Z]{1,4}-\d+)\)", re.I)
_STB_RE = re.compile(r"N\d{11}", re.I)
_PRODUCT_NAME = re.compile(
    r"^(?:[\d.]+|sony ten.*|zee .+|star .+|turner family pack|cable bill.*|"
    r".*\b(?:hd|bouquet|family pack)\b)$",
    re.I,
)


def _clean(value) -> str:
    return str(value or "").strip().strip("'").strip('"').strip()


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _extract_codes(*chunks: str) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        for match in _PAREN_CODE.finditer(chunk or ""):
            code = match.group(1).upper()
            if code not in found:
                found.append(code)
        for match in _PLATFORM_CODE.finditer(chunk or ""):
            code = match.group(1).upper()
            if any(code != other and code in other for other in found):
                continue
            if code not in found:
                found.append(code)
    return found


def _norm_code(value: str) -> str:
    """AJ-1 / MST-7 from a cell; ignore bare numeric Bix ids."""
    codes = _extract_codes(value)
    if codes:
        return codes[0]
    text = _clean(value).upper()
    return "" if text.isdigit() else text


def _norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _extract_stbs(*chunks: str) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        text = (chunk or "").upper().replace(" ", "").replace("'", "").replace('"', "")
        for match in _STB_RE.findall(text):
            stb = match.upper()
            if stb not in found:
                found.append(stb)
    return found


def _is_junk_row(name: str, phone: str, stbs: list[str], codes: list[str]) -> bool:
    if stbs or codes or len(phone) == 10:
        return False
    return not name or bool(_PRODUCT_NAME.match(name))


def _map_headers(headers: list[str]) -> dict[str, str]:
    normalized = {_norm_header(h): h for h in headers}
    mapping: dict[str, str] = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            src = normalized.get(_norm_header(alias))
            if src:
                mapping[target] = src
                break
    return mapping


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._in_th = False
        self._in_td = False
        self._cur_row: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "th":
            self._in_th = True
            self._buf = []
        elif tag == "td":
            self._in_td = True
            self._buf = []
        elif tag == "tr":
            self._cur_row = []

    def handle_endtag(self, tag) -> None:
        if tag == "th":
            self._in_th = False
            self.headers.append(_clean("".join(self._buf)))
        elif tag == "td":
            self._in_td = False
            self._cur_row.append(_clean("".join(self._buf)))
        elif tag == "tr" and self._cur_row:
            self.rows.append(self._cur_row)

    def handle_data(self, data) -> None:
        if self._in_th or self._in_td:
            self._buf.append(data)


def read_bix_file(path: Path) -> tuple[list[str], list[dict]]:
    """Bix Customer_details is often HTML saved as .xls. Also accept CSV / TSV."""
    raw = path.read_bytes()
    if raw[:2] == b"PK" or raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError(
            "This looks like an Excel workbook. Export Customer details from Bix "
            "as the usual .xls (or save as CSV) and upload that."
        )
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    head = text[:800].lower()
    if "<table" in head:
        parser = _TableParser()
        parser.feed(text)
        headers = [h for h in parser.headers if h]
        rows = []
        for cells in parser.rows:
            if len(cells) < len(headers):
                cells = cells + [""] * (len(headers) - len(cells))
            row = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers))}
            if any(str(v or "").strip() for v in row.values()):
                rows.append(row)
        return headers, rows

    handle = io.StringIO(text)
    first = next((line for line in text.splitlines() if line.strip()), "")
    # Do not use csv.Sniffer — Bix STB cells start with a quote like 'N701… and
    # the sniffer treats that apostrophe as the quoting character, then every
    # product line becomes a fake customer.
    dialect = csv.excel_tab if first.count("\t") > first.count(",") else csv.excel
    reader = csv.DictReader(handle, dialect=dialect)
    headers = [(h or "").strip() for h in (reader.fieldnames or [])]
    rows = [{(k or "").strip(): _clean(v) for k, v in row.items()} for row in reader]
    return headers, rows


def parse_bix_customers(path: Path) -> list[dict]:
    """One row per Bix customer id, with STBs folded together."""
    headers, raw_rows = read_bix_file(path)
    mapping = _map_headers(headers)
    if "balance" not in mapping and "customer_name" not in mapping:
        raise ValueError(
            "This file does not look like a Bix Customer details export. "
            "It needs a Due Amount (or Balance) column and a customer name or id."
        )

    grouped: dict[str, dict] = {}
    unnamed = 0
    for raw in raw_rows:
        get = lambda key: _clean(raw.get(mapping[key], "")) if key in mapping else ""
        name = get("customer_name")
        phone = _norm_phone(get("phone"))
        codes = _extract_codes(
            get("bix_customer_id"), get("customer_code"), name, get("remarks"),
        )
        stbs = _extract_stbs(
            get("stb_number"), get("card_number"), get("remarks"), get("customer_code"),
        )
        if _is_junk_row(name, phone, stbs, codes):
            continue
        if not name and not codes and not stbs and not phone:
            continue

        raw_id = get("bix_customer_id")
        numeric_id = raw_id if raw_id.isdigit() else ""
        display_code = codes[0] if codes else (numeric_id or "")
        if numeric_id:
            group_key = numeric_id
        elif display_code:
            group_key = display_code
        elif phone:
            group_key = f"p:{phone}"
        else:
            unnamed += 1
            group_key = f"BIX-{unnamed}"
            display_code = group_key

        due = to_paise(get("balance") or "0")
        if group_key not in grouped:
            grouped[group_key] = {
                "code": display_code or group_key,
                "name": name or display_code or group_key,
                "phone": phone,
                "locality": get("locality"),
                "city": get("city"),
                "status": (get("status") or "active").lower() or "active",
                "due_paise": due,
                "stbs": stbs,
                "codes": codes,
            }
        else:
            entry = grouped[group_key]
            if due > entry["due_paise"]:
                entry["due_paise"] = due
            for item in stbs:
                if item not in entry["stbs"]:
                    entry["stbs"].append(item)
            for code in codes:
                if code not in entry["codes"]:
                    entry["codes"].append(code)
                    if not _PLATFORM_CODE.search(entry["code"] or ""):
                        entry["code"] = code
            if phone and not entry["phone"]:
                entry["phone"] = phone
            if name and (not entry["name"] or entry["name"] == entry["code"]):
                entry["name"] = name
    return list(grouped.values())


def _hathway_ok(conn, customer_id: int) -> bool:
    """Prepaid-only customers are left alone. No connections, or any Hathway box, is fine."""
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN provider = 'railtel' THEN 1 ELSE 0 END) AS r, "
        "SUM(CASE WHEN provider = 'hathway' THEN 1 ELSE 0 END) AS h, "
        "SUM(CASE WHEN provider = 'iptv' THEN 1 ELSE 0 END) AS i "
        "FROM connections WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    railtel = int(row["r"] or 0)
    hathway = int(row["h"] or 0)
    iptv = int(row["i"] or 0)
    if (railtel or iptv) and not hathway:
        return False
    return True


def _match_customer(conn, item: dict):
    """STB first, then AJ/MST code, then a unique phone. Never match a numeric Bix id.

    A Railtel-only household is not a Bix match even if the phone is the same.
    """
    for stb in item.get("stbs") or []:
        row = conn.execute(
            "SELECT c.* FROM customers c JOIN connections cn ON cn.customer_id = c.id "
            "WHERE upper(cn.upstream_id) = upper(?) LIMIT 1",
            (stb,),
        ).fetchone()
        if row and _hathway_ok(conn, int(row["id"])):
            return row, "stb"
    codes = [c for c in (item.get("codes") or []) if c]
    display = (item.get("code") or "").strip()
    if display and not display.isdigit() and not display.startswith("BIX-") and display not in codes:
        codes.append(display)
    for code in codes:
        row = conn.execute(
            "SELECT * FROM customers WHERE upper(code) = upper(?)", (code,)
        ).fetchone()
        if row and _hathway_ok(conn, int(row["id"])):
            return row, "code"
    if item.get("phone") and len(item["phone"]) == 10:
        matches = [
            r for r in conn.execute(
                "SELECT * FROM customers WHERE phone = ?", (item["phone"],)
            ).fetchall()
            if _hathway_ok(conn, int(r["id"]))
        ]
        if len(matches) == 1:
            return matches[0], "phone"
    return None, ""


def preview(conn, items: list[dict]) -> list[dict]:
    """Compare each Bix customer to the ledger we already hold."""
    out = []
    for item in items:
        customer, how = _match_customer(conn, item)
        current = 0
        if customer:
            current = int(billing.customer_ledger(conn, int(customer["id"]))["net_due_paise"])
        delta = int(item["due_paise"]) - current
        out.append({
            **item,
            "customer_id": int(customer["id"]) if customer else None,
            "platform_name": customer["name"] if customer else "",
            "platform_code": customer["code"] if customer else "",
            "platform_due_paise": current,
            "delta_paise": delta,
            "match": how,
            "action": (
                "create" if customer is None
                else ("unchanged" if delta == 0 else "adjust")
            ),
        })
    return out


def ensure_stbs(conn, customer_id: int, stbs: list[str], *, stamp: str | None = None) -> int:
    """Attach Hathway STBs that are not already on any customer. Does not move existing ones."""
    added = 0
    stamp = stamp or now_iso()
    for raw in stbs or []:
        stb = _clean(raw).upper().lstrip("'")
        if not _STB_RE.fullmatch(stb):
            continue
        taken = conn.execute(
            "SELECT id FROM connections WHERE upper(upstream_id) = ?", (stb,)
        ).fetchone()
        if taken:
            continue
        conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, "
            "billing_type, amount_paise, created_at, updated_at) "
            "VALUES(?, 'hathway', ?, 'active', 'postpaid', 0, ?, ?)",
            (customer_id, stb, stamp, stamp),
        )
        added += 1
    return added


def attach_stbs_from_items(conn, items: list[dict]) -> dict:
    """Add missing STBs from a parsed Bix file onto already-matched customers."""
    attached = 0
    customers = 0
    unmatched = 0
    for item in items:
        customer, _how = _match_customer(conn, item)
        if customer is None:
            unmatched += 1
            continue
        n = ensure_stbs(conn, int(customer["id"]), item.get("stbs") or [])
        if n:
            customers += 1
            attached += n
    return {"attached": attached, "customers": customers, "unmatched": unmatched}


def _set_due(conn, customer_id: int, target: int, actor: str | None) -> str:
    current = int(billing.customer_ledger(conn, customer_id)["net_due_paise"])
    delta = int(target) - current
    if delta == 0:
        return "unchanged"
    if delta > 0:
        billing.create_bill(
            conn,
            customer_id=customer_id,
            connection_id=None,
            package_id=None,
            package_name="Bix balance update",
            amount_paise=delta,
            period_start=None,
            period_end=None,
            source="bix_sync",
            gst_percentage=0,
            notes=f"Bix due {target / 100:.2f} vs platform {current / 100:.2f}",
        )
    else:
        billing.record_payment(
            conn,
            customer_id=customer_id,
            amount_paise=-delta,
            mode="adjustment",
            collected_by=actor,
            notes=f"Bix due {target / 100:.2f} vs platform {current / 100:.2f}",
        )
    billing.reconcile_customer(conn, customer_id)
    return "adjusted"


def _usable_code(conn, customer_id: int, code: str) -> str:
    code = (code or "").strip()
    if not code:
        return ""
    taken = conn.execute(
        "SELECT id FROM customers WHERE upper(code) = upper(?) AND id != ?",
        (code, customer_id),
    ).fetchone()
    return "" if taken else code


def _norm_stb_list(stbs: list[str]) -> list[str]:
    out: list[str] = []
    for raw in stbs or []:
        stb = _clean(raw).upper().lstrip("'")
        if _STB_RE.fullmatch(stb) and stb not in out:
            out.append(stb)
    return out


def apply_preview(
    conn,
    rows: list[dict],
    *,
    create_missing: bool,
    actor: str | None,
) -> dict:
    """Make Hathway / Bix households match the export. Railtel-only customers are skipped."""
    created = 0
    adjusted = 0
    unchanged = 0
    skipped = 0
    moved = 0
    added = 0
    removed = 0
    stamp = now_iso()
    wanted: dict[int, list[str]] = {}

    for row in rows:
        customer_id = row.get("customer_id")
        stbs = _norm_stb_list(row.get("stbs") or [])
        status = (row.get("status") or "active").lower() or "active"
        if status not in {"active", "inactive", "suspended"}:
            status = "active"

        if customer_id is None:
            if not create_missing:
                skipped += 1
                continue
            code = _usable_code(conn, 0, row.get("code") or "")
            cursor = conn.execute(
                "INSERT INTO customers(code, name, phone, alt_phone, address, area, "
                "sub_area, pincode, status, notes, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, 'Tiptur', ?, '572201', ?, ?, ?, ?)",
                (
                    code or None,
                    row.get("name") or row.get("code") or "Bix customer",
                    row.get("phone") or "",
                    row.get("phone") or "",
                    row.get("city") or "Tiptur",
                    row.get("locality") or "",
                    status,
                    "Created from Bix sync",
                    stamp,
                    stamp,
                ),
            )
            customer_id = int(cursor.lastrowid)
            created += 1
        else:
            customer_id = int(customer_id)
            if not _hathway_ok(conn, customer_id):
                skipped += 1
                continue
            conn.execute(
                "UPDATE customers SET name = COALESCE(NULLIF(?, ''), name), "
                "phone = COALESCE(NULLIF(?, ''), phone), "
                "sub_area = COALESCE(NULLIF(?, ''), sub_area), "
                "code = COALESCE(NULLIF(?, ''), code), "
                "status = ?, updated_at = ? WHERE id = ?",
                (
                    row.get("name") or "",
                    row.get("phone") or "",
                    row.get("locality") or "",
                    _usable_code(conn, customer_id, row.get("code") or ""),
                    status,
                    stamp,
                    customer_id,
                ),
            )

        wanted[customer_id] = stbs
        if _set_due(conn, customer_id, int(row.get("due_paise") or 0), actor) == "adjusted":
            adjusted += 1
        else:
            unchanged += 1

    for customer_id, stbs in wanted.items():
        for stb in stbs:
            existing = conn.execute(
                "SELECT id, customer_id FROM connections WHERE upper(upstream_id) = ?",
                (stb,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO connections(customer_id, provider, upstream_id, status, "
                    "billing_type, amount_paise, created_at, updated_at) "
                    "VALUES(?, 'hathway', ?, 'active', 'postpaid', 0, ?, ?)",
                    (customer_id, stb, stamp, stamp),
                )
                added += 1
            elif int(existing["customer_id"]) != customer_id:
                other = int(existing["customer_id"])
                if _hathway_ok(conn, other):
                    conn.execute(
                        "UPDATE connections SET customer_id = ?, updated_at = ? WHERE id = ?",
                        (customer_id, stamp, existing["id"]),
                    )
                    moved += 1

        have = {
            (r["upstream_id"] or "").upper()
            for r in conn.execute(
                "SELECT id, upstream_id FROM connections "
                "WHERE customer_id = ? AND provider = 'hathway'",
                (customer_id,),
            )
        }
        extra = have - set(stbs)
        for stb in extra:
            conn.execute(
                "DELETE FROM connections WHERE customer_id = ? AND provider = 'hathway' "
                "AND upper(upstream_id) = ?",
                (customer_id, stb),
            )
            removed += 1

    try:
        from . import bix_history

        history = bix_history.rematch(conn)
    except Exception:
        history = {"matched": 0, "ambiguous": 0}

    summary = {
        "created": created,
        "adjusted": adjusted,
        "unchanged": unchanged,
        "skipped": skipped,
        "stbs_added": added,
        "stbs_moved": moved,
        "stbs_removed": removed,
        "history_matched": history.get("matched", 0),
    }
    log_activity(
        conn,
        "bix_sync",
        f"Bix align — {adjusted} due(s), {created} created, {added} STB(s) added, "
        f"{moved} moved, {removed} extra removed. Railtel left as-is.",
        actor=actor,
        meta_json=json.dumps(summary),
    )
    return summary
