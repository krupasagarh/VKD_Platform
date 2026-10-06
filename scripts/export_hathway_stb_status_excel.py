"""Export all Hathway STBs from VK Platform with live Hathway portal status to Excel.

One row per customer. Multiple STBs on the same customer appear in one row:
  STB | Customer Name | Area | Status | Multi connection/Not

STB and Status are separate columns; with several boxes, use comma-separated
values in the same order (first STB ↔ first status).

Requires Hathway credentials (HATHWAY_USER / HATHWAY_PASS) and Playwright.
This can take a long time (one portal lookup per unique STB).

Usage:
  python scripts/export_hathway_stb_status_excel.py --dry-run
  python scripts/export_hathway_stb_status_excel.py --limit 20
  python scripts/export_hathway_stb_status_excel.py
  python scripts/export_hathway_stb_status_excel.py --platform-status-only

  Resume after a failed bulk run (keep log results, recheck rest):
  python scripts/export_hathway_stb_status_excel.py --resume-from-index 292 \\
      --import-log data/exports/hathway_stb_status_run.log

  Only recheck STBs listed in a file (skips ids already in status cache):
  python scripts/export_hathway_stb_status_excel.py --check-list data/exports/hathway_stb_status_remaining.txt \\
      --import-log data/exports/hathway_stb_status_run.log --resume-from-index 292
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT.parent
VK_AGENT = REPO_ROOT / "railtel_debugger" / "vk_agent"

for path in (PROJECT, VK_AGENT):
    p = str(path)
    if p not in sys.path:
        sys.path.insert(0, p)

from app.db import connection  # noqa: E402

N_STB = re.compile(r"^N\d{11}$", re.I)
PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\] (N\d{11}): (.+)$")
DEFAULT_OUT = PROJECT / "data" / "exports" / "hathway_stb_status.xlsx"
DEFAULT_CACHE = PROJECT / "data" / "exports" / "hathway_stb_status_cache.json"
DEFAULT_REMAINING = PROJECT / "data" / "exports" / "hathway_stb_status_remaining.txt"


def _load_customers_with_stbs() -> list[dict]:
    """Group active Hathway N-series STBs by customer."""
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id AS customer_id, c.name, c.code, c.sub_area, c.area,
                   cn.id AS connection_id, upper(cn.upstream_id) AS stb,
                   cn.status AS platform_status, cn.card_number
            FROM connections cn
            JOIN customers c ON c.id = cn.customer_id
            WHERE cn.provider = 'hathway'
              AND upper(cn.upstream_id) GLOB 'N[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
            ORDER BY c.name COLLATE NOCASE, cn.upstream_id
            """
        ).fetchall()

    by_customer: dict[int, dict] = {}
    for row in rows:
        stb = (row["stb"] or "").strip().upper()
        if not N_STB.match(stb):
            continue
        cid = int(row["customer_id"])
        if cid not in by_customer:
            area = (row["sub_area"] or "").strip() or (row["area"] or "").strip()
            by_customer[cid] = {
                "customer_id": cid,
                "customer_name": (row["name"] or "").strip(),
                "code": (row["code"] or "").strip(),
                "area": area,
                "stbs": [],
                "platform_statuses": [],
            }
        entry = by_customer[cid]
        if stb not in entry["stbs"]:
            entry["stbs"].append(stb)
            entry["platform_statuses"].append((row["platform_status"] or "active").strip())

    out = list(by_customer.values())
    out.sort(key=lambda r: (r["customer_name"].lower(), r["code"]))
    return out


