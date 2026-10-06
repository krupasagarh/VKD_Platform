"""Dry-run: align Hathway boxes and cable dues. Does not write the database.

Hathway dashboard = which boxes exist (active / inactive).
Mobize = name, phone, and cable due for the household that owns the box.
Bix is not used as inventory or as the due.

Match a missing live box onto an existing customer in this order:
  1. An existing Hathway connection already holds that N-STB (keep it there).
  2. Customer code from Mobize (AJ-1, PR-21, ...).
  3. Mobize phone only when it is one customer, and that customer already has a
     live Hathway box or already carries that code.

Never match on the Hathway RMN (the LCO phone). Never create a customer.
Railtel, IPTV, and OTT connection rows are not in the change list.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.money import to_paise  # noqa: E402

MOBIZE = Path(r"c:\Users\A1\Downloads\Customer_details (11).xls")
HATHWAY = Path(r"c:\Users\A1\Downloads\TotalDashboardData_2992026142955.xls")
HW_PACKS = Path(r"c:\Users\A1\Downloads\Hathway_29sep.csv")
DB = PROJECT / "data" / "vk_platform.db"
OUT_JSON = PROJECT / "scripts" / "dry_run_hathway_billing.json"
OUT_CSV = PROJECT / "scripts" / "dry_run_hathway_actions.csv"

N_RE = re.compile(r"^N\d{11}$", re.I)
T_RE = re.compile(r"^T\d{12}$", re.I)
ID_RE = re.compile(r"(N\d{11}|T\d{12})", re.I)
CODE_RE = re.compile(r"\b([A-Z]{1,4}-\d+)\b", re.I)
MAIN_PACKS = {"DPO PLAN", "FTA PLAN"}
# Hathway RMN is the operator line, repeated across hundreds of boxes.
LCO_PHONES = {"7259316656", "9019563840", "9148287555"}


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip("'").strip('"')


def phone_of(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else ""


def codes_of(*chunks: str) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        for match in CODE_RE.finditer(chunk or ""):
            code = match.group(1).upper()
            if code not in found:
                found.append(code)
    return found


def ids_of(*chunks: str) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        text = (chunk or "").upper().replace(" ", "").replace("'", "").replace('"', "")
        for match in ID_RE.findall(text):
            if match not in found:
                found.append(match)
    return found


def rupees(paise: int) -> str:
    sign = "-" if int(paise) < 0 else ""
    whole, frac = divmod(abs(int(paise)), 100)
    return f"{sign}{whole}.{frac:02d}"


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._buf: list[str] = []
        self._in_th = False
        self._in_td = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "th":
            self._in_th = True
            self._buf = []
        elif tag == "td":
            self._in_td = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "th":
            self.headers.append("".join(self._buf).strip())
            self._in_th = False
        elif tag == "td":
            if self._row is not None:
                self._row.append("".join(self._buf).strip())
            self._in_td = False
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_th or self._in_td:
            self._buf.append(data)


def col_index(headers: list[str], *names: str) -> int | None:
    lowered = [h.lower() for h in headers]
    for name in names:
        for i, header in enumerate(lowered):
            if name in header:
                return i
    return None


def add_pair(t_to_n: dict[str, str], n_to_t: dict[str, str], n_id: str, t_id: str) -> None:
    if N_RE.match(n_id) and T_RE.match(t_id):
        t_to_n[t_id.upper()] = n_id.upper()
        n_to_t[n_id.upper()] = t_id.upper()


def load_pairs() -> tuple[dict[str, str], dict[str, str]]:
    t_to_n: dict[str, str] = {}
    n_to_t: dict[str, str] = {}
    text = HATHWAY.read_text(encoding="utf-8-sig", errors="replace")
    for i, line in enumerate(text.splitlines()):
        if not line.strip() or (i == 0 and "STB" in line.upper()):
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        n_ids = [item for item in ids_of(parts[2]) if item.startswith("N")]
        t_ids = [item for item in ids_of(parts[3]) if item.startswith("T")]
        if n_ids and t_ids:
            add_pair(t_to_n, n_to_t, n_ids[0], t_ids[0])
    if HW_PACKS.is_file():
        with HW_PACKS.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                found = ids_of(raw.get("VC Id") or "", raw.get("New STB No") or "")
                n_ids = [item for item in found if item.startswith("N")]
                t_ids = [item for item in found if item.startswith("T")]
                if n_ids and t_ids:
                    add_pair(t_to_n, n_to_t, n_ids[0], t_ids[0])
    return t_to_n, n_to_t


def canonical(value: str, t_to_n: dict[str, str]) -> str:
    text = clean(value).upper()
    if N_RE.match(text):
        return text
    if T_RE.match(text) and text in t_to_n:
        return t_to_n[text]
    return ""


def load_hathway(t_to_n: dict[str, str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    text = HATHWAY.read_text(encoding="utf-8-sig", errors="replace")
    for i, line in enumerate(text.splitlines()):
        if not line.strip() or (i == 0 and "STB" in line.upper()):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        n_ids = [item for item in ids_of(parts[2]) if item.startswith("N")]
        t_ids = [item for item in ids_of(parts[3] if len(parts) > 3 else "") if item.startswith("T")]
        hardware = n_ids[0] if n_ids else canonical(t_ids[0], t_to_n) if t_ids else ""
        if not hardware:
            continue
        status = clean(parts[1]).upper()
        out[hardware] = {
            "stb": hardware,
            "status": status if status in {"ACTIVE", "INACTIVE"} else status,
            "vc": t_ids[0] if t_ids else "",
            "name": clean(parts[5] if len(parts) > 5 else ""),
        }
    return out


def load_packs(t_to_n: dict[str, str]) -> dict[str, dict]:
    packs: dict[str, dict] = {}
    if not HW_PACKS.is_file():
        return packs
    with HW_PACKS.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            plan_type = clean(raw.get("Plan Type") or "").upper()
            folded = plan_type.replace(" ", "").replace("-", "")
            if "ALA" in folded or "ALACARTE" in folded:
                continue
            if plan_type and plan_type not in MAIN_PACKS:
                continue
            found = ids_of(raw.get("VC Id") or "", raw.get("New STB No") or "")
            n_ids = [item for item in found if item.startswith("N")]
            t_ids = [item for item in found if item.startswith("T")]
            hardware = n_ids[0] if n_ids else (canonical(t_ids[0], t_to_n) if t_ids else "")
            if not hardware:
                continue
            end = clean(raw.get("End Date") or "").lstrip("'")
            iso = ""
            for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    iso = datetime.strptime(end[:11].title() if "%b" in fmt else end[:10], fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            packs[hardware] = {
                "plan": clean(raw.get("Base Plan") or ""),
                "expiry": iso or end,
            }
    return packs


def load_mobize(t_to_n: dict[str, str]) -> list[dict]:
    parser = TableParser()
    parser.feed(MOBIZE.read_text(encoding="utf-8-sig", errors="replace"))
    i_id = col_index(parser.headers, "customer id")
    i_name = col_index(parser.headers, "customer name")
    i_phone = col_index(parser.headers, "phone")
    i_stb = col_index(parser.headers, "stb")
    i_vc = col_index(parser.headers, "vc")
    i_due = col_index(parser.headers, "due amount", "balance")
    i_rm = col_index(parser.headers, "remarks")
    rows: list[dict] = []

    def get(cells: list[str], index: int | None) -> str:
        return cells[index] if index is not None and index < len(cells) else ""

    for cells in parser.rows:
        if len(cells) < 8:
            continue
        found = []
        for item in ids_of(get(cells, i_stb), get(cells, i_vc), get(cells, i_rm)):
            hardware = canonical(item, t_to_n) or (item if item.startswith("N") else "")
            if hardware and hardware not in found:
                found.append(hardware)
        identity = clean(get(cells, i_id))
        name = clean(get(cells, i_name))
        codes = codes_of(identity, name, get(cells, i_rm))
        if not found and not codes and not name:
            continue
        rows.append(
            {
                "code": codes[0] if codes else "",
                "codes": codes,
                "name": name,
                "phone": phone_of(get(cells, i_phone)),
                "due_paise": to_paise(get(cells, i_due) or "0"),
                "stbs": found,
                "key": (codes[0] if codes else "") or phone_of(get(cells, i_phone)) or name,
            }
        )
    return rows


def open_ro(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_platform(conn: sqlite3.Connection, t_to_n: dict[str, str]) -> dict:
    customers = []
    by_id = {}
    by_code: dict[str, dict] = {}
    by_phone: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT id, code, name, phone, status FROM customers ORDER BY id"
    ):
        item = {
            "id": int(row["id"]),
            "code": (row["code"] or "").strip().upper(),
            "name": row["name"] or "",
            "phone": phone_of(row["phone"] or ""),
            "status": row["status"] or "",
        }
        customers.append(item)
        by_id[item["id"]] = item
        if item["code"]:
            by_code.setdefault(item["code"], item)
        if item["phone"]:
            by_phone.setdefault(item["phone"], []).append(item)

    # Some households keep the letter code in the name, e.g. "Manoj, (AJ-27)",
    # while customers.code is still the old numeric Bix id.
    by_name_code: dict[str, list[dict]] = {}
    for item in customers:
        for code in codes_of(item["name"]):
            if code == item["code"] or code in by_code:
                continue
            by_name_code.setdefault(code, []).append(item)

    connections = []
    for row in conn.execute(
        "SELECT cn.id, cn.customer_id, cn.provider, cn.upstream_id, cn.card_number, "
        "cn.status, cn.upstream_plan_name, cn.expiry_date, cn.package_id, "
        "p.name AS package_name "
        "FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "ORDER BY cn.id"
    ):
        connections.append(
            {
                "id": int(row["id"]),
                "customer_id": int(row["customer_id"]),
                "provider": (row["provider"] or "").strip().lower(),
                "upstream_id": clean(row["upstream_id"]).upper(),
                "card_number": clean(row["card_number"]).upper(),
                "status": (row["status"] or "").strip().lower(),
                "plan": row["upstream_plan_name"] or "",
                "package_name": row["package_name"] or "",
                "package_id": row["package_id"],
                "expiry": (row["expiry_date"] or "")[:10],
            }
        )

    billed: dict[int, tuple[int, int]] = {}
    for row in conn.execute(
        "SELECT customer_id, COALESCE(SUM(total_paise), 0) AS billed, "
        "COALESCE(SUM(paid_paise), 0) AS allocated "
        "FROM bills WHERE status != 'cancelled' GROUP BY customer_id"
    ):
        billed[int(row["customer_id"])] = (int(row["billed"]), int(row["allocated"]))
    paid: dict[int, int] = {}
    for row in conn.execute(
        "SELECT customer_id, COALESCE(SUM(amount_paise), 0) AS paid "
        "FROM payments GROUP BY customer_id"
    ):
        paid[int(row["customer_id"])] = int(row["paid"])

    def net_due(customer_id: int) -> int:
        total, allocated = billed.get(customer_id, (0, 0))
        total_paid = paid.get(customer_id, 0)
        outstanding = max(0, total - allocated)
        credit = max(0, total_paid - allocated)
        return outstanding - credit

    return {
        "customers": customers,
        "by_id": by_id,
        "by_code": by_code,
        "by_name_code": by_name_code,
        "by_phone": by_phone,
        "connections": connections,
        "net_due": net_due,
    }


def other_connections(connections: list[dict], customer_id: int) -> str:
    parts = []
    for row in connections:
        if row["customer_id"] != customer_id or row["provider"] == "hathway":
            continue
        parts.append(f"{row['provider']}:{row['id']}:{row['upstream_id']}")
    return "; ".join(parts)


def _person_text(value: str) -> str:
    """Drop household codes so an area tag like (SVP-5) is not treated as a name."""
    text = re.sub(r"\([^)]*\)", " ", value or "")
    text = CODE_RE.sub(" ", text)
    return text


def name_contains(left: str, right: str) -> bool:
    """True when one person's name sits inside the other."""

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", _person_text(value).lower())

    a, b = norm(left), norm(right)
    if len(a) < 4 or len(b) < 4:
        return False
    return a in b or b in a


