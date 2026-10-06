"""Pair Hathway N (STB) and T (VC) ids from CustomerMasterSummaryReport export.

Updates connections.upstream_id (N) and connections.card_number (T).
Does not change package or expiry.

Usage:
  python scripts/import_hathway_stb_pairs.py --dry-run path/to/report.xls
  python scripts/import_hathway_stb_pairs.py path/to/report.xls
  python scripts/import_hathway_stb_pairs.py --only-missing path/to/report.xls
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from app.bix_sync import read_bix_file  # noqa: E402
from app.db import connection, transaction  # noqa: E402
from app.upstream.providers import is_hybrid_hathway_stb  # noqa: E402

N_RE = re.compile(r"^N\d{11}$", re.I)
T_RE = re.compile(r"^T\d{12}$", re.I)

HEADER_ALIASES = {
    "stb": ("stb id", "stb no", "stb number", "settop box", "set top box"),
    "vc": ("vc id", "vc number", "viewing card", "card number", "smart card"),
}


def _norm_header(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _map_columns(headers: list[str]) -> tuple[str, str]:
    norm = {_norm_header(h): h for h in headers}
    stb_col = vc_col = ""
    for alias in HEADER_ALIASES["stb"]:
        if alias in norm:
            stb_col = norm[alias]
            break
    for alias in HEADER_ALIASES["vc"]:
        if alias in norm:
            vc_col = norm[alias]
            break
    if not stb_col or not vc_col:
        raise ValueError(
            f"Could not find STB ID and VC ID columns. Headers: {headers!r}"
        )
    return stb_col, vc_col


def _clean_id(value: str) -> str:
    return (value or "").strip().upper().lstrip("'\"")


def _classify_pair(stb_raw: str, vc_raw: str) -> tuple[str, str, str | None]:
    """Return (upstream_n, card_t, problem)."""
    stb = _clean_id(stb_raw)
    vc = _clean_id(vc_raw)
    if N_RE.match(stb):
        n = stb
        if vc and T_RE.match(vc):
            return n, vc, None
        if not vc:
            if is_hybrid_hathway_stb(n):
                return n, "", "hybrid_no_vc"
            return n, "", "missing_vc_in_report"
        return n, "", f"bad_vc:{vc}"
    if T_RE.match(stb) and (not vc or stb == vc):
        return "", stb, "stb_column_is_t_only"
    if T_RE.match(stb) and N_RE.match(vc):
        return vc, stb, "swapped_in_report"
    return stb, vc, f"unrecognised_stb:{stb or 'empty'}"


def parse_report(path: Path) -> list[dict]:
    headers, rows = read_bix_file(path)
    stb_col, vc_col = _map_columns(headers)
    out: list[dict] = []
    for row in rows:
        n, t, problem = _classify_pair(row.get(stb_col, ""), row.get(vc_col, ""))
        if not n and not t:
            continue
        out.append(
            {
                "upstream_id": n,
                "card_number": t,
                "problem": problem,
            }
        )
    return out


def analyse(pairs: list[dict]) -> dict:
    stats = {
        "report_rows": len(pairs),
        "matched": 0,
        "would_update_card": 0,
        "already_ok": 0,
        "missing_vc_in_report": 0,
        "missing_vc_on_platform": 0,
        "not_in_platform": 0,
        "conflicts": 0,
        "t_only_rows": 0,
        "bad_report_rows": 0,
    }
    with connection() as conn:
        for item in pairs:
            prob = item.get("problem") or ""
            if prob == "missing_vc_in_report":
                stats["missing_vc_in_report"] += 1
            elif prob == "hybrid_no_vc":
                pass
            elif prob == "stb_column_is_t_only":
                stats["t_only_rows"] += 1
                stats["bad_report_rows"] += 1
            elif prob:
                stats["bad_report_rows"] += 1

            n = item.get("upstream_id") or ""
            t = item.get("card_number") or ""
            if not n:
                stats["not_in_platform"] += 1
                continue
            row = conn.execute(
                "SELECT upstream_id, card_number FROM connections "
                "WHERE provider = 'hathway' AND upper(upstream_id) = upper(?) LIMIT 1",
                (n,),
            ).fetchone()
            if row is None:
                stats["not_in_platform"] += 1
                continue
            stats["matched"] += 1
            cur_n = _clean_id(row["upstream_id"])
            cur_t = _clean_id(row["card_number"] or "")
            if cur_n == n and (cur_t == t or (is_hybrid_hathway_stb(n) and not t)):
                stats["already_ok"] += 1
            elif t and cur_t and cur_t != t:
                stats["conflicts"] += 1
            elif t and not cur_t:
                stats["would_update_card"] += 1
            elif not t and not cur_t:
                if not is_hybrid_hathway_stb(n):
                    stats["missing_vc_on_platform"] += 1
    return stats


def apply(pairs: list[dict], *, only_missing: bool = False) -> dict:
    stamp = now_iso()
    stats = {
        "report_rows": len(pairs),
        "matched": 0,
        "updated_card": 0,
        "already_ok": 0,
        "skipped_only_missing": 0,
        "not_in_platform": 0,
        "conflicts": 0,
        "report_problems": 0,
    }
    with transaction() as conn:
        for item in pairs:
            if item.get("problem"):
                stats["report_problems"] += 1
            n = item.get("upstream_id") or ""
            t = item.get("card_number") or ""
            if not n:
                stats["not_in_platform"] += 1
                continue
            row = conn.execute(
                "SELECT id, upstream_id, card_number FROM connections "
                "WHERE provider = 'hathway' AND upper(upstream_id) = upper(?) LIMIT 1",
                (n,),
            ).fetchone()
            if row is None:
                stats["not_in_platform"] += 1
                continue
            stats["matched"] += 1
            cur_n = _clean_id(row["upstream_id"])
            cur_t = _clean_id(row["card_number"] or "")
            if cur_n == n and (cur_t == t or (is_hybrid_hathway_stb(n) and not t)):
                stats["already_ok"] += 1
                continue
            if t and cur_t and cur_t != t:
                stats["conflicts"] += 1
                continue
            if only_missing and cur_t:
                stats["skipped_only_missing"] += 1
                continue
            new_t = t or cur_t or None
            conn.execute(
                "UPDATE connections SET upstream_id = ?, card_number = ?, updated_at = ? "
                "WHERE id = ?",
                (n, new_t, stamp, int(row["id"])),
            )
            if t and cur_t != t:
                stats["updated_card"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="Hathway CustomerMasterSummaryReport file")
    parser.add_argument("--dry-run", action="store_true", help="Analyse only; do not write")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Fill empty card_number only; do not overwrite existing VC",
    )
    args = parser.parse_args()
    path = args.report.expanduser().resolve()
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    pairs = parse_report(path)
    if args.dry_run:
        stats = analyse(pairs)
        print(f"Dry run — {path.name}")
        print(f"  Report rows:              {stats['report_rows']}")
        print(f"  Matched in platform:      {stats['matched']}")
        print(f"  Already N+T correct:      {stats['already_ok']}")
        print(f"  Would fill/update VC:     {stats['would_update_card']}")
        print(f"  Still missing VC (both):  {stats['missing_vc_on_platform']}")
        print(f"  Report row missing VC:    {stats['missing_vc_in_report']}")
        print(f"  Report T-only STB col:    {stats['t_only_rows']}")
        print(f"  Not in platform:          {stats['not_in_platform']}")
        print(f"  VC conflicts:             {stats['conflicts']}")
        return 0

    stats = apply(pairs, only_missing=args.only_missing)
    print(f"Applied — {path.name}")
    print(f"  Rows parsed:        {stats['report_rows']}")
    print(f"  Matched platform:   {stats['matched']}")
    print(f"  Already correct:    {stats['already_ok']}")
    print(f"  VC filled/updated:  {stats['updated_card']}")
    print(f"  Skipped (only-miss):{stats['skipped_only_missing']}")
    print(f"  Not in platform:    {stats['not_in_platform']}")
    print(f"  Conflicts skipped:  {stats['conflicts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
