"""Summarise Hathway N/T state vs CustomerMasterSummaryReport."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.db import connection
from app.upstream.providers import is_hybrid_hathway_stb
from scripts.import_hathway_stb_pairs import parse_report, _clean_id

REPORT = Path(r"c:\Users\A1\Downloads\CustomerMasterSummaryReport_139202623134.xls")

N_GLOB = "N[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
T_GLOB = "T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"

pairs = parse_report(REPORT)

verified_ok = []
verified_hybrid = []
updated_would_be = []  # report says should have T, platform now matches
not_in_platform = []
report_no_vc_normal = []
report_t_only = []
mismatch = []

with connection() as conn:
    platform_total = conn.execute(
        "SELECT COUNT(*) FROM connections WHERE provider='hathway'"
    ).fetchone()[0]
    platform_n_t = conn.execute(
        f"SELECT COUNT(*) FROM connections WHERE provider='hathway' "
        f"AND upper(upstream_id) GLOB '{N_GLOB}' AND upper(card_number) GLOB '{T_GLOB}'"
    ).fetchone()[0]
    platform_hybrid = conn.execute(
        "SELECT COUNT(*) FROM connections WHERE provider='hathway' "
        "AND upper(upstream_id) LIKE 'N722%'"
    ).fetchone()[0]
    platform_n_no_t = conn.execute(
        f"SELECT COUNT(*) FROM connections WHERE provider='hathway' "
        f"AND upper(upstream_id) GLOB '{N_GLOB}' "
        f"AND upper(upstream_id) NOT LIKE 'N722%' "
        f"AND (card_number IS NULL OR trim(card_number)='' "
        f"OR upper(card_number) NOT GLOB '{T_GLOB}')"
    ).fetchone()[0]

    for item in pairs:
        n = item.get("upstream_id") or ""
        t = item.get("card_number") or ""
        prob = item.get("problem") or ""

        if prob == "stb_column_is_t_only":
            report_t_only.append(item)
            continue
        if not n:
            not_in_platform.append(item)
            continue

        row = conn.execute(
            "SELECT cn.upstream_id, cn.card_number, c.name FROM connections cn "
            "JOIN customers c ON c.id = cn.customer_id "
            "WHERE cn.provider='hathway' AND upper(cn.upstream_id)=upper(?) LIMIT 1",
            (n,),
        ).fetchone()

        if row is None:
            not_in_platform.append({**item, "stb": n, "vc_report": t})
            continue

        cur_n = _clean_id(row["upstream_id"])
        cur_t = _clean_id(row["card_number"] or "")
        name = row["name"] or ""

        if is_hybrid_hathway_stb(n):
            if cur_n == n and not cur_t:
                verified_hybrid.append({"stb": n, "customer": name})
            else:
                verified_hybrid.append({"stb": n, "customer": name, "note": "hybrid"})
            continue

        if prob == "missing_vc_in_report" or not t:
            report_no_vc_normal.append({"stb": n, "customer": name, "platform_vc": cur_t})
            continue

        if cur_n == n and cur_t == t:
            verified_ok.append({"stb": n, "vc": t, "customer": name})
        elif cur_n == n and not cur_t:
            mismatch.append({"stb": n, "vc_report": t, "platform_vc": cur_t, "customer": name, "issue": "still missing T"})
        elif cur_n == n and cur_t != t:
            mismatch.append({"stb": n, "vc_report": t, "platform_vc": cur_t, "customer": name, "issue": "different T"})

print("=== EXCEL REPORT ===")
print(f"Rows in report:              {len(pairs)}")
print(f"Verified N+T match:          {len(verified_ok)}")
print(f"Verified hybrid (N722, no T): {len(verified_hybrid)}")
print(f"Not in platform:             {len(not_in_platform)}")
print(f"Normal N, no T in report:    {len(report_no_vc_normal)}")
print(f"Report T-only bad rows:      {len(report_t_only)}")
print(f"Mismatches vs report:        {len(mismatch)}")
print()
print("=== PLATFORM (all Hathway STBs) ===")
print(f"Total Hathway connections:   {platform_total}")
print(f"N + T paired (normal):       {platform_n_t}")
print(f"Hybrid N722 (no T):          {platform_hybrid}")
print(f"Normal N still missing T:    {platform_n_no_t}")
print()
print("=== IMPORT RUN (earlier today) ===")
print("VC (T) filled from report:    34")
print("Already correct before run:  500")
print("Skipped (had VC already):    1")
print()
if mismatch:
    print("MISMATCHES:")
    for m in mismatch[:10]:
        print(f"  {m}")
if report_no_vc_normal:
    print("NORMAL N — NO T IN REPORT (in platform):")
    for m in report_no_vc_normal:
        print(f"  {m['stb']} | {m['customer']} | platform VC: {m.get('platform_vc') or '-'}")
print()
print("NOT IN PLATFORM (sample):")
for m in not_in_platform[:8]:
    print(f"  {m.get('stb') or m.get('upstream_id')} | VC report: {m.get('vc_report') or m.get('card_number') or '-'}")
