"""Convert batch_hathway_stb_status.py console lines to a simple CSV."""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

ACTIVE_RE = re.compile(
    r"^(?P<stb>N\d{11}|T\d{12}): Active — STB/Mac: (?P<mac>[^,]+), Pack: (?P<pack>[^,]+), Valid upto: (?P<expiry>[^,]+), LCO Price: (?P<price>.+)$"
)
NO_PKG_RE = re.compile(r"^(?P<stb>N\d{11}|T\d{12}): no package present(?: \((?P<detail>.*)\))?$")
TERM_RE = re.compile(
    r"^(?P<stb>N\d{11}|T\d{12}): terminated / not found \((?P<detail>.*)\)$"
)
LCO_RE = re.compile(
    r"^(?P<stb>N\d{11}|T\d{12}): not in your LCO account \((?P<detail>.*)\)$"
)


def parse_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("Warning:") or line.startswith("Login failed"):
        return None

    m = ACTIVE_RE.match(line)
    if m:
        return {
            "stb": m.group("stb"),
            "status": "Active",
            "package": m.group("pack").strip(),
            "expiry_date": m.group("expiry").strip(),
        }

    m = TERM_RE.match(line)
    if m:
        return {
            "stb": m.group("stb"),
            "status": "Terminated / not found",
            "package": "",
            "expiry_date": "",
        }

    m = LCO_RE.match(line)
    if m:
        return {
            "stb": m.group("stb"),
            "status": "Not in LCO account",
            "package": "",
            "expiry_date": "",
        }

    m = NO_PKG_RE.match(line)
    if m:
        detail = (m.group("detail") or "").strip()
        status = "No package present"
        if detail:
            status = f"No package present ({detail})"
        return {
            "stb": m.group("stb"),
            "status": status,
            "package": "",
            "expiry_date": "",
        }

    if ": " in line:
        stb, rest = line.split(": ", 1)
        return {
            "stb": stb.strip(),
            "status": rest.strip(),
            "package": "",
            "expiry_date": "",
        }
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="inp", required=True, help="Console output txt from batch check")
    parser.add_argument("--out", required=True, help="Output CSV path")
    args = parser.parse_args()

    rows: list[dict] = []
    for line in Path(args.inp).read_text(encoding="utf-8", errors="replace").splitlines():
        row = parse_line(line)
        if row:
            rows.append(row)

    out = Path(args.out)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["stb", "status", "package", "expiry_date"])
        w.writeheader()
        w.writerows(rows)

    print(f"Parsed {len(rows)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
