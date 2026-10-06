"""Inspect today's scheduled sweep / status sync — read-only."""
from __future__ import annotations

import json
from datetime import datetime

from app.db import connection, get_setting, init_db
from app.upstream.jobs import sync_schedule

init_db()
today = datetime.now().strftime("%Y-%m-%d")
print("Today:", today)

with connection() as conn:
    sched = sync_schedule(conn)
    print("\n=== Sync schedule settings ===")
    for k, v in sched.items():
        print(f"  {k}: {v}")

    print("\n=== Sync sweeps today ===")
    sweeps = conn.execute(
        "SELECT * FROM sync_sweeps WHERE substr(created_at, 1, 10) = ? ORDER BY id",
        (today,),
    ).fetchall()
    if not sweeps:
        print("  (no sweeps created today)")
    for s in sweeps:
        print(dict(s))

    print("\n=== Activity log: sweep / status today ===")
    acts = conn.execute(
        "SELECT at, kind, actor, message, meta_json FROM activity_log "
        "WHERE substr(at, 1, 10) = ? AND (kind LIKE '%sweep%' OR kind LIKE '%status%' "
        "OR message LIKE '%sweep%' OR message LIKE '%status%' OR message LIKE '%batch%') "
        "ORDER BY id DESC LIMIT 40",
        (today,),
    ).fetchall()
    for a in acts:
        print(f"{a['at']} [{a['kind']}] {a['actor']}: {a['message'][:160]}")
        if a["meta_json"]:
            print(f"  meta: {a['meta_json'][:200]}")

    print("\n=== Upstream jobs today (status / status_batch / sweep) ===")
    jobs = conn.execute(
        "SELECT id, provider, action, status, connection_id, sweep_id, requested_by, "
        "       created_at, completed_at, error "
        "FROM upstream_jobs "
        "WHERE substr(created_at, 1, 10) = ? "
        "AND action IN ('status', 'status_batch') "
        "ORDER BY id DESC LIMIT 30",
        (today,),
    ).fetchall()
    print(f"  count (last 30 shown): {len(jobs)}")
    by_status = {}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    print("  status breakdown (sample):", by_status)
    for j in jobs[:15]:
        err = (j["error"] or "")[:80]
        print(
            f"  #{j['id']} {j['provider']} {j['action']} {j['status']} "
            f"conn={j['connection_id']} sweep={j['sweep_id']} by={j['requested_by']} "
            f"created={j['created_at']} err={err!r}"
        )

    print("\n=== Expired-status scheduler runs today ===")
    exp = conn.execute(
        "SELECT at, message, meta_json FROM activity_log "
        "WHERE kind = 'expired_status_refresh' AND substr(at, 1, 10) = ? ORDER BY id",
        (today,),
    ).fetchall()
    if not exp:
        print("  (none logged today)")
    for e in exp:
        print(f"  {e['at']} {e['message']}")

    print("\n=== Last sync settings (expired scheduler) ===")
    for key in (
        "expired_status_refresh_enabled",
        "expired_status_last_run_1d",
        "expired_status_last_run_2d",
        "expired_status_last_run_7d",
    ):
        print(f"  {key}: {get_setting(conn, key, '')}")

    if sweeps:
        sid = int(sweeps[-1]["id"])
        print(f"\n=== Job outcomes for latest sweep #{sid} ===")
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? GROUP BY status",
            (sid,),
        ).fetchall()
        for r in rows:
            print(f"  {r['status']}: {r['n']}")
        fails = conn.execute(
            "SELECT id, connection_id, error FROM upstream_jobs "
            "WHERE sweep_id = ? AND status = 'failed' LIMIT 10",
            (sid,),
        ).fetchall()
        if fails:
            print("  sample failures:")
            for f in fails:
                print(f"    job #{f['id']} conn {f['connection_id']}: {(f['error'] or '')[:120]}")

    print("\n=== Recent sync_sweeps (all time, last 10) ===")
    sweeps_all = conn.execute(
        "SELECT id, trigger, total, status, stale_days, created_at, requested_by "
        "FROM sync_sweeps ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for s in sweeps_all:
        print(dict(s))

    print("\n=== ka.amruthamg (conn 929) after batch job #1089 ===")
    j = conn.execute(
        "SELECT id, status, completed_at, error FROM upstream_jobs WHERE id=1102"
    ).fetchone()
    print("  job 1102:", dict(j) if j else None)
    c = conn.execute(
        "SELECT expiry_date, last_synced_at, status, upstream_plan_name FROM connections WHERE id=929"
    ).fetchone()
    print("  connection:", dict(c) if c else None)

    print("\n=== Sweep activity log (last 10 ever) ===")
    acts = conn.execute(
        "SELECT at, kind, message FROM activity_log "
        "WHERE kind = 'sweep_started' OR message LIKE '%Bulk status check%' "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for a in acts:
        print(f"  {a['at']} [{a['kind']}] {a['message']}")
