"""Quick counts: Bix master vs platform customers."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from app import bix_sync  # noqa: E402
from app.db import connection  # noqa: E402


def main() -> None:
    items = bix_sync.load_master_items()
    with connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"]
        preview = bix_sync.preview(conn, items)
        create = sum(1 for r in preview if r["action"] == "create")
        matched = sum(1 for r in preview if r.get("customer_id"))
        no_loc = conn.execute(
            "SELECT COUNT(*) AS n FROM customers "
            "WHERE sub_area IS NULL OR trim(sub_area) = ''"
        ).fetchone()["n"]
    print(f"Platform customers: {n}")
    print(f"Bix households in master file: {len(items)}")
    print(f"Bix rows matched to a platform customer: {matched}")
    print(f"In Bix but missing on platform (preview action=create): {create}")
    print(f"Platform customers with empty locality: {no_loc}")


if __name__ == "__main__":
    main()
