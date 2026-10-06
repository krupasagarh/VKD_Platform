"""Write a 3-column CSV of N-series STB / T-series VC IDs from Bix, Hathway, Mobize."""
from __future__ import annotations

import csv
import re
from html.parser import HTMLParser
from pathlib import Path

ID_RE = re.compile(r"(N\d{11}|T\d{12})", re.I)

MOBIZE = Path(r"c:\Users\A1\Downloads\Customer_details (11).xls")
HATHWAY = Path(r"c:\Users\A1\Downloads\TotalDashboardData_2992026142955.xls")
BIX = Path(r"c:\Users\A1\Downloads\Customer_Export_96785_28_Sep_2026_170112.csv")
OUT = Path(r"c:\Users\A1\Downloads\stb_ids_bix_hathway_mobize.csv")


def ids_from(*chunks: str) -> set[str]:
    found: set[str] = set()
    for chunk in chunks:
        text = (chunk or "").upper().replace(" ", "").replace("'", "")
        found.update(match.upper() for match in ID_RE.findall(text))
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


def bix_ids(path: Path) -> list[str]:
    found: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            found |= ids_from(
                row.get("Settop Box Number") or "",
                row.get("Card Number") or "",
                row.get("Remark") or "",
                row.get("Membership Number") or "",
            )
    return sorted(found)


def hathway_ids(path: Path) -> list[str]:
    found: set[str] = set()
    for i, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines()):
        if i == 0 and "STB" in line.upper():
            continue
        parts = line.split("\t")
        stb = parts[2] if len(parts) > 2 else ""
        vc = parts[3] if len(parts) > 3 else ""
        found |= ids_from(stb, vc)
    return sorted(found)


def mobize_ids(path: Path) -> list[str]:
    parser = TableParser()
    parser.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    lowered = [h.lower() for h in parser.headers]

    def idx(*names: str) -> int | None:
        for name in names:
            for i, header in enumerate(lowered):
                if name in header:
                    return i
        return None

    i_stb = idx("stb")
    i_vc = idx("vc")
    i_rm = idx("remarks")
    found: set[str] = set()
    for cells in parser.rows:
        def get(i: int | None) -> str:
            return cells[i] if i is not None and i < len(cells) else ""

        found |= ids_from(get(i_stb), get(i_vc), get(i_rm))
    return sorted(found)


def main() -> int:
    bix = bix_ids(BIX)
    hathway = hathway_ids(HATHWAY)
    mobize = mobize_ids(MOBIZE)
    rows = max(len(bix), len(hathway), len(mobize))
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["In Bix", "In Hathway", "In Mobize"])
        for i in range(rows):
            writer.writerow(
                [
                    bix[i] if i < len(bix) else "",
                    hathway[i] if i < len(hathway) else "",
                    mobize[i] if i < len(mobize) else "",
                ]
            )
    print(f"Bix {len(bix)}  Hathway {len(hathway)}  Mobize {len(mobize)}")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