def names_share_token(left: str, right: str) -> bool:
    def tokens(value: str) -> set[str]:
        return {part for part in re.sub(r"[^a-z0-9 ]", " ", _person_text(value).lower()).split() if len(part) > 2}

    a, b = tokens(left), tokens(right)
    if not a or not b:
        return True
    return bool(a & b)


def main() -> int:
    before = DB.stat()
    t_to_n, n_to_t = load_pairs()
    hathway = load_hathway(t_to_n)
    packs = load_packs(t_to_n)
    mobize = load_mobize(t_to_n)
    mobize_by_stb: dict[str, list[dict]] = {}
    for row in mobize:
        for stb in row["stbs"]:
            mobize_by_stb.setdefault(stb, []).append(row)

    conn = open_ro(DB)
    try:
        platform = load_platform(conn, t_to_n)
    finally:
        conn.close()

    customers = platform["by_id"]
    by_code = platform["by_code"]
    by_name_code = platform["by_name_code"]
    by_phone = platform["by_phone"]
    connections = platform["connections"]
    net_due = platform["net_due"]

    hathway_rows = [row for row in connections if row["provider"] == "hathway"]
    other_rows = [row for row in connections if row["provider"] != "hathway"]
    other_fingerprint = [
        (row["id"], row["customer_id"], row["provider"], row["upstream_id"]) for row in other_rows
    ]

    by_canonical: dict[str, list[dict]] = {}
    for row in hathway_rows:
        hardware = canonical(row["upstream_id"], t_to_n)
        row["canonical"] = hardware
        if hardware:
            by_canonical.setdefault(hardware, []).append(row)

    actions: list[dict] = []
    kept_customer_stbs: dict[int, set[str]] = {}
    attached_stbs: dict[str, int] = {}

    def add_action(**fields) -> None:
        customer_id = fields.get("customer_id")
        cust = customers.get(customer_id) if customer_id else None
        actions.append(
            {
                "customer_id": customer_id or "",
                "code": (cust or {}).get("code", "") if cust else fields.get("code", ""),
                "platform_name": (cust or {}).get("name", ""),
                "other_connections": other_connections(connections, customer_id) if customer_id else "",
                **fields,
            }
        )

    # Boxes already on the platform.
    for hardware, rows in sorted(by_canonical.items()):
        live = hathway.get(hardware)
        same_customer = len({row["customer_id"] for row in rows}) == 1
        n_rows = [row for row in rows if N_RE.match(row["upstream_id"])]
        if live and n_rows and same_customer:
            primary = n_rows[0]
            kept_customer_stbs.setdefault(primary["customer_id"], set()).add(hardware)
            for extra in rows:
                if extra["id"] == primary["id"]:
                    continue
                if extra["status"] != "terminated":
                    add_action(
                        section="stb",
                        action="terminate",
                        customer_id=extra["customer_id"],
                        stb=extra["upstream_id"],
                        connection_id=extra["id"],
                        match="duplicate_of_" + hardware,
                        hathway_status=live["status"],
                        status_from=extra["status"],
                        status_to="terminated",
                        note=f"Same box as {hardware}; keep connection {primary['id']}",
                    )
            continue
        if live and len(rows) == 1 and not N_RE.match(rows[0]["upstream_id"]):
            row = rows[0]
            kept_customer_stbs.setdefault(row["customer_id"], set()).add(hardware)
            add_action(
                section="stb",
                action="canonicalize",
                customer_id=row["customer_id"],
                stb=hardware,
                connection_id=row["id"],
                match="t_to_n",
                hathway_status=live["status"],
                status_from=row["status"],
                status_to="active" if live["status"] == "ACTIVE" else "inactive",
                note=f"Store upstream_id {row['upstream_id']} as {hardware}",
            )
            continue
        if live and len(rows) > 1:
            add_action(
                section="review",
                action="review",
                customer_id=rows[0]["customer_id"],
                stb=hardware,
                connection_id=";".join(str(row["id"]) for row in rows),
                match="duplicate_customers" if not same_customer else "duplicate_rows",
                hathway_status=live["status"],
                note="Live box is on more than one Hathway row; no move",
            )
            if same_customer:
                kept_customer_stbs.setdefault(rows[0]["customer_id"], set()).add(hardware)
            continue
        if not live:
            for row in rows:
                if row["status"] == "terminated":
                    add_action(
                        section="stb",
                        action="already_terminated",
                        customer_id=row["customer_id"],
                        stb=row["canonical"] or row["upstream_id"],
                        connection_id=row["id"],
                        match="not_on_hathway",
                        status_from=row["status"],
                        status_to="terminated",
                        note="Already terminated; no write",
                    )
                else:
                    add_action(
                        section="stb",
                        action="terminate",
                        customer_id=row["customer_id"],
                        stb=row["canonical"] or row["upstream_id"],
                        connection_id=row["id"],
                        match="not_on_hathway",
                        status_from=row["status"],
                        status_to="terminated",
                        note="N-STB is not on the live Hathway dump",
                    )

    # Platform Hathway ids that are neither N nor a known T.
    for row in hathway_rows:
        if row.get("canonical"):
            continue
        if row["status"] == "terminated":
            continue
        add_action(
            section="stb",
            action="terminate",
            customer_id=row["customer_id"],
            stb=row["upstream_id"],
            connection_id=row["id"],
            match="not_on_hathway",
            status_from=row["status"],
            status_to="terminated",
            note="Id is not a live N-STB",
        )

    # Live boxes missing from the platform.
    for hardware, live in sorted(hathway.items()):
        if hardware in by_canonical:
            continue
        mz_rows = mobize_by_stb.get(hardware) or []
        if not mz_rows:
            add_action(
                section="stb",
                action="unmatched",
                stb=hardware,
                match="no_mobize",
                hathway_status=live["status"],
                note=f"Hathway name {live['name']}; no customer created",
            )
            continue

        code_hits: list[tuple[dict, str, dict, str]] = []
        seen_ids: set[int] = set()
        for mz in mz_rows:
            for code in mz["codes"]:
                cust = by_code.get(code)
                how = "code"
                if cust is None:
                    named = by_name_code.get(code) or []
                    if len(named) == 1:
                        cust = named[0]
                        how = "code_in_name"
                    elif len(named) > 1:
                        add_action(
                            section="review",
                            action="review",
                            stb=hardware,
                            match="code_in_name_ambiguous",
                            hathway_status=live["status"],
                            note=f"Code {code} appears on more than one customer name",
                        )
                        cust = None
                        break
                if cust and cust["id"] not in seen_ids:
                    seen_ids.add(cust["id"])
                    code_hits.append((cust, code, mz, how))
        if len(code_hits) > 1:
            add_action(
                section="review",
                action="review",
                stb=hardware,
                match="code_conflict",
                hathway_status=live["status"],
                note="Mobize codes point at different customers: "
                + ", ".join(f"{code}→{cust['id']}" for cust, code, _mz, _how in code_hits),
            )
            continue
        if len(code_hits) == 1:
            cust, code, mz, how = code_hits[0]
            same_person = names_share_token(cust["name"], mz["name"]) or name_contains(cust["name"], mz["name"])
            if not same_person:
                add_action(
                    section="review",
                    action="attach_held",
                    customer_id=cust["id"],
                    code=code,
                    stb=hardware,
                    match=how,
                    hathway_status=live["status"],
                    mobize_name=mz["name"],
                    note=(
                        f"Code {code} is on this customer, but the name is "
                        f"{cust['name']!r} here and {mz['name']!r} in Mobize. Box not attached."
                    ),
                )
                continue
            attached_stbs[hardware] = cust["id"]
            kept_customer_stbs.setdefault(cust["id"], set()).add(hardware)
            add_action(
                section="stb",
                action="attach",
                customer_id=cust["id"],
                code=code,
                stb=hardware,
                match=how,
                hathway_status=live["status"],
                status_to="active" if live["status"] == "ACTIVE" else "inactive",
                mobize_name=mz["name"],
                note="Attach live box onto the household that already has this code",
            )
            continue

        phone_hits: list[tuple[dict, dict, str]] = []
        phone_rejected: list[str] = []
        seen_phone_customers: set[int] = set()
        for mz in mz_rows:
            phone = mz["phone"]
            if not phone or phone in LCO_PHONES:
                if phone in LCO_PHONES:
                    phone_rejected.append("lco_phone")
                continue
            matches = by_phone.get(phone) or []
            if len(matches) != 1:
                phone_rejected.append("phone_not_unique" if matches else "phone_unknown")
                continue
            cust = matches[0]
            if cust["id"] in seen_phone_customers:
                continue
            seen_phone_customers.add(cust["id"])
            providers = {row["provider"] for row in connections if row["customer_id"] == cust["id"]}
            prepaid_only = bool(providers & {"railtel", "iptv", "ott"}) and "hathway" not in providers
            code_ok = bool(cust["code"] and cust["code"] in mz["codes"])
            has_hw = "hathway" in providers
            if prepaid_only and not code_ok:
                phone_rejected.append(
                    "phone_refused_prepaid_only:" + (cust["code"] or str(cust["id"]))
                )
            elif has_hw and names_share_token(cust["name"], mz["name"]):
                phone_hits.append((cust, mz, "phone_existing_hathway"))
            elif not prepaid_only and name_contains(cust["name"], mz["name"]):
                phone_hits.append((cust, mz, "phone_same_household"))
            else:
                phone_rejected.append(
                    "phone_name_differs:" + (cust["code"] or str(cust["id"])) + ":" + cust["name"]
                )
        if len(phone_hits) == 1:
            cust, mz, how = phone_hits[0]
            attached_stbs[hardware] = cust["id"]
            kept_customer_stbs.setdefault(cust["id"], set()).add(hardware)
            add_action(
                section="stb",
                action="attach",
                customer_id=cust["id"],
                stb=hardware,
                match=how,
                hathway_status=live["status"],
                status_to="active" if live["status"] == "ACTIVE" else "inactive",
                mobize_name=mz["name"],
                note=(
                    "Unique Mobize phone, same name. "
                    "Not used when the only household on that phone is Railtel, IPTV, or OTT."
                ),
            )
            continue
        add_action(
            section="stb",
            action="unmatched",
            stb=hardware,
            match="no_safe_customer",
            hathway_status=live["status"],
            mobize_name=mz_rows[0]["name"],
            note="; ".join(phone_rejected) or "No code on an existing customer",
        )

    # Status, card, and main-pack plan on boxes we are keeping (not new attaches).
    for hardware, rows in by_canonical.items():
        live = hathway.get(hardware)
        if not live:
            continue
        if len(rows) != 1:
            continue
        row = rows[0]
        if not N_RE.match(row["upstream_id"]):
            continue
        target_status = "active" if live["status"] == "ACTIVE" else "inactive"
        card = n_to_t.get(hardware) or live.get("vc") or ""
        pack = packs.get(hardware) or {}
        notes = []
        plan_from = row["plan"] or row["package_name"]
        plan_to = pack.get("plan") or ""
        expiry_to = pack.get("expiry") or ""
        if row["status"] != target_status:
            notes.append(f"status {row['status']}→{target_status}")
        if card and row["card_number"] != card:
            if row["card_number"] and row["card_number"] != card:
                notes.append(f"card conflict {row['card_number']} vs {card}")
                card = ""
            else:
                notes.append(f"card → {card}")
        if plan_to and plan_from != plan_to:
            notes.append("plan from Hathway main pack")
        if expiry_to and row["expiry"] != expiry_to:
            notes.append("expiry from Hathway main pack")
        mz_rows = mobize_by_stb.get(hardware) or []
        mz_codes = sorted({code for mz in mz_rows for code in mz["codes"]})
        cust = customers[row["customer_id"]]
        elsewhere = []
        for code in mz_codes:
            other = by_code.get(code)
            if other and other["id"] != cust["id"]:
                elsewhere.append(f"{code}→{other['id']} {other['name']}")
        if elsewhere:
            add_action(
                section="review",
                action="review",
                customer_id=row["customer_id"],
                stb=hardware,
                connection_id=row["id"],
                match="stb_kept",
                note=(
                    f"Box stays on {cust['code'] or cust['id']} {cust['name']} "
                    f"(already this Hathway connection). Mobize code is also on "
                    + "; ".join(elsewhere)
                    + ". Not moved."
                ),
            )
        if not notes:
            continue
        add_action(
            section="stb",
            action="update",
            customer_id=row["customer_id"],
            stb=hardware,
            connection_id=row["id"],
            match="existing_n",
            hathway_status=live["status"],
            status_from=row["status"],
            status_to=target_status if row["status"] != target_status else row["status"],
            plan_from=plan_from,
            plan_to=plan_to,
            expiry_from=row["expiry"],
            expiry_to=expiry_to if expiry_to and row["expiry"] != expiry_to else row["expiry"],
            note="; ".join(notes),
        )

    # Mobize identity + due, only where a live Hathway box remains on that customer.
    mobize_used: dict[str, list[int]] = {}
    due_targets: dict[int, dict] = {}
    for customer_id, stbs in kept_customer_stbs.items():
        hits: list[dict] = []
        seen_keys: set[str] = set()
        for stb in stbs:
            for mz in mobize_by_stb.get(stb) or []:
                if mz["key"] in seen_keys:
                    continue
                seen_keys.add(mz["key"])
                hits.append(mz)
        cust = customers[customer_id]
        if not hits and cust["code"]:
            for mz in mobize:
                if cust["code"] in mz["codes"] and mz["key"] not in seen_keys:
                    seen_keys.add(mz["key"])
                    hits.append(mz)
        if len(hits) != 1:
            if len(hits) > 1:
                add_action(
                    section="review",
                    action="review",
                    customer_id=customer_id,
                    match="mobize_conflict",
                    note="More than one Mobize household for this customer's live boxes: "
                    + ", ".join(f"{mz['code'] or mz['name']} due {rupees(mz['due_paise'])}" for mz in hits),
                )
            continue
        mz = hits[0]
        due_targets[customer_id] = mz
        mobize_used.setdefault(mz["key"], []).append(customer_id)

    for key, customer_ids in mobize_used.items():
        if len(customer_ids) <= 1:
            continue
        for customer_id in customer_ids:
            due_targets.pop(customer_id, None)
            add_action(
                section="review",
                action="review",
                customer_id=customer_id,
                match="mobize_shared",
                note=f"Mobize row {key} also matches customers {customer_ids}; due not copied onto both",
            )

    for customer_id, mz in sorted(due_targets.items()):
        cust = customers[customer_id]
        current = net_due(customer_id)
        target = int(mz["due_paise"])
        phone_to = mz["phone"] if mz["phone"] and mz["phone"] not in LCO_PHONES else ""
        name_to = mz["name"]
        name_change = bool(name_to) and name_to.strip() != (cust["name"] or "").strip()
        phone_change = bool(phone_to) and phone_to != cust["phone"]
        due_change = current != target
        if not (due_change or name_change or phone_change):
            continue
        if name_to and not names_share_token(cust["name"], name_to) and not name_contains(cust["name"], name_to):
            add_action(
                section="review",
                action="overlay_held",
                customer_id=customer_id,
                match="name_clash",
                mobize_name=name_to,
                phone_from=cust["phone"],
                due_from=rupees(current),
                due_to=rupees(target),
                note=(
                    f"Mobize name {name_to!r} does not match {cust['name']!r}. "
                    f"Due would move {rupees(current)}→{rupees(target)}. Not applied."
                ),
            )
            continue
        warn = ""
        providers = sorted({row["provider"] for row in connections if row["customer_id"] == customer_id})
        if "hathway" not in providers and customer_id not in attached_stbs.values():
            add_action(
                section="review",
                action="review",
                customer_id=customer_id,
                match="no_hathway",
                note="Due not set; customer would have no Hathway box",
            )
            continue
        add_action(
            section="customer",
            action="overlay",
            customer_id=customer_id,
            match="stb_or_code",
            mobize_name=name_to,
            phone_from=cust["phone"],
            phone_to=phone_to or cust["phone"],
            due_from=rupees(current),
            due_to=rupees(target),
            note=warn
            + (
                f"due {rupees(current)}→{rupees(target)}; "
                f"name {cust['name']!r}→{name_to!r}; "
                f"phone {cust['phone'] or '—'}→{phone_to or cust['phone'] or '—'}"
            ),
        )

    # Houses that lose every live Hathway box and still show a balance.
    for cust in platform["customers"]:
        if cust["id"] in kept_customer_stbs:
            continue
        had = [row for row in hathway_rows if row["customer_id"] == cust["id"]]
        if not had:
            continue
        current = net_due(cust["id"])
        if current == 0:
            continue
        providers = sorted({row["provider"] for row in other_rows if row["customer_id"] == cust["id"]})
        add_action(
            section="review",
            action="due_left",
            customer_id=cust["id"],
            due_from=rupees(current),
            due_to=rupees(current),
            note=(
                "No live Hathway box remains, so Mobize due was not applied. "
                f"Balance stays {rupees(current)}. Other services: {', '.join(providers) or 'none'}."
            ),
        )

    # Prove the plan does not touch non-Hathway rows.
    touched_ids = set()
    for action in actions:
        if action.get("section") == "stb" and action.get("connection_id"):
            raw = str(action["connection_id"])
            if raw.isdigit():
                touched_ids.add(int(raw))
    other_ids = {row["id"] for row in other_rows}
    leaked = sorted(touched_ids & other_ids)
    moved = [
        action for action in actions
        if action.get("action") in {"attach", "terminate", "canonicalize", "update"}
        and "customer_id" in action
        and any(
            row["id"] == action.get("connection_id") and row["customer_id"] != action.get("customer_id")
            for row in connections
            if isinstance(action.get("connection_id"), int)
        )
    ]

    def count(section: str, action: str) -> int:
        return sum(1 for row in actions if row.get("section") == section and row.get("action") == action)

    summary = {
        "dry_run": True,
        "writes": 0,
        "hathway_live_n": len(hathway),
        "hathway_active": sum(1 for row in hathway.values() if row["status"] == "ACTIVE"),
        "hathway_inactive": sum(1 for row in hathway.values() if row["status"] == "INACTIVE"),
        "pairs": len(t_to_n),
        "main_packs": len(packs),
        "mobize_rows": len(mobize),
        "platform_hathway_rows": len(hathway_rows),
        "platform_other_rows": len(other_rows),
        "terminate": count("stb", "terminate"),
        "already_terminated": count("stb", "already_terminated"),
        "attach": count("stb", "attach"),
        "attach_held": count("review", "attach_held"),
        "overlay_held": count("review", "overlay_held"),
        "canonicalize": count("stb", "canonicalize"),
        "update_kept_box": count("stb", "update"),
        "unmatched_live_boxes": count("stb", "unmatched"),
        "customer_overlays": count("customer", "overlay"),
        "reviews": count("review", "review") + count("review", "due_left"),
        "other_connection_ids_unchanged": len(other_rows),
        "other_connection_ids_in_plan": leaked,
        "existing_hathway_customer_moves": len(moved),
        "other_fingerprint_sha": __import__("hashlib").sha256(
            json.dumps(other_fingerprint).encode("utf-8")
        ).hexdigest(),
    }

    fieldnames = [
        "section", "action", "customer_id", "code", "platform_name", "mobize_name",
        "phone_from", "phone_to", "stb", "connection_id", "match", "hathway_status",
        "status_from", "status_to", "due_from", "due_to", "plan_from", "plan_to",
        "expiry_from", "expiry_to", "other_connections", "note",
    ]
    with OUT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in actions:
            writer.writerow({key: row.get(key, "") for key in fieldnames})

    payload = {"summary": summary, "actions": actions}
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    after = DB.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        print("ERROR: database file changed during the dry-run", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    print(f"csv {OUT_CSV}")
    print(f"json {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
