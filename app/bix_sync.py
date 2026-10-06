"""Read a Bix Customer_details export and bring matched households' dues in line.

Bix is an old archive. It is not the live cable inventory. A file may still
update name, phone, area and due on a household that is already here. It must
not create customers and must not add, move, or remove Hathway boxes. Which
boxes exist comes from the Hathway dashboard; the latest cable due comes from
Mobize. Nothing is fetched from the Bix website — only the file you upload.
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
from .money import fmt_rupees, now_iso, round_up_rupee, to_paise

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
    "remarks": ("remarks", "notes", "extra stbs", "products", "remark", "follow_up_comments"),
    "product_name": ("products", "product", "bouquet", "package"),
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
        if codes:
            group_key = codes[0].upper()
            display_code = codes[0]
        elif phone:
            group_key = f"p:{phone}"
        elif numeric_id:
            group_key = numeric_id
        else:
            unnamed += 1
            group_key = f"BIX-{unnamed}"
            display_code = group_key

        due = to_paise(get("balance") or "0")
        rent = to_paise(get("monthly_rent") or "0")
        plan_note = _clean(get("product_name") or get("remarks") or "").split("\n")[0].strip()
        stb = _clean(get("stb_number") or "").upper().lstrip("'")
        if not stb:
            stb = next((s for s in stbs), "")
        vc = _clean(get("card_number") or "").upper().lstrip("'")

        if group_key not in grouped:
            grouped[group_key] = {
                "code": display_code or group_key,
                "name": name or display_code or group_key,
                "phone": phone,
                "locality": get("locality"),
                "city": get("city"),
                "status": (get("status") or "active").lower() or "active",
                "due_paise": due,
                "plan_amount_paise": 0,
                "plan_name": "",
                "row_plans": [],
                "stbs": [],
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

        entry = grouped[group_key]
        if rent > 0:
            row_id = get("bix_customer_id") or f"{group_key}:{len(entry['row_plans'])}"
            if row_id not in entry.setdefault("row_ids", set()):
                entry["row_ids"].add(row_id)
                entry["row_plans"].append(rent)
            for item in stbs:
                if item not in entry["stbs"]:
                    entry["stbs"].append(item)
            if stb and _STB_RE.fullmatch(stb) and stb not in entry["stbs"]:
                entry["stbs"].append(stb)
        if plan_note and not entry["plan_name"]:
            entry["plan_name"] = plan_note[:120]

    for entry in grouped.values():
        entry.pop("row_ids", None)
        plans = entry.pop("row_plans", [])
        if plans:
            entry["plan_amount_paise"] = sum(int(v) for v in plans)
        entry.setdefault("plan_amount_paise", 0)
        entry.setdefault("plan_name", "")
    return list(grouped.values())


def items_from_accounts_csv(path: Path) -> list[dict]:
    """Build preview/apply rows from cableway_automation ``bix_accounts.csv``."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Bix accounts file not found: {path}")

    stb_owners = _hathway_stb_owners(path.parent)
    grouped: dict[str, dict] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            code = _norm_code(raw.get("bix_customer_id") or "")
            if not code or code.isdigit():
                continue
            name = _clean(raw.get("customer_name") or "")
            phone = _norm_phone(raw.get("phone") or raw.get("phone_normalized") or "")
            stb = _clean(raw.get("stb_number") or "").upper().lstrip("'")
            due = to_paise(raw.get("balance") or "0")
            rent = to_paise(raw.get("monthly_rent") or "0")
            plan_note = _clean(raw.get("product_note") or "")
            status = (_clean(raw.get("status") or "active") or "active").lower()
            locality = _clean(raw.get("locality") or "")
            city = _clean(raw.get("city") or "")

            if code not in grouped:
                grouped[code] = {
                    "code": code,
                    "name": name or code,
                    "phone": phone,
                    "locality": locality,
                    "city": city,
                    "status": status,
                    "due_paise": due,
                    "plan_amount_paise": 0,
                    "plan_name": "",
                    "stb_rents": {},
                    "stbs": [],
                    "codes": [code],
                }
            entry = grouped[code]
            if due > entry["due_paise"]:
                entry["due_paise"] = due
            if rent > 0:
                rent_key = ""
                if stb and _STB_RE.fullmatch(stb):
                    rent_key = stb
                else:
                    vc = _clean(raw.get("vc_number_bix") or raw.get("vc_number") or "").upper()
                    if vc:
                        rent_key = vc
                if rent_key and rent_key not in entry["stb_rents"]:
                    entry["stb_rents"][rent_key] = rent
                elif not rent_key and rent > entry["plan_amount_paise"]:
                    entry["plan_amount_paise"] = rent
            if plan_note and not entry["plan_name"]:
                entry["plan_name"] = plan_note
            if name and (not entry["name"] or entry["name"] == entry["code"]):
                entry["name"] = name
            if phone and not entry["phone"]:
                entry["phone"] = phone
            if locality and not entry["locality"]:
                entry["locality"] = locality
            if city and not entry["city"]:
                entry["city"] = city
            if stb and _STB_RE.fullmatch(stb):
                owner = stb_owners.get(stb, code)
                if owner != code:
                    continue
                if stb not in entry["stbs"]:
                    entry["stbs"].append(stb)
    for entry in grouped.values():
        rents = entry.pop("stb_rents", {})
        if rents:
            entry["plan_amount_paise"] = sum(int(v) for v in rents.values())
        entry.setdefault("plan_amount_paise", 0)
        entry.setdefault("plan_name", "")
    return list(grouped.values())


