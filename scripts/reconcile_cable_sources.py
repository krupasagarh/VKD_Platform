"""One-shot STB compare: Hathway dashboard vs Bix export vs Mobize Customer_details vs platform."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app.bix_sync import _STB_RE, _norm_phone, parse_bix_customers  # noqa: E402

MOBIZE = Path(r"c:\Users\A1\Downloads\Customer_details (11).xls")
HATHWAY = Path(r"c:\Users\A1\Downloads\TotalDashboardData_2992026142955.xls")
BIX = Path(r"c:\Users\A1\Downloads\Customer_Export_96785_28_Sep_2026_170112.csv")
DB = PROJECT_DIR / "data" / "vk_platform.db"
OUT = PROJECT_DIR / "scripts" / "cable_reconcile.json"

OFFICE_NAMES = ("KRUPA", "DIGITAL NETWORK", "VK TIPTUR", "VK DIGITAL")


def stbs_from(*chunks: str) -> list[str]:
    found: list[str] = []
    for chunk in chunks:
        text = (chunk or "").upper().replace(" ", "").replace("'", "")
        for match in _STB_RE.findall(text):
            if match not in found:
                found.append(match)
    return found


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


def load_hathway(path: Path) -> dict[str, dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    out: dict[str, dict] = {}
    for i, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        parts = line.split("\t")
        if i == 0 and "STB" in line.upper():
            continue
        if len(parts) < 3:
            continue
        stb = parts[2].strip().upper().lstrip("'")
        if not _STB_RE.fullmatch(stb):
            continue
        status = parts[1].strip().upper() if len(parts) > 1 else ""
        vc = parts[3].strip().lstrip("'") if len(parts) > 3 else ""
        rmn = re.sub(r"\D", "", parts[4] if len(parts) > 4 else "")
        rmn = rmn[-10:] if len(rmn) >= 10 else rmn
        name = parts[5].strip().lstrip("'") if len(parts) > 5 else ""
        out[stb] = {"stb": stb, "status": status, "vc": vc, "phone": rmn, "name": name}
    return out


def col_index(headers: list[str], *names: str) -> int | None:
    lowered = [h.lower() for h in headers]
    for name in names:
        for i, header in enumerate(lowered):
            if name in header:
                return i
    return None


def load_mobize_raw(path: Path) -> tuple[list[dict], set[str], set[str]]:
    parser = TableParser()
    parser.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    i_id = col_index(parser.headers, "customer id")
    i_name = col_index(parser.headers, "customer name")
    i_st = col_index(parser.headers, "status")
    i_ph = col_index(parser.headers, "phone")
    i_stb = col_index(parser.headers, "stb")
    i_rm = col_index(parser.headers, "remarks")
    i_area = col_index(parser.headers, "area")
    rows: list[dict] = []
    primary: set[str] = set()
    remark_only: set[str] = set()
    for cells in parser.rows:
        if len(cells) < 8:
            continue

        def get(idx: int | None) -> str:
            return cells[idx] if idx is not None and idx < len(cells) else ""

        prim = stbs_from(get(i_stb))
        extra = stbs_from(get(i_rm))
        all_stbs = list(dict.fromkeys(prim + extra))
        primary.update(prim)
        for stb in extra:
            if stb not in prim:
                remark_only.add(stb)
        rows.append(
            {
                "code": get(i_id).lstrip("'"),
                "name": get(i_name),
                "status": get(i_st),
                "phone": _norm_phone(get(i_ph)),
                "stbs": all_stbs,
                "primary": prim,
                "extra": extra,
                "area": get(i_area),
            }
        )
    return rows, primary, remark_only


def load_platform(db: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not db.is_file():
        return out
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    for row in con.execute(
        "SELECT cn.upstream_id, cn.status, cn.card_number, c.id AS customer_id, "
        "c.code, c.name, c.phone FROM connections cn "
        "JOIN customers c ON c.id = cn.customer_id "
        "WHERE cn.provider = 'hathway'"
    ):
        stb = (row["upstream_id"] or "").upper()
        if _STB_RE.fullmatch(stb):
            out[stb] = dict(row)
    con.close()
    return out


def is_office(name: str) -> bool:
    upper = (name or "").upper()
    return any(token in upper for token in OFFICE_NAMES)


def sample_rows(stbs: set[str], n: int, hw, mz, bix, plat) -> list[dict]:
    rows = []
    for stb in sorted(stbs)[:n]:
        h = hw.get(stb, {})
        m = mz.get(stb) or {}
        b = bix.get(stb) or {}
        p = plat.get(stb) or {}
        rows.append(
            {
                "stb": stb,
                "hw_status": h.get("status", ""),
                "hw_name": h.get("name", ""),
                "hw_phone": h.get("phone", ""),
                "mz_name": m.get("name", ""),
                "mz_code": m.get("code", ""),
                "bix_name": b.get("name", ""),
                "bix_code": b.get("code", ""),
                "plat_name": p.get("name", ""),
                "plat_code": p.get("code", ""),
                "plat_status": p.get("status", ""),
            }
        )
    return rows


def main() -> int:
    hw = load_hathway(HATHWAY)
    mz_items = parse_bix_customers(MOBIZE)
    mz_raw, primary_stbs, remark_only = load_mobize_raw(MOBIZE)
    bix_items = parse_bix_customers(BIX)
    plat = load_platform(DB)

    mz_by_stb: dict[str, dict] = {}
    for item in mz_items:
        for stb in item.get("stbs") or []:
            mz_by_stb.setdefault(stb.upper(), item)

    bix_by_stb: dict[str, dict] = {}
    for item in bix_items:
        for stb in item.get("stbs") or []:
            bix_by_stb.setdefault(stb.upper(), item)

    bix_raw_stbs: set[str] = set()
    with BIX.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            bix_raw_stbs.update(
                stbs_from(
                    row.get("Settop Box Number") or "",
                    row.get("Card Number") or "",
                    row.get("Remark") or "",
                    row.get("Customer Code") or "",
                    row.get("Name") or "",
                )
            )

    H = set(hw)
    M = set(mz_by_stb) | primary_stbs | remark_only
    B = set(bix_by_stb) | bix_raw_stbs
    P = set(plat)

    buckets = {
        "all_three": H & M & B,
        "hathway_bix_not_mobize": (H & B) - M,
        "hathway_mobize_not_bix": (H & M) - B,
        "bix_mobize_not_hathway": (B & M) - H,
        "hathway_only": H - M - B,
        "bix_only": B - H - M,
        "mobize_only": M - H - B,
        "hathway_not_bix": H - B,
        "bix_not_hathway": B - H,
        "hathway_not_mobize": H - M,
        "mobize_not_hathway": M - H,
        "hathway_not_platform": H - P,
        "platform_not_hathway": P - H,
        "bix_not_platform": B - P,
        "platform_not_bix": P - B,
        "mobize_not_bix": M - B,
        "bix_not_mobize": B - M,
    }

    plat_status = Counter((row.get("status") or "") for row in plat.values())
    mz_status = Counter((item.get("status") or "").lower() for item in mz_items)
    bix_status = Counter((item.get("status") or "").lower() for item in bix_items)
    hw_status = Counter(row["status"] for row in hw.values())
    hw_phones = Counter(row["phone"] for row in hw.values() if row["phone"])

    summary = {
        "files": {
            "mobize_labeled": MOBIZE.name,
            "hathway": HATHWAY.name,
            "bix_yesterday_export": BIX.name,
        },
        "counts": {
            "hathway_unique_stbs": len(H),
            "hathway_status": dict(hw_status),
            "mobize_customers": len(mz_items),
            "mobize_raw_rows": len(mz_raw),
            "mobize_unique_stbs": len(M),
            "mobize_primary_stbs": len(primary_stbs),
            "mobize_remark_extra_stbs": len(remark_only),
            "mobize_status": dict(mz_status),
            "bix_customers": len(bix_items),
            "bix_unique_stbs": len(B),
            "bix_status": dict(bix_status),
            "platform_hathway_stbs": len(P),
            "platform_status": dict(plat_status),
        },
        "buckets": {key: len(value) for key, value in buckets.items()},
        "hathway_top_phones": hw_phones.most_common(6),
        "hathway_not_bix_office_named": sum(
            1 for stb in buckets["hathway_not_bix"] if is_office(hw[stb]["name"])
        ),
        "hathway_not_bix_active": sum(
            1 for stb in buckets["hathway_not_bix"] if hw[stb]["status"] == "ACTIVE"
        ),
        "hathway_only_office_named": sum(
            1 for stb in buckets["hathway_only"] if is_office(hw[stb]["name"])
        ),
        "samples": {
            "hathway_not_bix": sample_rows(buckets["hathway_not_bix"], 12, hw, mz_by_stb, bix_by_stb, plat),
            "bix_not_hathway": sample_rows(buckets["bix_not_hathway"], 12, hw, mz_by_stb, bix_by_stb, plat),
            "hathway_only": sample_rows(buckets["hathway_only"], 10, hw, mz_by_stb, bix_by_stb, plat),
            "hathway_not_platform": sample_rows(buckets["hathway_not_platform"], 10, hw, mz_by_stb, bix_by_stb, plat),
            "platform_not_hathway": sample_rows(buckets["platform_not_hathway"], 10, hw, mz_by_stb, bix_by_stb, plat),
            "mobize_not_hathway": sample_rows(buckets["mobize_not_hathway"], 8, hw, mz_by_stb, bix_by_stb, plat),
        },
    }
    OUT.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("files", "counts", "buckets", "hathway_top_phones", "hathway_not_bix_office_named", "hathway_not_bix_active", "hathway_only_office_named")}, indent=2))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
