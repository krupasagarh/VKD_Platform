"""Check Hathway status for STBs missing VC; write txt + CSV."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT = SCRIPTS.parent
HATHWAY_AGENT = Path(r"c:\Users\A1\Desktop\AI agent\vk_digital_hub\hathway_agent\vk_agent")

for path in (PROJECT, HATHWAY_AGENT):
    p = str(path)
    if p not in sys.path:
        sys.path.insert(0, p)

from batch_hathway_stb_status import format_one, parse_stbs  # noqa: E402
from hathway_portal import (  # noqa: E402
    audit_hathway_subscriber,
    close_hathway_browser,
    hathway_login_once,
    launch_hathway_browser,
)
from hathway_status_output_to_csv import parse_line  # noqa: E402

STB_FILE = SCRIPTS / "hathway_missing_vc_stbs.txt"
OUT_TXT = SCRIPTS / "hathway_missing_vc_status_output.txt"
OUT_CSV = SCRIPTS / "hathway_missing_vc_status.csv"


class _Args:
    stb_ids: list[str] = []
    stbs: list[str] = []
    file = str(STB_FILE)


def main() -> int:
    if not STB_FILE.is_file():
        print(f"Missing STB list: {STB_FILE}", file=sys.stderr)
        return 1

    stbs = parse_stbs(_Args())
    if not stbs:
        print("No STBs to check.", file=sys.stderr)
        return 1

    print(f"Checking {len(stbs)} STB(s)...", flush=True)
    print(f"Output -> {OUT_TXT}", flush=True)

    lines: list[str] = []
    playwright = browser = page = None
    try:
        playwright, browser, page = launch_hathway_browser(headless=True)
        if not hathway_login_once(page):
            msg = "Login failed. Check credentials and CAPTCHA (HATHWAY_USER/HATHWAY_PASS)."
            print(msg, flush=True)
            OUT_TXT.write_text(msg + "\n", encoding="utf-8")
            return 2

        with OUT_TXT.open("w", encoding="utf-8", newline="\n") as fh:
            for idx, stb_id in enumerate(stbs, start=1):
                try:
                    audit = audit_hathway_subscriber(page, stb_id)
                    line = format_one(stb_id, audit)
                except Exception as exc:
                    line = f"{stb_id}: no package present (audit exception: {exc})"
                fh.write(line + "\n")
                fh.flush()
                lines.append(line)
                print(f"[{idx}/{len(stbs)}] {line}", flush=True)
                if idx % 5 == 0:
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
    finally:
        if playwright is not None and browser is not None:
            try:
                close_hathway_browser(playwright, browser)
            except Exception:
                pass

    rows = []
    for line in lines:
        row = parse_line(line)
        if row:
            rows.append(row)

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["stb", "status", "package", "expiry_date"])
        w.writeheader()
        w.writerows(rows)

    print(f"CSV -> {OUT_CSV} ({len(rows)} rows)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