def _hathway_stb_owners(data_dir: Path) -> dict[str, str]:
    """Map Hathway STB -> household code from the CableWay Hathway export."""
    path = Path(data_dir) / "cableway_hathway_generated_v2.csv"
    owners: dict[str, str] = {}
    if not path.is_file():
        return owners
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            code = _norm_code(raw.get("customer_id") or "")
            stb = _clean(raw.get("settop_box_number") or "").upper().lstrip("'")
            if code and _STB_RE.fullmatch(stb):
                owners[stb] = code
    return owners


def master_accounts_path() -> Path:
    from .config import CABLEWAY_DATA_DIR

    return CABLEWAY_DATA_DIR / "bix_accounts.csv"


def load_master_items() -> list[dict]:
    path = master_accounts_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Bix master file not found: {path}. "
            "Export Customer details from Bix into cableway_automation/data first."
        )
    return items_from_accounts_csv(path)


def load_bix_items(path: Path | None = None) -> list[dict]:
    """Load Bix households from a Customer export or the master accounts CSV."""
    if path is not None:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.name.lower() == "bix_accounts.csv":
            return items_from_accounts_csv(path)
        return parse_bix_customers(path)
    return load_master_items()


def _hathway_ok(conn, customer_id: int, *, bix_code: str = "") -> bool:
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
        code = (bix_code or "").strip()
        if code and conn.execute(
            "SELECT 1 FROM customers WHERE id = ? AND upper(code) = upper(?)",
            (customer_id, code),
        ).fetchone():
            return True
        return False
    return True


def customer_has_hathway(conn, customer_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM connections WHERE customer_id = ? AND provider = 'hathway' LIMIT 1",
        (customer_id,),
    ).fetchone()
    return row is not None


def _match_hathway_customer_by_stb(conn, stb: str):
    stb = _clean(stb).upper().lstrip("'")
    if not stb:
        return None
    return conn.execute(
        "SELECT c.* FROM customers c JOIN connections cn ON cn.customer_id = c.id "
        "WHERE cn.provider = 'hathway' AND upper(cn.upstream_id) = upper(?) LIMIT 1",
        (stb,),
    ).fetchone()


def _bix_source_row_key(raw: dict, mapping: dict, stbs: list[str]) -> str:
    """One key per Bix billing row — plan amount is charged once per key, not per STB."""

    def get(key: str) -> str:
        return _clean(raw.get(mapping[key], "")) if key in mapping else ""

    bix_id = get("bix_customer_id")
    if bix_id.isdigit():
        return f"bix:{bix_id}"

    codes = _extract_codes(bix_id, get("customer_code"), get("customer_name"), get("remarks"))
    code = codes[0] if codes else get("customer_code") or ""
    primary = stbs[0] if stbs else _clean(get("stb_number")).upper().lstrip("'")
    if not primary:
        vc = _clean(get("card_number")).upper().lstrip("'")
        primary = vc or get("customer_name")[:48] or bix_id
    return f"line:{code}:{primary}"


