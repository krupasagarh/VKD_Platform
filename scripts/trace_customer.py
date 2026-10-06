"""Trace one customer across platform DB and Bix master."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from app import bix_sync  # noqa: E402
from app.db import connection  # noqa: E402

PHONE = "9964533743"


def main() -> None:
    with connection() as conn:
        print("=== customers (phone or name like vinay+gold) ===")
        for r in conn.execute(
            "SELECT id, code, name, phone, sub_area FROM customers "
            "WHERE phone = ? OR lower(name) LIKE '%vinay%gold%'",
            (PHONE,),
        ):
            print(dict(r))
        print("=== connections for phone", PHONE, "===")
        for r in conn.execute(
            "SELECT cn.id, cn.customer_id, cn.provider, cn.upstream_id, "
            "c.code, c.name, c.phone FROM connections cn "
            "JOIN customers c ON c.id = cn.customer_id WHERE c.phone = ?",
            (PHONE,),
        ):
            print(dict(r))
        for stb in ("N70150033069", "N70150026881"):
            print(f"=== who owns STB {stb} ===")
            for r in conn.execute(
                "SELECT cn.*, c.code, c.name, c.phone FROM connections cn "
                "JOIN customers c ON c.id = cn.customer_id "
                "WHERE upper(cn.upstream_id) = upper(?)",
                (stb,),
            ):
                print(dict(r))

    items = bix_sync.load_master_items()
    print(f"\nBix master path: {bix_sync.master_accounts_path()}")
    print("=== Bix items for phone/name ===")
    for item in items:
        name = (item.get("name") or "").lower()
        if item.get("phone") == PHONE or ("vinay" in name and "gold" in name):
            print(item)

    with connection() as conn:
        preview = bix_sync.preview(conn, items)
        for row in preview:
            if row.get("phone") == PHONE or (
                "vinay" in (row.get("name") or "").lower()
                and "gold" in (row.get("name") or "").lower()
            ):
                print(
                    "=== preview ===",
                    {
                        "code": row.get("code"),
                        "name": row.get("name"),
                        "phone": row.get("phone"),
                        "stbs": row.get("stbs"),
                        "customer_id": row.get("customer_id"),
                        "platform_name": row.get("platform_name"),
                        "platform_code": row.get("platform_code"),
                        "match": row.get("match"),
                        "action": row.get("action"),
                    },
                )


if __name__ == "__main__":
    main()
