from app.db import connection, init_db
from app.money import today
from app.repo import (
    CONNECTION_EFFECTIVE_EXPIRY_SQL,
    expiry_window_connections,
    expiry_window_stats,
    parse_expiry_when,
)

init_db()
needle = "amruthamg"
today_str = today().strftime("%Y-%m-%d")
print("Today:", today_str)

with connection() as conn:
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.code, c.phone, c.custom_plan_validity_days,
               cn.id AS conn_id, cn.provider, cn.upstream_id, cn.status,
               cn.expiry_date, cn.last_synced_at, cn.upstream_plan_name, cn.validity_days
        FROM customers c
        JOIN connections cn ON cn.customer_id = c.id
        WHERE lower(cn.upstream_id) LIKE ? OR lower(c.name) LIKE ?
        ORDER BY cn.provider, cn.id
        """,
        (f"%{needle}%", f"%{needle}%"),
    ).fetchall()

    print("\n=== Platform records ===")
    for r in rows:
        print(dict(r))
        eff = conn.execute(
            f"SELECT ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) AS eff "
            f"FROM connections cn JOIN customers c ON c.id=cn.customer_id "
            f"LEFT JOIN packages p ON p.id=cn.package_id WHERE cn.id=?",
            (int(r["conn_id"]),),
        ).fetchone()
        print(f"  effective_expiry={eff['eff']!r}")
        pays = conn.execute(
            "SELECT paid_at, amount_paise, mode FROM payments WHERE customer_id=? "
            "ORDER BY id DESC LIMIT 5",
            (int(r["id"]),),
        ).fetchall()
        print("  payments:", [dict(p) for p in pays])

    railtel = [r for r in rows if r["provider"] == "railtel"]
    if not railtel:
        print("\nNo railtel connection found for amruthamg")
    else:
        cid = int(railtel[0]["conn_id"])
        print("\n=== Expired / expiring buckets (railtel) ===")
        for when in ("tonight", "1d", "2d", "7d"):
            stats = expiry_window_stats(conn, when=when, provider="railtel")
            bucket = expiry_window_connections(
                conn, when=when, provider="railtel", limit=5000
            )
            ids = {int(x["id"]) for x in bucket}
            key, spec = parse_expiry_when(when)
            print(
                f"  {spec['label']}: total={stats['connections']} "
                f"ka.amruthamg_in_list={cid in ids}"
            )
            if cid in ids:
                row = next(x for x in bucket if int(x["id"]) == cid)
                print(f"    effective_expiry={row['effective_expiry']!r}")

    if railtel:
        cid = int(railtel[0]["conn_id"])
        print("\n=== Recent status jobs ===")
        jobs = conn.execute(
            "SELECT id, action, status, created_at, completed_at, last_error "
            "FROM upstream_jobs WHERE connection_id=? ORDER BY id DESC LIMIT 5",
            (cid,),
        ).fetchall()
        for j in jobs:
            print(dict(j))

        subs = conn.execute(
            "SELECT username, expiry_date, plan_name, fetched_at "
            "FROM railtel_subscriber_rows WHERE lower(username)=lower(?) "
            "ORDER BY id DESC LIMIT 3",
            ("ka.amruthamg",),
        ).fetchall()
        print("\n=== Railtel subscriber export rows ===")
        for s in subs:
            print(dict(s))
        if not subs:
            print("  (none)")
