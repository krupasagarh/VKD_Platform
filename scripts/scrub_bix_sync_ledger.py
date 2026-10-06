"""Remove cancelled bix_sync bills and orphan sync adjustments."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from app import billing, bix_sync  # noqa: E402
from app.db import connection, transaction  # noqa: E402


def main() -> int:
    customer_id = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    with transaction() as conn:
        if customer_id:
            before = billing.customer_ledger(conn, customer_id)
            ghosts = bix_sync.purge_bix_sync_ghosts(conn, customer_id)
            bix_sync.scrub_bix_sync_ledger(conn, customer_id)
            after = billing.customer_ledger(conn, customer_id)
            print(f"customer {customer_id}: {ghosts}")
            print("before", before)
            print("after ", after)
        else:
            totals = bix_sync.purge_all_bix_sync_ghosts(conn)
            print("purge_all", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
