from app.db import connection, transaction
from app.money import now_iso

with transaction() as conn:
    rows = conn.execute(
        "SELECT id, upstream_id, card_number FROM connections "
        "WHERE provider = 'hathway' AND upper(upstream_id) LIKE 'N722%' "
        "AND card_number IS NOT NULL AND trim(card_number) != ''"
    ).fetchall()
    stamp = now_iso()
    for row in rows:
        conn.execute(
            "UPDATE connections SET card_number = NULL, updated_at = ? WHERE id = ?",
            (stamp, int(row["id"])),
        )
        print(f"cleared {row['upstream_id']} was {row['card_number']}")
print(f"done {len(rows)}")
