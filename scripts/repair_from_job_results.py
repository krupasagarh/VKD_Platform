"""Re-read what the portals already told us, and fix what we stored badly.

Every finished job keeps the provider's raw reply in `upstream_jobs.result_json`. When a
parsing bug means we threw a value away, the value is still there — so the repair is a
local re-read, not another round of logins at 25 seconds each.

Fixes two things:

* expiry dates that failed to parse, e.g. Hathway's "12-OCT-26" and Railtel's
  "23/09/26 11:59:59 PM"
* card numbers taken from the portals' `mac` field, which is a viewing card on Hathway
  but the router's network MAC on Railtel
* Railtel "Active since" / "Down since" session timestamps left unused on earlier checks

Only the newest status result per connection is applied, so an old check can never
overwrite a newer one.

Usage:
    python scripts/repair_from_job_results.py --dry-run
    python scripts/repair_from_job_results.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.db import init_db, log_activity, transaction  # noqa: E402
from app.money import fmt_date, now_iso, parse_date  # noqa: E402
from app.upstream.providers import (  # noqa: E402
    card_number_from,
    link_state_from,
    looks_like_network_mac,
)

EXPIRY_KEYS = ("expiry", "hathway_valid_upto", "valid_upto")


def newest_status_results(conn) -> dict[int, dict]:
    """The most recent successful status reply per connection."""
    out: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT connection_id, provider, result_json FROM upstream_jobs "
        "WHERE action = 'status' AND status = 'done' AND connection_id IS NOT NULL "
        "AND result_json IS NOT NULL ORDER BY id"
    ):
        try:
            result = json.loads(row["result_json"])
        except ValueError:
            continue
        if result.get("simulated"):
            continue
        out[int(row["connection_id"])] = {
            "provider": row["provider"],
            "raw": result.get("raw") or {},
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    expiry_fixed: list[str] = []
    cards_cleared: list[str] = []
    cards_added: list[str] = []
    links_fixed: list[str] = []

    with transaction() as conn:
        results = newest_status_results(conn)

        for connection_id, info in results.items():
            row = conn.execute(
                "SELECT cn.id, cn.provider, cn.upstream_id, cn.expiry_date, cn.card_number, "
                "cn.link_state, cn.link_since, cn.link_days, cu.name "
                "FROM connections cn JOIN customers cu ON cu.id = cn.customer_id "
                "WHERE cn.id = ?", (connection_id,)
            ).fetchone()
            if row is None:
                continue

            raw = info["raw"]
            label = f"{row['name']} · {row['upstream_id']}"

            # Expiry the portal reported, parsed with the current rules.
            reported = ""
            for key in EXPIRY_KEYS:
                if raw.get(key):
                    reported = fmt_date(parse_date(raw[key]))
                    if reported:
                        break

            if reported and reported != (row["expiry_date"] or ""):
                expiry_fixed.append(
                    f"{label}: {row['expiry_date'] or '(none)'} -> {reported}"
                )
                if not args.dry_run:
                    conn.execute(
                        "UPDATE connections SET expiry_date = ?, updated_at = ? WHERE id = ?",
                        (reported, now_iso(), connection_id),
                    )

            # A network MAC is not a viewing card.
            current_card = (row["card_number"] or "").strip()
            if looks_like_network_mac(current_card):
                cards_cleared.append(f"{label}: cleared {current_card}")
                if not args.dry_run:
                    conn.execute(
                        "UPDATE connections SET card_number = '', updated_at = ? WHERE id = ?",
                        (now_iso(), connection_id),
                    )
                current_card = ""

            proper_card = card_number_from(row["provider"], raw)
            if proper_card and not current_card:
                cards_added.append(f"{label}: card {proper_card}")
                if not args.dry_run:
                    conn.execute(
                        "UPDATE connections SET card_number = ?, updated_at = ? WHERE id = ?",
                        (proper_card, now_iso(), connection_id),
                    )

            link = link_state_from(row["provider"], raw)
            if link["state"] and (
                link["state"] != (row["link_state"] or "")
                or link["since"] != (row["link_since"] or "")
                or link["days"] != row["link_days"]
            ):
                since_text = link["since"] or ("now" if link["state"] == "online" else "unknown")
                links_fixed.append(f"{label}: {link['state']} since {since_text}")
                if not args.dry_run:
                    conn.execute(
                        "UPDATE connections SET link_state = ?, link_since = ?, link_days = ?, "
                        "updated_at = ? WHERE id = ?",
                        (link["state"], link["since"], link["days"], now_iso(), connection_id),
                    )

        # Any MAC written to a card field by the same bug, even without a stored result.
        for row in conn.execute(
            "SELECT cn.id, cn.upstream_id, cn.card_number, cu.name FROM connections cn "
            "JOIN customers cu ON cu.id = cn.customer_id "
            "WHERE COALESCE(cn.card_number, '') != ''"
        ):
            if not looks_like_network_mac(row["card_number"]):
                continue
            entry = f"{row['name']} · {row['upstream_id']}: cleared {row['card_number']}"
            if entry in cards_cleared:
                continue
            cards_cleared.append(entry)
            if not args.dry_run:
                conn.execute(
                    "UPDATE connections SET card_number = '', updated_at = ? WHERE id = ?",
                    (now_iso(), row["id"]),
                )

        if not args.dry_run and (expiry_fixed or cards_cleared or cards_added or links_fixed):
            log_activity(
                conn,
                "repaired",
                f"Re-read stored provider replies: {len(expiry_fixed)} expiry date(s) "
                f"corrected, {len(cards_cleared)} network MAC(s) cleared from card fields, "
                f"{len(cards_added)} viewing card(s) filled in, "
                f"{len(links_fixed)} Railtel session(s) filled in",
                meta_json=json.dumps({
                    "expiry_fixed": len(expiry_fixed),
                    "cards_cleared": len(cards_cleared),
                    "cards_added": len(cards_added),
                    "links_fixed": len(links_fixed),
                }),
            )

    print(f"Read {len(results)} stored status repl{'y' if len(results) == 1 else 'ies'}"
          f"{' (dry run — nothing written)' if args.dry_run else ''}\n")

    for title, items in (
        ("Expiry dates corrected", expiry_fixed),
        ("Network MACs cleared from card fields", cards_cleared),
        ("Viewing cards filled in", cards_added),
        ("Railtel sessions filled in", links_fixed),
    ):
        print(f"{title}: {len(items)}")
        for line in items:
            print(f"  {line}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