def collect_unique_stbs(customers: list[dict]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for cust in customers:
        for stb in cust["stbs"]:
            if stb not in seen:
                seen.add(stb)
                unique.append(stb)
    return unique


def _normalize_log_status(status: str) -> str:
    s = (status or "").strip()
    if "Locator.click" in s or "Timeout" in s:
        return "Portal timeout (recheck)"
    if s.lower().startswith("error ("):
        return s[:100]
    return s[:120]


def _read_log_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def import_run_log(path: Path, *, through_index: int) -> dict[str, str]:
    """Load STB statuses from a prior run log up to through_index (1-based)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in _read_log_text(path).splitlines():
        m = PROGRESS_RE.match(line.strip())
        if not m:
            continue
        idx = int(m.group(1))
        if idx > through_index:
            continue
        out[m.group(3).upper()] = _normalize_log_status(m.group(4))
    return out


def load_status_cache(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k.upper(): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        return {}


def save_status_cache(path: Path, cache: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def write_remaining_stb_list(path: Path, stbs: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(stbs) + ("\n" if stbs else ""), encoding="utf-8")


def merge_all_status_sources(cache_path: Path) -> dict[str, str]:
    """Status cache plus every hathway_stb_status*.log in exports (latest log wins per STB)."""
    merged = load_status_cache(cache_path)
    exports = cache_path.parent
    if exports.is_dir():
        logs = sorted(exports.glob("hathway_stb_status*.log"))
        for log in logs:
            merged.update(import_run_log(log, through_index=10_000))
    return merged


def load_stb_list_file(path: Path) -> list[str]:
    """One Hathway STB id per line (order preserved, blanks and # comments skipped)."""
    if not path.is_file():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        stb = line.upper()
        if not N_STB.match(stb):
            continue
        if stb in seen:
            continue
        seen.add(stb)
        out.append(stb)
    return out


def _portal_status_label(audit: dict) -> str:
    if not audit.get("success"):
        err = (audit.get("error") or "Unknown error").strip()
        low = err.lower()
        if "not with your lco" in low or "other lco" in low:
            return "Not in LCO account"
        if "no matching subscriber" in low or "no record" in low:
            return "Terminated / not found"
        if "no package" in low or "bouquet" in low:
            return "No package present"
        return err[:80]

    blob = (
        audit.get("hathway_tv_status")
        or audit.get("hathway_plan_status")
        or ""
    ).strip()
    if not blob:
        blob = "Active" if audit.get("is_online") else "Inactive"
    return blob


def _check_stbs_on_portal(
    stb_ids: list[str],
    *,
    cache: dict[str, str] | None = None,
    cache_path: Path | None = None,
    index_offset: int = 0,
    total_count: int | None = None,
    stb_index: dict[str, int] | None = None,
) -> dict[str, str]:
    from hathway_portal import (  # noqa: E402
        audit_hathway_subscriber,
        cleanup_hathway_ui,
        close_hathway_browser,
        hathway_login_once,
        launch_hathway_browser,
    )

    merged = dict(cache or {})
    playwright = browser = page = None
    total_all = total_count or (index_offset + len(stb_ids))
    try:
        playwright, browser, page = launch_hathway_browser(headless=True)
        if not hathway_login_once(page):
            raise RuntimeError(
                "Hathway login failed — check HATHWAY_USER / HATHWAY_PASS and CAPTCHA."
            )
        batch = len(stb_ids)
        for idx, stb in enumerate(stb_ids, start=1):
            stb = stb.upper()
            global_n = (stb_index or {}).get(stb, index_offset + idx)
            try:
                audit = audit_hathway_subscriber(page, stb)
                label = _portal_status_label(audit if isinstance(audit, dict) else {})
            except Exception as exc:
                label = f"Error ({exc})"
                try:
                    cleanup_hathway_ui(page)
                    page.wait_for_timeout(800)
                except Exception:
                    pass
            merged[stb] = label
            if cache_path:
                save_status_cache(cache_path, merged)
            print(f"[{global_n}/{total_all}] {stb}: {label}", flush=True)
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
    return merged


def _platform_status_label(platform_status: str) -> str:
    s = (platform_status or "active").strip().lower()
    if s == "terminated":
        return "Terminated (platform)"
    if s == "inactive":
        return "Inactive (platform)"
    if s == "suspended":
        return "Suspended (platform)"
    return "Active (platform record)"


def _write_xlsx(rows: list[dict], out_path: Path) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        raise SystemExit(
            "openpyxl is required for .xlsx output. Run: pip install openpyxl"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Hathway STBs"
    headers = [
        "STB",
        "Customer Name",
        "Area",
        "Status",
        "Multi connection/Not",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for row in rows:
        ws.append(
            [
                row["stb_col"],
                row["customer_name"],
                row["area"],
                row["status_col"],
                row["multi"],
            ]
        )
    ws.column_dimensions["A"].width = 48
    ws.column_dimensions["B"].width = 36
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 56
    ws.column_dimensions["E"].width = 22
    wb.save(out_path)


def _write_csv(rows: list[dict], out_path: Path) -> None:
    import csv

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "STB",
                "Customer Name",
                "Area",
                "Status",
                "Multi connection/Not",
            ],
        )
        w.writeheader()
        for row in rows:
            w.writerow(
                {
                    "STB": row["stb_col"],
                    "Customer Name": row["customer_name"],
                    "Area": row["area"],
                    "Status": row["status_col"],
                    "Multi connection/Not": row["multi"],
                }
            )


def build_rows(
    customers: list[dict],
    status_by_stb: dict[str, str],
    *,
    use_portal: bool,
) -> list[dict]:
    excel_rows: list[dict] = []
    for cust in customers:
        stbs = cust["stbs"]
        stb_col = ", ".join(stbs)
        multi = "Multi connection" if len(stbs) > 1 else "Not"
        status_parts: list[str] = []
        for i, stb in enumerate(stbs):
            if use_portal:
                status_parts.append(status_by_stb.get(stb, "Not checked"))
            else:
                ps = cust["platform_statuses"][i] if i < len(cust["platform_statuses"]) else "active"
                status_parts.append(_platform_status_label(ps))
        excel_rows.append(
            {
                "stb_col": stb_col,
                "customer_name": cust["customer_name"],
                "area": cust["area"],
                "status_col": ", ".join(status_parts),
                "multi": multi,
            }
        )
    return excel_rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output .xlsx path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only check first N customers (for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts and sample rows; no portal, no file",
    )
    parser.add_argument(
        "--platform-status-only",
        action="store_true",
        help="Skip Hathway portal; use connection status stored on platform only",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Write UTF-8 CSV instead of xlsx (no openpyxl needed)",
    )
    parser.add_argument(
        "--resume-from-index",
        type=int,
        default=0,
        metavar="N",
        help="1-based STB number from the full run to start rechecking (e.g. 292)",
    )
    parser.add_argument(
        "--import-log",
        type=Path,
        default=None,
        help="Seed cache from a prior run log for indices before --resume-from-index",
    )
    parser.add_argument(
        "--status-cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"JSON cache of STB statuses (default: {DEFAULT_CACHE.name})",
    )
    parser.add_argument(
        "--remaining-list",
        type=Path,
        default=DEFAULT_REMAINING,
        help="Write STBs still to check (one per line)",
    )
    parser.add_argument(
        "--check-list",
        type=Path,
        default=None,
        help="Only portal-check STBs from this file (one per line); skips cached ids",
    )
    parser.add_argument(
        "--from-cache-only",
        action="store_true",
        help="Skip portal; build Excel/CSV from status cache + all export logs",
    )
    args = parser.parse_args()

    customers = _load_customers_with_stbs()
    if args.limit and args.limit > 0:
        customers = customers[: args.limit]

    unique_stbs = collect_unique_stbs(customers)

    print(
        f"Customers: {len(customers)} · Unique STBs to check: {len(unique_stbs)}",
        flush=True,
    )

    if args.dry_run:
        for cust in customers[:5]:
            print(
                f"  {cust['customer_name'][:40]} | {len(cust['stbs'])} STB(s) | {cust['area']}",
            )
        if len(customers) > 5:
            print(f"  ... and {len(customers) - 5} more")
        return 0

    status_by_stb: dict[str, str] = {}
    use_portal = not args.platform_status_only and not args.from_cache_only
    if args.from_cache_only:
        status_by_stb = merge_all_status_sources(args.status_cache)
        save_status_cache(args.status_cache, status_by_stb)
        print(
            f"Building export from cache + logs ({len(status_by_stb)} STB statuses)",
            flush=True,
        )
    elif use_portal:
        resume = int(args.resume_from_index or 0)
        cache = load_status_cache(args.status_cache)
        if args.import_log and resume > 1:
            from_log = import_run_log(args.import_log, through_index=resume - 1)
            cache.update(from_log)
            run_log = PROJECT / "data" / "exports" / "hathway_stb_status_run.log"
            if run_log.is_file() and run_log.resolve() != args.import_log.resolve():
                cache.update(import_run_log(run_log, through_index=min(resume - 1, 291)))
            print(
                f"Imported {len(from_log)} STB statuses from log (indices 1–{resume - 1})",
                flush=True,
            )

        stb_index = {stb: i for i, stb in enumerate(unique_stbs, start=1)}
        if args.check_list:
            listed = load_stb_list_file(args.check_list)
            to_check = [s for s in listed if s not in cache]
            skipped = len(listed) - len(to_check)
            if not listed:
                print(f"No STBs in check list: {args.check_list}", file=sys.stderr)
                return 1
            first = listed[0]
            global_start = stb_index.get(first, resume or 1)
            print(
                f"Check list {args.check_list.name}: {len(listed)} listed, "
                f"{skipped} already cached, {len(to_check)} to check",
                flush=True,
            )
            if not to_check:
                print("All listed STBs already in cache — building export only.", flush=True)
                status_by_stb = cache
            else:
                write_remaining_stb_list(args.remaining_list, to_check)
                print(
                    f"Starting at [{global_start}/{len(unique_stbs)}] {to_check[0]} · "
                    f"remaining -> {args.remaining_list}",
                    flush=True,
                )
                print("Logging into Hathway Pack Management (one session)...", flush=True)
                status_by_stb = _check_stbs_on_portal(
                    to_check,
                    cache=cache,
                    cache_path=args.status_cache,
                    index_offset=global_start - 1,
                    total_count=len(unique_stbs),
                    stb_index=stb_index,
                )
                still = [s for s in listed if s not in status_by_stb]
                write_remaining_stb_list(args.remaining_list, still)
        elif resume > 1:
            if resume > len(unique_stbs):
                print("resume-from-index past end of STB list", file=sys.stderr)
                return 1
            to_check = unique_stbs[resume - 1 :]
            write_remaining_stb_list(args.remaining_list, to_check)
            print(
                f"Resume at [{resume}/{len(unique_stbs)}] {unique_stbs[resume - 1]} · "
                f"{len(to_check)} STB(s) left · list -> {args.remaining_list}",
                flush=True,
            )
            print("Logging into Hathway Pack Management (one session)...", flush=True)
            status_by_stb = _check_stbs_on_portal(
                to_check,
                cache=cache,
                cache_path=args.status_cache,
                index_offset=resume - 1,
                total_count=len(unique_stbs),
                stb_index=stb_index,
            )
            still = unique_stbs[resume - 1 :]
            still = [s for s in still if s not in status_by_stb]
            write_remaining_stb_list(args.remaining_list, still)
        else:
            print("Logging into Hathway Pack Management (one session)...", flush=True)
            status_by_stb = _check_stbs_on_portal(
                unique_stbs,
                cache=cache,
                cache_path=args.status_cache,
                index_offset=0,
                total_count=len(unique_stbs),
                stb_index=stb_index,
            )
    else:
        print("Using platform connection status only (no portal login).", flush=True)

    if use_portal and status_by_stb:
        disk = merge_all_status_sources(args.status_cache)
        disk.update(status_by_stb)
        status_by_stb = disk
        save_status_cache(args.status_cache, status_by_stb)

    excel_rows = build_rows(customers, status_by_stb, use_portal=use_portal or args.from_cache_only)

    out: Path = args.out
    if args.csv:
        out = out.with_suffix(".csv")
        _write_csv(excel_rows, out)
    else:
        if out.suffix.lower() != ".xlsx":
            out = out.with_suffix(".xlsx")
        _write_xlsx(excel_rows, out)

    print(f"Wrote {len(excel_rows)} customer rows -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