def _match_hathway_household(conn, *, stbs, codes, phone, name):
    """Map one Bix customer row to a platform household with Hathway."""
    for stb in stbs or []:
        row = _match_hathway_customer_by_stb(conn, stb)
        if row is not None:
            return row

    for code in codes or []:
        row = conn.execute(
            "SELECT * FROM customers WHERE upper(code) = upper(?) LIMIT 1", (code,)
        ).fetchone()
        if row is not None and customer_has_hathway(conn, int(row["id"])):
            return row

    phone = _norm_phone(phone or "")
    if len(phone) == 10:
        matches = [
            r
            for r in conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchall()
            if customer_has_hathway(conn, int(r["id"]))
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def plan_amounts_from_bix_file(conn, path: Path) -> dict[int, dict]:
    """Bix file → platform customer id → household plan amount (Hathway only).

    Plan Amount in Bix is per **customer row**, not per STB. When one row lists
    several boxes, the amount is still counted once. Separate Bix rows that map
    to the same platform household are summed (e.g. one row per box with its
    own line charge in ``bix_accounts.csv``).
    """
    headers, raw_rows = read_bix_file(path)
    mapping = _map_headers(headers)
    bix_rows: dict[str, dict] = {}

    for raw in raw_rows:
        get = lambda key, row=raw: _clean(row.get(mapping[key], "")) if key in mapping else ""
        stbs = _extract_stbs(
            get("stb_number"), get("card_number"), get("remarks"), get("customer_code"),
        )
        rent = to_paise(get("monthly_rent") or "0")
        if rent <= 0:
            continue

        row_key = _bix_source_row_key(raw, mapping, stbs)
        codes = _extract_codes(
            get("bix_customer_id"), get("customer_code"), get("customer_name"), get("remarks"),
        )
        phone = _norm_phone(get("phone"))
        plan_note = _clean(get("product_name") or get("remarks") or "").split("\n")[0].strip()

        bucket = bix_rows.setdefault(
            row_key,
            {
                "amount_paise": 0,
                "stbs": set(),
                "codes": set(),
                "phone": phone,
                "name": get("customer_name"),
                "plan_name": "",
            },
        )
        bucket["amount_paise"] = rent
        bucket["stbs"].update(stbs)
        bucket["codes"].update(codes)
        if phone:
            bucket["phone"] = phone
        if plan_note and not bucket["plan_name"]:
            bucket["plan_name"] = plan_note[:120]

    out: dict[int, dict] = {}
    for bix_row in bix_rows.values():
        amount = int(bix_row["amount_paise"] or 0)
        if amount <= 0:
            continue

        customer = _match_hathway_household(
            conn,
            stbs=list(bix_row["stbs"]),
            codes=list(bix_row["codes"]),
            phone=bix_row.get("phone") or "",
            name=bix_row.get("name") or "",
        )
        if customer is None:
            continue
        cid = int(customer["id"])
        if not customer_has_hathway(conn, cid):
            continue

        slot = out.setdefault(
            cid,
            {
                "amount_paise": 0,
                "stbs": set(),
                "plan_name": "",
                "code": customer["code"],
                "name": customer["name"],
            },
        )
        slot["amount_paise"] += amount
        slot["stbs"].update(bix_row["stbs"])
        if bix_row["plan_name"] and not slot["plan_name"]:
            slot["plan_name"] = bix_row["plan_name"]

    return out


# Backwards-compatible alias
plan_amounts_from_bix_export = plan_amounts_from_bix_file


def apply_bix_hathway_plan_amounts(
    conn,
    *,
    source_path: Path,
    actor: str = "bix-plan-sync",
    dry_run: bool = False,
) -> dict:
    """Set household custom plan amount from Bix — Hathway customers only."""
    updated = 0
    no_match = 0
    no_rent = 0
    unchanged = 0
    samples: list[dict] = []

    by_customer = plan_amounts_from_bix_file(conn, Path(source_path))

    for customer_id, data in by_customer.items():
        amount = int(data["amount_paise"])
        if amount <= 0:
            continue

        plan_name = (data.get("plan_name") or "").strip()
        if not plan_name:
            plan_name = "Hathway plan"

        prev = billing.customer_custom_plan(conn, customer_id)
        prev_amount = int((prev or {}).get("list_paise") or 0)
        if prev_amount == amount and (prev or {}).get("name") == plan_name:
            unchanged += 1
            continue

        if not dry_run:
            billing.set_customer_custom_plan(
                conn,
                customer_id,
                name=plan_name,
                amount_paise=amount,
                validity_days=30,
                details="Synced from Bix plan amount (household)",
                bundle="",
            )

        updated += 1
        if len(samples) < 12:
            samples.append({
                "code": data.get("code", ""),
                "name": data.get("name", ""),
                "amount_rupees": amount / 100,
                "plan_name": plan_name,
                "stbs": len(data.get("stbs") or []),
            })

    summary = {
        "updated": updated,
        "unchanged": unchanged,
        "no_bix_rent": no_rent,
        "no_platform_match": no_match,
        "not_hathway": 0,
        "samples": samples,
        "matched_customers": len(by_customer),
    }
    if not dry_run and updated:
        log_activity(
            conn,
            "bix_plan_sync",
            f"Bix Hathway plan amounts — {updated} customer(s) updated",
            actor=actor,
            meta_json=json.dumps({k: v for k, v in summary.items() if k != "samples"}),
        )
    return summary


def _platform_code(value: str) -> str:
    text = (value or "").strip()
    if not text or text.isdigit() or text.startswith("BIX-"):
        return ""
    return text


def _match_customer(conn, item: dict):
    """Code first, then STB, then a unique phone. Never match a numeric Bix id.

    When Bix carries a household code that is not on the platform yet, create a new
    customer instead of attaching via a shared STB on another code.
    """
    codes = [c for c in (item.get("codes") or []) if c]
    display = _platform_code(item.get("code") or "")
    if display and display not in codes:
        codes.insert(0, display)

    for code in codes:
        row = conn.execute(
            "SELECT * FROM customers WHERE upper(code) = upper(?)", (code,)
        ).fetchone()
        if row and _hathway_ok(conn, int(row["id"]), bix_code=code):
            return row, "code"

    if display and not conn.execute(
        "SELECT id FROM customers WHERE upper(code) = upper(?)", (display,)
    ).fetchone():
        return None, ""

    for stb in item.get("stbs") or []:
        row = conn.execute(
            "SELECT c.* FROM customers c JOIN connections cn ON cn.customer_id = c.id "
            "WHERE upper(cn.upstream_id) = upper(?) LIMIT 1",
            (stb,),
        ).fetchone()
        if row and _hathway_ok(conn, int(row["id"]), bix_code=display):
            return row, "stb"

    if item.get("phone") and len(item["phone"]) == 10:
        matches = [
            r for r in conn.execute(
                "SELECT * FROM customers WHERE phone = ?", (item["phone"],)
            ).fetchall()
            if _hathway_ok(conn, int(r["id"]), bix_code=display)
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
    """Do not add Hathway boxes from Bix.

    Live boxes come from the Hathway dashboard. Bix still lists terminated
    hardware, so inserting those rows made the platform track the archive.
    """
    del conn, customer_id, stbs, stamp
    return 0


def attach_stbs_from_items(conn, items: list[dict]) -> dict:
    """Bix no longer attaches STBs. Counts stay at zero so older callers keep working."""
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


def scrub_bix_sync_ledger(conn, customer_id: int) -> bool:
    """Drop bix_sync-only bills and matching adjustments (wrong-household cleanup)."""
    real = conn.execute(
        "SELECT 1 FROM bills WHERE customer_id = ? AND status != 'cancelled' "
        "AND COALESCE(source, '') != 'bix_sync' LIMIT 1",
        (customer_id,),
    ).fetchone()
    if real:
        purge_bix_sync_ghosts(conn, customer_id)
        return False
    rows = conn.execute(
        "SELECT id FROM bills WHERE customer_id = ? AND status != 'cancelled' "
        "AND source = 'bix_sync'",
        (customer_id,),
    ).fetchall()
    changed = False
    for row in rows:
        billing.cancel_bill(conn, int(row["id"]))
        changed = True
    for row in conn.execute(
        "SELECT id FROM payments WHERE customer_id = ? AND mode = 'adjustment' "
        "AND notes LIKE 'Bix due%'",
        (customer_id,),
    ):
        billing.delete_payment(conn, int(row["id"]))
        changed = True
    if changed:
        billing.reconcile_customer(conn, customer_id)
    ghosts = purge_bix_sync_ghosts(conn, customer_id)
    return changed or bool(ghosts.get("bills_deleted"))


def purge_bix_sync_ghosts(conn, customer_id: int) -> dict:
    """Remove cancelled bix_sync bills that should never appear on a customer."""
    real = conn.execute(
        "SELECT 1 FROM bills WHERE customer_id = ? AND status != 'cancelled' "
        "AND COALESCE(source, '') != 'bix_sync' LIMIT 1",
        (customer_id,),
    ).fetchone()
    if real:
        return {"bills_deleted": 0, "payments_deleted": 0}

    bills_deleted = 0
    for row in conn.execute(
        "SELECT id FROM bills WHERE customer_id = ? AND source = 'bix_sync' "
        "AND status = 'cancelled'",
        (customer_id,),
    ):
        conn.execute("DELETE FROM bill_payments WHERE bill_id = ?", (int(row["id"]),))
        conn.execute("DELETE FROM bills WHERE id = ?", (int(row["id"]),))
        bills_deleted += 1

    payments_deleted = 0
    any_bill = conn.execute(
        "SELECT 1 FROM bills WHERE customer_id = ? AND status != 'cancelled' LIMIT 1",
        (customer_id,),
    ).fetchone()
    if not any_bill:
        ledger = billing.customer_ledger(conn, customer_id)
        if int(ledger["net_due_paise"]) == 0:
            for row in conn.execute(
                "SELECT id FROM payments WHERE customer_id = ? AND mode = 'adjustment' "
                "AND notes LIKE 'Bix due%'",
                (customer_id,),
            ):
                billing.delete_payment(conn, int(row["id"]))
                payments_deleted += 1
        if bills_deleted or payments_deleted:
            billing.reconcile_customer(conn, customer_id)
    return {"bills_deleted": bills_deleted, "payments_deleted": payments_deleted}


def purge_all_bix_sync_ghosts(conn) -> dict:
    """Walk every customer; remove leftover cancelled bix_sync rows."""
    totals = {"customers": 0, "bills_deleted": 0, "payments_deleted": 0}
    for row in conn.execute("SELECT id FROM customers ORDER BY id"):
        result = purge_bix_sync_ghosts(conn, int(row["id"]))
        if result["bills_deleted"] or result["payments_deleted"]:
            totals["customers"] += 1
            totals["bills_deleted"] += result["bills_deleted"]
            totals["payments_deleted"] += result["payments_deleted"]
    return totals


def _set_due(conn, customer_id: int, target: int, actor: str | None) -> str:
    target = round_up_rupee(target)
    if int(target) == 0:
        scrub_bix_sync_ledger(conn, customer_id)
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
            notes=f"Bix due {fmt_rupees(target)} vs platform {fmt_rupees(current)}",
        )
    else:
        billing.record_payment(
            conn,
            customer_id=customer_id,
            amount_paise=-delta,
            mode="adjustment",
            collected_by=actor,
            notes=f"Bix due {fmt_rupees(target)} vs platform {fmt_rupees(current)}",
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
    """Update name, phone, area and due for households already on the platform.

    Does not create customers and does not add, move, or delete Hathway
    connections. ``create_missing`` is ignored: Bix is not the live box list.
    Railtel-only customers are still skipped by ``_hathway_ok``.
    """
    del create_missing
    created = 0
    adjusted = 0
    unchanged = 0
    skipped = 0
    moved = 0
    added = 0
    removed = 0
    stamp = now_iso()

    for row in rows:
        customer_id = row.get("customer_id")
        status = (row.get("status") or "active").lower() or "active"
        if status not in {"active", "inactive", "suspended"}:
            status = "active"

        if customer_id is None:
            skipped += 1
            continue
        customer_id = int(customer_id)
        if not _hathway_ok(conn, customer_id, bix_code=row.get("code") or ""):
            skipped += 1
            continue
        bix_phone = row.get("phone") or ""
        conn.execute(
            "UPDATE customers SET name = COALESCE(NULLIF(?, ''), name), "
            "phone = COALESCE(NULLIF(?, ''), phone), "
            "alt_phone = CASE WHEN ? != '' THEN ? ELSE alt_phone END, "
            "sub_area = COALESCE(NULLIF(?, ''), sub_area), "
            "code = COALESCE(NULLIF(?, ''), code), "
            "status = ?, updated_at = ? WHERE id = ?",
            (
                row.get("name") or "",
                bix_phone,
                bix_phone,
                bix_phone,
                row.get("locality") or "",
                _usable_code(conn, customer_id, row.get("code") or ""),
                status,
                stamp,
                customer_id,
            ),
        )

        if _set_due(conn, customer_id, int(row.get("due_paise") or 0), actor) == "adjusted":
            adjusted += 1
        else:
            unchanged += 1

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
        f"Bix dues — {adjusted} updated, {skipped} left unmatched. "
        f"No Hathway boxes added, moved, or removed.",
        actor=actor,
        meta_json=json.dumps(summary),
    )
    return summary
