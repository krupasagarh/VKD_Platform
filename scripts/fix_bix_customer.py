"""Apply Bix sync for one phone (create + STB move + due)."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from app import billing, bix_sync  # noqa: E402
from app.db import connection, transaction  # noqa: E402

PHONE = "9964533743"


def main() -> int:
    items = bix_sync.load_master_items()
    with transaction() as conn:
        preview = bix_sync.preview(conn, items)
        row = next((r for r in preview if r.get("phone") == PHONE), None)
        if row is None:
            print("Not found in Bix master file.")
            return 1
        print("Before:", row["action"], "match", row.get("match"), "id", row.get("customer_id"))
        summary = bix_sync.apply_preview(conn, [row], create_missing=True, actor="admin")
        print("Apply summary:", summary)
        cust = conn.execute(
            "SELECT id, code, name, phone, sub_area FROM customers WHERE phone = ?", (PHONE,)
        ).fetchone()
        print("Customer:", dict(cust) if cust else None)
        if cust:
            ledger = billing.customer_ledger(conn, int(cust["id"]))
            print("Net due:", ledger["net_due_paise"])
        for stb in ("N70150033069", "N70150026881"):
            cn = conn.execute(
                "SELECT cn.upstream_id, c.code, c.name FROM connections cn "
                "JOIN customers c ON c.id = cn.customer_id "
                "WHERE upper(cn.upstream_id) = upper(?)",
                (stb,),
            ).fetchone()
            print(f"STB {stb} ->", dict(cn) if cn else "not on platform")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
