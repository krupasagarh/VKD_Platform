from app.db import connection, init_db
from app.money import today
from app import repo

init_db()
needle = "vishwanath"
with connection() as conn:
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.code, cn.id AS conn_id, cn.provider, cn.upstream_id,
               cn.status, cn.expiry_date, cn.last_synced_at, cn.upstream_plan_name
        FROM customers c
        JOIN connections cn ON cn.customer_id = c.id
        WHERE lower(cn.upstream_id) LIKE ?
           OR lower(c.name) LIKE ?
           OR lower(c.code) LIKE ?
        """,
        (f"%{needle}%", f"%{needle}%", f"%{needle}%"),
    ).fetchall()
    print("=== Matches ===")
    for r in rows:
        print(dict(r))
    print("\n=== In expired list? ===")
    from app.repo import expiry_window_connections, CONNECTION_EFFECTIVE_EXPIRY_SQL

    for when in ("tonight", "1d", "2d", "7d"):
        rows_w = expiry_window_connections(conn, when=when, provider="railtel", limit=5000)
        ids = {int(r["id"]) for r in rows_w}
        for r in rows:
            cid = int(r["conn_id"])
            if r["provider"] == "railtel":
                print(f"when={when} conn #{cid} in_list={cid in ids}")

    print("\n=== Effective expiry ===")
    for r in rows:
        if r["provider"] != "railtel":
            continue
        cid = int(r["conn_id"])
        eff = conn.execute(
            f"SELECT ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) AS eff "
            f"FROM connections cn JOIN customers c ON c.id=cn.customer_id "
            f"LEFT JOIN packages p ON p.id=cn.package_id WHERE cn.id=?",
            (cid,),
        ).fetchone()
        print(f"conn #{cid} expiry_date={r['expiry_date']!r} effective={eff['eff']!r} status={r['status']!r}")

    print("\nToday:", today())
