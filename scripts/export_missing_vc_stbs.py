"""Export Hathway STBs missing T (VC) to a txt file."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.db import connection

N_GLOB = "N[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
T_GLOB = "T[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
OUT = Path(__file__).resolve().parent / "hathway_missing_vc_stbs.txt"

with connection() as conn:
    rows = conn.execute(
        f"SELECT upper(upstream_id) AS stb FROM connections WHERE provider='hathway' "
        f"AND upper(upstream_id) GLOB '{N_GLOB}' AND upper(upstream_id) NOT LIKE 'N722%' "
        f"AND (card_number IS NULL OR trim(card_number)='' "
        f"OR upper(card_number) NOT GLOB '{T_GLOB}') "
        f"ORDER BY stb"
    ).fetchall()

stbs = [r["stb"] for r in rows]
OUT.write_text("\n".join(stbs) + ("\n" if stbs else ""), encoding="utf-8")
print(f"Wrote {len(stbs)} STBs to {OUT}")
