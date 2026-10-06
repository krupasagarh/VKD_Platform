"""Audit Hathway STB gaps: platform vs bix_accounts vs hathway_raw."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA = PROJECT_DIR.parent / "cableway_automation" / "data"
sys.path.insert(0, str(PROJECT_DIR))

from app import bix_sync  # noqa: E402
from app.db import connection  # noqa: E402

STB_RE = re.compile(r"N\d{11}", re.I)


def load_hathway_raw_stbs(path: Path) -> dict[str, str]:
    """STB -> card from hathway_raw.xls tab export."""
    cards: dict[str, str] = {}
    if not path.is_file():
        return cards
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and STB_RE.fullmatch(parts[1].strip().upper()):
            stb = parts[1].strip().upper()
            cards[stb] = parts[2].strip()
    return cards


def main() -> int:
    master_items = bix_sync.load_master_items()
    master_by_code = {i["code"]: i for i in master_items}
    master_stbs: dict[str, set[str]] = defaultdict(set)
    for item in master_items:
        for stb in item.get("stbs") or []:
            master_stbs[stb.upper()].add(item["code"])

    hathway_cards = load_hathway_raw_stbs(DATA / "hathway_raw.xls")
    all_hathway_stbs = set(hathway_cards)

    missing_on_platform: list[dict] = []
    extra_on_platform: list[dict] = []
    orphan_hathway: list[str] = []
    shared_stb_codes: list[tuple] = []

    with connection() as conn:
        platform_stb_owner: dict[str, tuple[int, str, str]] = {}
        for row in conn.execute(
            "SELECT cn.upstream_id, cn.customer_id, c.code, c.name "
            "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
            "WHERE cn.provider = 'hathway'"
        ):
            stb = (row["upstream_id"] or "").upper()
            if not STB_RE.fullmatch(stb):
                continue
            platform_stb_owner[stb] = (int(row["customer_id"]), row["code"] or "", row["name"] or "")

        for item in master_items:
            row, how = bix_sync._match_customer(conn, item)
            if not row:
                continue
            cid = int(row["id"])
            want = {s.upper() for s in (item.get("stbs") or [])}
            have = {
                (r["upstream_id"] or "").upper()
                for r in conn.execute(
                    "SELECT upstream_id FROM connections WHERE customer_id = ? AND provider = 'hathway'",
                    (cid,),
                )
            }
            miss = want - have
            extra = have - want
            if miss:
                missing_on_platform.append({
                    "code": item["code"], "name": item["name"], "id": cid,
                    "missing": sorted(miss),
                })
            if extra:
                extra_on_platform.append({
                    "code": item["code"], "name": item["name"], "id": cid,
                    "extra": sorted(extra),
                })

        for stb in sorted(all_hathway_stbs):
            if stb not in platform_stb_owner:
                codes = sorted(master_stbs.get(stb, []))
                orphan_hathway.append(f"{stb}  (bix code hint: {', '.join(codes) or 'none'})")

        for stb, codes in master_stbs.items():
            if len(codes) > 1:
                shared_stb_codes.append((stb, sorted(codes)))

    print("=== 1. STBs in bix_accounts but missing on platform ===")
    print(f"Count: {len(missing_on_platform)}")
    for r in missing_on_platform[:20]:
        print(f"  {r['code']:8} {r['name'][:30]:30} missing {r['missing']}")

    print("\n=== 2. STBs on platform but not in bix_accounts ===")
    print(f"Count: {len(extra_on_platform)}")
    for r in extra_on_platform[:20]:
        print(f"  {r['code']:8} extra {r['extra']}")

    print("\n=== 3. Hathway portal STBs not on any platform customer ===")
    print(f"Count: {len(orphan_hathway)} (from hathway_raw.xls — may include inactive/unassigned)")
    for line in orphan_hathway[:30]:
        print(f"  {line}")
    if len(orphan_hathway) > 30:
        print(f"  ... and {len(orphan_hathway) - 30} more")

    print("\n=== 4. STBs claimed by multiple Bix codes in master file ===")
    print(f"Count: {len(shared_stb_codes)}")
    for stb, codes in shared_stb_codes[:15]:
        print(f"  {stb} -> {codes}")

    print("\n=== Summary ===")
    print(f"Bix households in master: {len(master_items)}")
    print(f"Platform hathway STBs: {len(platform_stb_owner)}")
    print(f"Hathway raw STBs: {len(all_hathway_stbs)}")
    if not missing_on_platform and not extra_on_platform:
        print("Platform matches bix_accounts.csv fully.")
        print("Any gap like Krishnan's 2nd STB means live Bix is ahead of bix_accounts.csv — re-export Customer details from Bix, then Sync all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
