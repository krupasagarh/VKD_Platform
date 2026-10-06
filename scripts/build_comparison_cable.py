"""Build comparison_cable.xlsx from the user's template.

Sheets:
  comparison_cable  In Bix | In Hathway | In Mobize  (N/T IDs only)
  BIX               In Bix, Name, Phone, Due, Plan, Expiry
  Hathway           In Hathway, Name, Phone, Due, Plan, Expiry
  Mobize            In Mobizee, Name, Phone, Due, Plan, Expiry
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

ID_RE = re.compile(r"(N\d{11}|T\d{12})", re.I)

MOBIZE = Path(r"c:\Users\A1\Downloads\Customer_details (11).xls")
HATHWAY = Path(r"c:\Users\A1\Downloads\TotalDashboardData_2992026142955.xls")
HW_MASTER = Path(r"c:\Users\A1\Downloads\Hathway_29sep.csv")
BIX = Path(r"c:\Users\A1\Downloads\Customer_Export_96785_28_Sep_2026_170112.csv")
OUT = Path(r"c:\Users\A1\Downloads\comparison_cable.xlsx")
OUT_FALLBACK = Path(r"c:\Users\A1\Downloads\comparison_cable_v2.xlsx")

MAIN_PACK_TYPES = {"DPO PLAN", "FTA PLAN"}

DETAIL_TAIL = ["Name", "Phone", "Due", "Plan ", "Expiry"]
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True)
THIN = Border(
    left=Side(style="thin", color="D0D7DE"),
    right=Side(style="thin", color="D0D7DE"),
    top=Side(style="thin", color="D0D7DE"),
    bottom=Side(style="thin", color="D0D7DE"),
)


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


def ids_from(*chunks: str) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        text = (chunk or "").upper().replace(" ", "").replace("'", "")
        for match in ID_RE.findall(text):
            value = match.upper()
            if value not in found:
                found.append(value)
    return found


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip("'").strip('"')


def phone_of(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else digits


def money_of(value: str):
    text = clean(value).replace(",", "")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return clean(value)
    if abs(number - round(number)) < 0.001:
        return int(round(number))
    return round(number, 2)


def expiry_of(value: str) -> str:
    text = clean(value).lstrip("'")
    if not text:
        return ""
    try:
        serial = float(text)
        if serial > 20000:
            day = datetime(1899, 12, 30) + timedelta(days=int(serial))
            return day.strftime("%d-%m-%Y")
    except ValueError:
        pass
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    named = re.match(r"^(\d{1,2})-([A-Z]{3})-(\d{4})$", text.upper())
    if named:
        day, mon, year = named.groups()
        return f"{int(day):02d}-{months[mon]:02d}-{year}"
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%d-%m-%Y")
        except ValueError:
            continue
    if " " in text:
        return expiry_of(text.split()[0])
    return text


def plan_of(value: str) -> str:
    lines = [clean(line) for line in str(value or "").splitlines()]
    lines = [line for line in lines if line]
    return lines[0] if lines else ""


def col_index(headers: list[str], *names: str) -> int | None:
    lowered = [h.lower() for h in headers]
    for name in names:
        for i, header in enumerate(lowered):
            if name in header:
                return i
    return None


def merge_row(store: dict[str, dict], hardware_id: str, **fields) -> None:
    current = store.setdefault(
        hardware_id,
        {"id": hardware_id, "name": "", "phone": "", "due": "", "plan": "", "expiry": ""},
    )
    for key, value in fields.items():
        if value in ("", None):
            continue
        if key == "due":
            old = current.get("due")
            if old == "" or (isinstance(value, (int, float)) and (not isinstance(old, (int, float)) or value > old)):
                current["due"] = value
            continue
        if not current.get(key):
            current[key] = value


def load_hathway_pairs(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """One box = one N STB + one T VC. Map either ID to the N-series STB."""
    t_to_n: dict[str, str] = {}
    n_to_t: dict[str, str] = {}
    for i, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
        if not line.strip() or (i == 0 and "STB" in line.upper()):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        n_ids = [item for item in ids_from(parts[2]) if item.startswith("N")]
        t_ids = [item for item in ids_from(parts[3] if len(parts) > 3 else "") if item.startswith("T")]
        if not n_ids:
            n_ids = [item for item in ids_from(parts[2], parts[3] if len(parts) > 3 else "") if item.startswith("N")]
        if not n_ids:
            continue
        stb = n_ids[0]
        if t_ids:
            add_pair(t_to_n, n_to_t, stb, t_ids[0])
    return t_to_n, n_to_t


def add_pair(t_to_n: dict[str, str], n_to_t: dict[str, str], n_id: str, t_id: str) -> None:
    if n_id.startswith("N") and t_id.startswith("T"):
        t_to_n[t_id] = n_id
        n_to_t[n_id] = t_id


def load_hathway_master(path: Path, t_to_n: dict[str, str]) -> dict[str, dict]:
    """Main pack only (DPO / FTA). Ignore a-la-carte rows if they appear."""
    packs: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            plan_type = clean(raw.get("Plan Type") or "").upper()
            if "ALA" in plan_type or "A-LA" in plan_type or "ALACARTE" in plan_type.replace(" ", ""):
                continue
            if plan_type and plan_type not in MAIN_PACK_TYPES:
                continue
            col_a = clean(raw.get("VC Id") or "")
            col_b = clean(raw.get("New STB No") or "")
            found = ids_from(col_a, col_b)
            n_ids = [item for item in found if item.startswith("N")]
            t_ids = [item for item in found if item.startswith("T")]
            hardware_id = n_ids[0] if n_ids else (canonical(t_ids[0], t_to_n) if t_ids else "")
            if not hardware_id:
                continue
            packs[hardware_id] = {
                "plan": clean(raw.get("Base Plan") or ""),
                "expiry": expiry_of(raw.get("End Date") or ""),
            }
    return packs


def canonical(hardware_id: str, t_to_n: dict[str, str]) -> str:
    value = (hardware_id or "").upper()
    if value.startswith("T") and value in t_to_n:
        return t_to_n[value]
    return value


def canonical_ids(found: list[str], t_to_n: dict[str, str]) -> list[str]:
    out: list[str] = []
    for item in found:
        mapped = canonical(item, t_to_n)
        if mapped not in out:
            out.append(mapped)
    return out


def load_bix(path: Path, t_to_n: dict[str, str]) -> tuple[list[str], dict[str, dict]]:
    ids: set[str] = set()
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            found = canonical_ids(
                ids_from(
                    raw.get("Settop Box Number") or "",
                    raw.get("Card Number") or "",
                    raw.get("Remark") or "",
                    raw.get("Membership Number") or "",
                ),
                t_to_n,
            )
            ids.update(found)
            payload = {
                "name": clean(raw.get("Name") or raw.get("Bill Name") or ""),
                "phone": phone_of(raw.get("Mobile") or ""),
                "due": money_of(raw.get("Balance Amount") or ""),
                "plan": plan_of(raw.get("Products") or ""),
                "expiry": expiry_of(raw.get("Expiry Date") or ""),
            }
            for hardware_id in found:
                merge_row(rows, hardware_id, **payload)
    return sorted(ids), rows


def load_hathway(path: Path, t_to_n: dict[str, str]) -> tuple[list[str], dict[str, dict]]:
    ids: set[str] = set()
    rows: dict[str, dict] = {}
    for i, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
        if not line.strip() or (i == 0 and "STB" in line.upper()):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        status = clean(parts[1]).upper()
        found = canonical_ids(
            ids_from(parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else ""),
            t_to_n,
        )
        phone = phone_of(parts[4] if len(parts) > 4 else "")
        name = clean(parts[5] if len(parts) > 5 else "")
        ids.update(found)
        for hardware_id in found:
            merge_row(rows, hardware_id, name=name, phone=phone, plan=status)
    return sorted(ids), rows


def load_mobize(path: Path, t_to_n: dict[str, str]) -> tuple[list[str], dict[str, dict]]:
    parser = TableParser()
    parser.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    i_name = col_index(parser.headers, "customer name")
    i_phone = col_index(parser.headers, "phone")
    i_stb = col_index(parser.headers, "stb")
    i_vc = col_index(parser.headers, "vc")
    i_due = col_index(parser.headers, "due amount")
    i_rent = col_index(parser.headers, "monthly rent")
    i_paid = col_index(parser.headers, "last paid date")
    i_rm = col_index(parser.headers, "remarks")
    ids: set[str] = set()
    rows: dict[str, dict] = {}

    def get(cells: list[str], index: int | None) -> str:
        return cells[index] if index is not None and index < len(cells) else ""

    for cells in parser.rows:
        if len(cells) < 8:
            continue
        found = canonical_ids(
            ids_from(get(cells, i_stb), get(cells, i_vc), get(cells, i_rm)),
            t_to_n,
        )
        ids.update(found)
        payload = {
            "name": clean(get(cells, i_name)),
            "phone": phone_of(get(cells, i_phone)),
            "due": money_of(get(cells, i_due)),
            "plan": money_of(get(cells, i_rent)),
            "expiry": "",
        }
        for hardware_id in found:
            merge_row(rows, hardware_id, **payload)
    return sorted(ids), rows


def style_header(ws) -> None:
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        cell.border = THIN
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def write_id_sheet(wb: Workbook, name: str, columns: list[list[str]]) -> None:
    ws = wb.create_sheet(name)
    headers = ["In Bix", "In Hathway", "In Mobize"]
    ws.append(headers)
    rows = max((len(col) for col in columns), default=0)
    for i in range(rows):
        ws.append([col[i] if i < len(col) else "" for col in columns])
    style_header(ws)
    for idx in range(1, 4):
        ws.column_dimensions[get_column_letter(idx)].width = 18


def write_detail_sheet(wb: Workbook, name: str, id_header: str, rows: dict[str, dict]) -> None:
    ws = wb.create_sheet(name)
    ws.append([id_header, *DETAIL_TAIL])
    for hardware_id in sorted(rows):
        row = rows[hardware_id]
        ws.append(
            [
                row["id"],
                row.get("name") or "",
                row.get("phone") or "",
                row.get("due") if row.get("due") != "" else "",
                row.get("plan") or "",
                row.get("expiry") or "",
            ]
        )
    style_header(ws)
    widths = [18, 28, 14, 12, 28, 14]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width


def main() -> int:
    t_to_n, n_to_t = load_hathway_pairs(HATHWAY)
    if HW_MASTER.is_file():
        with HW_MASTER.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                found = ids_from(raw.get("VC Id") or "", raw.get("New STB No") or "")
                n_ids = [item for item in found if item.startswith("N")]
                t_ids = [item for item in found if item.startswith("T")]
                if n_ids and t_ids:
                    add_pair(t_to_n, n_to_t, n_ids[0], t_ids[0])

    bix_ids, bix_rows = load_bix(BIX, t_to_n)
    hw_ids, hw_rows = load_hathway(HATHWAY, t_to_n)
    mz_ids, mz_rows = load_mobize(MOBIZE, t_to_n)
    hw_packs = load_hathway_master(HW_MASTER, t_to_n) if HW_MASTER.is_file() else {}

    for hardware_id, row in hw_rows.items():
        pack = hw_packs.get(hardware_id) or hw_packs.get(canonical(hardware_id, t_to_n))
        if pack:
            row["plan"] = pack.get("plan") or ""
            row["expiry"] = pack.get("expiry") or ""
        else:
            status = str(row.get("plan") or "").upper()
            if status not in {"ACTIVE", "INACTIVE"}:
                row["plan"] = ""
            row["expiry"] = ""

    def fill_from_bix(rows: dict[str, dict], *, replace_plan: bool) -> None:
        for hardware_id, row in rows.items():
            source = bix_rows.get(hardware_id)
            if not source:
                continue
            if source.get("plan") and (
                replace_plan
                or not row.get("plan")
                or str(row.get("plan")).upper() in {"ACTIVE", "INACTIVE"}
            ):
                row["plan"] = source["plan"]
            if source.get("expiry") and not row.get("expiry"):
                row["expiry"] = source["expiry"]

    fill_from_bix(mz_rows, replace_plan=True)

    wb = Workbook()
    default = wb.active
    wb.remove(default)
    write_id_sheet(wb, "comparison_cable", [bix_ids, hw_ids, mz_ids])
    write_detail_sheet(wb, "BIX", "In Bix", bix_rows)
    write_detail_sheet(wb, "Hathway", "In Hathway", hw_rows)
    write_detail_sheet(wb, "Mobize", "In Mobizee", mz_rows)
    target = OUT
    try:
        wb.save(target)
    except PermissionError:
        target = OUT_FALLBACK
        wb.save(target)
    print(target)
    print(f"comparison_cable  Bix {len(bix_ids)}  Hathway {len(hw_ids)}  Mobize {len(mz_ids)}")
    print(f"Hathway N-T pairs {len(t_to_n)}")
    print(f"Hathway main packs applied {sum(1 for r in hw_rows.values() if r.get('plan'))} / {len(hw_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
