"""Fill T (VC) for the 9 active Hathway STBs found in portal status check."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.db import connection, transaction
from app.money import now_iso

PAIRS = {
    "N70150030107": "T403049505445",
    "N70150032020": "T403049507367",
    "N70150083932": "T403049559277",
    "N70152388701": "T403051864045",
    "N70152389022": "T403051864367",
    "N70152400498": "T403051875835",
    "N70174970668": "T403074446002",
    "N70177485516": "T403076960851",
    "N71734720171": "T403236193872",
}


def main() -> int:
    stamp = now_iso()
    updated = 0
    with transaction() as conn:
        for n, t in PAIRS.items():
            row = conn.execute(
                "SELECT id, upstream_id, card_number FROM connections "
                "WHERE provider='hathway' AND upper(upstream_id)=upper(?) LIMIT 1",
                (n,),
            ).fetchone()
            if row is None:
                print(f"SKIP {n}: not in platform")
                continue
            conn.execute(
                "UPDATE connections SET upstream_id = ?, card_number = ?, updated_at = ? WHERE id = ?",
                (n.upper(), t.upper(), stamp, row["id"]),
            )
            print(f"OK {n} + {t} (was T={row['card_number'] or '-'})")
            updated += 1
    print(f"Updated {updated} connection(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
