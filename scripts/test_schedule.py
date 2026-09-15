"""The nightly sweep scheduler: fires in its hour, exactly once a day, never stacking.

Runs against a throwaway database in simulate mode.

Run: python scripts/test_schedule.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

_tmp = Path(tempfile.mkdtemp(prefix="vkp_sched_"))
os.environ["VK_PLATFORM_DB"] = str(_tmp / "sched.db")
os.environ["VK_PLATFORM_UPSTREAM_MODE"] = "simulate"
os.environ["VK_PLATFORM_WORKER_ENABLED"] = "0"

from app.config import settings  # noqa: E402
from app.db import get_setting, init_db, set_setting, transaction  # noqa: E402
from app.money import now_iso  # noqa: E402
from app.upstream import jobs as q  # noqa: E402

assert not settings.is_live
failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{': ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(label)


def sweep_count():
    with transaction() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM sync_sweeps").fetchone()["n"]


init_db()
stamp = now_iso()
with transaction() as conn:
    for i in range(3):
        cur = conn.execute(
            "INSERT INTO customers(name, status, created_at, updated_at) VALUES(?,'active',?,?)",
            (f"Cust {i}", stamp, stamp))
        conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, billing_type, "
            "amount_paise, expiry_date, last_synced_at, created_at, updated_at) "
            "VALUES(?, 'hathway', ?, 'active', 'prepaid', 30000, '', NULL, ?, ?)",
            (cur.lastrowid, f"N7010000000{i}", stamp, stamp))

this_hour = datetime.now().hour
other_hour = (this_hour + 5) % 24

print("Disabled schedule does nothing")
with transaction() as conn:
    q.save_sync_schedule(conn, {"sync_schedule_enabled": "0", "sync_schedule_hour": str(this_hour)})
q.run_scheduled_sweep()
check("no sweep started", sweep_count() == 0, sweep_count())

print("\nEnabled but not its hour yet")
with transaction() as conn:
    q.save_sync_schedule(conn, {"sync_schedule_enabled": "1", "sync_schedule_hour": str(other_hour),
                                "sync_schedule_providers": "both", "sync_schedule_stale_days": "7",
                                "sync_schedule_limit": "500"})
q.run_scheduled_sweep()
check("still no sweep", sweep_count() == 0, sweep_count())

print("\nEnabled and its hour has arrived")
with transaction() as conn:
    q.save_sync_schedule(conn, {"sync_schedule_enabled": "1", "sync_schedule_hour": str(this_hour)})
q.run_scheduled_sweep()
check("sweep started", sweep_count() == 1, sweep_count())
with transaction() as conn:
    sweep = conn.execute("SELECT * FROM sync_sweeps ORDER BY id DESC LIMIT 1").fetchone()
    jobs = conn.execute(
        "SELECT COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ?", (sweep["id"],)
    ).fetchone()["n"]
check("marked as scheduled, not manual", sweep["trigger"] == "scheduled", sweep["trigger"])
check("requested by the scheduler", sweep["requested_by"] == "scheduler", sweep["requested_by"])
check("queued all three connections", jobs == 3, jobs)

print("\nThe same hour must not start a second sweep")
for _ in range(4):
    q.run_scheduled_sweep()
check("still exactly one sweep", sweep_count() == 1, sweep_count())
with transaction() as conn:
    check("today recorded as the last run",
          get_setting(conn, "sync_last_run_date") == datetime.now().strftime("%Y-%m-%d"))

print("\nA restart inside the same hour must not start another")
q.run_scheduled_sweep()
check("still one", sweep_count() == 1, sweep_count())

print("\nNext day, it runs again")
q.drain_queue(max_jobs=50)
q.close_finished_sweeps()
with transaction() as conn:
    set_setting(conn, "sync_last_run_date", "2026-01-01")
    # Age the connections so there is something to pick up.
    conn.execute("UPDATE connections SET last_synced_at = '2026-01-01 00:00:00'")
q.run_scheduled_sweep()
check("a second sweep started", sweep_count() == 2, sweep_count())

print("\nAn already-running sweep blocks the schedule rather than stacking")
with transaction() as conn:
    set_setting(conn, "sync_last_run_date", "2026-01-01")
q.run_scheduled_sweep()
check("no third sweep while one runs", sweep_count() == 2, sweep_count())

print("\n" + ("Scheduler checks passed." if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
