"""Bulk status sweep logic, on a throwaway database in simulate mode.

Covers what the sweep must never get wrong: who it picks up, that a renewal at the
counter overtakes hundreds of queued checks, that a CAPTCHA misread gets a second try
while a terminated box does not, and that sweeps cannot stack.

Run: python scripts/test_sweep.py
"""
import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

_tmp = Path(tempfile.mkdtemp(prefix="vkp_sweep_"))
os.environ["VK_PLATFORM_DB"] = str(_tmp / "sweep.db")
os.environ["VK_PLATFORM_UPSTREAM_MODE"] = "simulate"
os.environ["VK_PLATFORM_WORKER_ENABLED"] = "0"

from app.config import settings  # noqa: E402
from app.db import init_db, transaction  # noqa: E402
from app.money import now_iso  # noqa: E402
from app.upstream import jobs as q  # noqa: E402

assert not settings.is_live, "must not run live"

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{': ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(label)


init_db()
stamp = now_iso()

# A spread of connections: fresh Hathway, stale Hathway, stale Railtel, a terminated box,
# a placeholder id, and one already synced today.
FIXTURES = [
    ("Fresh Hathway",  "hathway", "N70100000001", "active",     stamp),
    ("Stale Hathway 1", "hathway", "N70100000002", "active",     "2026-01-01 00:00:00"),
    ("Stale Hathway 2", "hathway", "N70100000003", "suspended",  None),
    ("Stale Railtel 1", "railtel", "ka.stale1",    "active",     None),
    ("Stale Railtel 2", "railtel", "ka.stale2",    "active",     "2026-02-01 00:00:00"),
    ("Terminated box",  "hathway", "N70100000004", "terminated", None),
    ("Placeholder id",  "hathway", "NOBOX9",       "active",     None),
]

with transaction() as conn:
    for name, provider, uid, status, synced in FIXTURES:
        cur = conn.execute(
            "INSERT INTO customers(name, status, created_at, updated_at) VALUES(?, 'active', ?, ?)",
            (name, stamp, stamp))
        cid = cur.lastrowid
        conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, "
            "billing_type, amount_paise, expiry_date, last_synced_at, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 'prepaid', 30000, '', ?, ?, ?)",
            (cid, provider, uid, status, synced, stamp, stamp))

print("Who a sweep would pick up (stale > 7 days)")
with transaction() as conn:
    both = q.sweep_candidates(conn, providers="both", stale_days=7)
    names = [r["upstream_id"] for r in both]
print(f"  candidates: {names}")
check("skips the connection synced today", "N70100000001" not in names)
check("skips the terminated box", "N70100000004" not in names)
check("skips the placeholder id", "NOBOX9" not in names)
check("includes stale Hathway boxes", "N70100000002" in names and "N70100000003" in names)
check("includes stale Railtel logins", "ka.stale1" in names and "ka.stale2" in names)
check("covers both providers", len(both) == 4, len(both))
check("never-synced come before older-synced",
      names.index("N70100000003") < names.index("N70100000002"), names)

with transaction() as conn:
    only_h = q.sweep_candidates(conn, providers="hathway", stale_days=7)
    only_r = q.sweep_candidates(conn, providers="railtel", stale_days=7)
check("hathway-only filter", {r["upstream_id"] for r in only_h} == {"N70100000002", "N70100000003"})
check("railtel-only filter", {r["upstream_id"] for r in only_r} == {"ka.stale1", "ka.stale2"})

with transaction() as conn:
    zero_days = q.sweep_candidates(conn, providers="both", stale_days=0)
check("stale_days=0 widens the net, never narrows it",
      len(zero_days) >= len(both) and {r["upstream_id"] for r in both}
      <= {r["upstream_id"] for r in zero_days}, len(zero_days))
check("stale_days=0 still refuses terminated and placeholder ids",
      "N70100000004" not in {r["upstream_id"] for r in zero_days}
      and "NOBOX9" not in {r["upstream_id"] for r in zero_days})

print("\nStarting a sweep")
with transaction() as conn:
    sweep_id, queued, note = q.start_sweep(conn, providers="both", stale_days=7, limit=100)
print(f"  {note}")
check("sweep created", sweep_id is not None)
check("queued all four", queued == 4, queued)

with transaction() as conn:
    jobs = conn.execute(
        "SELECT id, priority, status, sweep_id, action, max_attempts FROM upstream_jobs "
        "WHERE sweep_id = ? ORDER BY id", (sweep_id,)).fetchall()
check("all jobs are status checks", all(j["action"] == "status" for j in jobs))
check("all queued without needing approval", all(j["status"] == "queued" for j in jobs))
check("all carry sweep priority", all(j["priority"] == q.PRIORITY_SWEEP for j in jobs))
check("one retry allowed for a CAPTCHA misread", all(j["max_attempts"] == 2 for j in jobs))

print("\nThat retry must not be spent on a box that no longer exists")
with transaction() as conn:
    victim = conn.execute(
        "SELECT j.id, j.connection_id, j.customer_id, j.provider, j.action, j.attempts, "
        "j.max_attempts FROM upstream_jobs j WHERE j.sweep_id = ? LIMIT 1", (sweep_id,)).fetchone()
    victim_job = dict(victim)
    victim_conn = victim["connection_id"]
q._finish_failure({**victim_job, "attempts": 1}, "STB is terminated.")
with transaction() as conn:
    after_perm = conn.execute(
        "SELECT status FROM upstream_jobs WHERE id = ?", (victim_job["id"],)).fetchone()
    conn_after = conn.execute(
        "SELECT status FROM connections WHERE id = ?", (victim_conn,)).fetchone()
check("terminated box fails outright despite having a retry left",
      after_perm["status"] == "failed", after_perm["status"])
check("and the connection is marked terminated", conn_after["status"] == "terminated",
      conn_after["status"])

print("\nA CAPTCHA failure, by contrast, keeps its retry")
with transaction() as conn:
    other = conn.execute(
        "SELECT id, connection_id, customer_id, provider, action, attempts, max_attempts "
        "FROM upstream_jobs j WHERE sweep_id = ? AND status = 'queued' LIMIT 1",
        (sweep_id,)).fetchone()
    other_job = dict(other)
q._finish_failure({**other_job, "attempts": 1}, "Hathway login failed — check credentials and CAPTCHA.")
with transaction() as conn:
    after_transient = conn.execute(
        "SELECT status, scheduled_for FROM upstream_jobs WHERE id = ?",
        (other_job["id"],)).fetchone()
check("requeued for another go", after_transient["status"] == "queued",
      after_transient["status"])
check("and scheduled for later", after_transient["scheduled_for"] > now_iso(),
      after_transient["scheduled_for"])
check("a job waiting for its retry is not claimable yet",
      not any(j["id"] == other_job["id"] for j in [q.claim_next_job() or {"id": None}]))

# Bring the retry forward so the rest of this test can drain the queue.
with transaction() as conn:
    conn.execute("UPDATE upstream_jobs SET status='queued', scheduled_for=? WHERE id=?",
                 (now_iso(), other_job["id"]))

print("\nA second sweep must not start while one is running")
with transaction() as conn:
    second_id, second_queued, second_note = q.start_sweep(conn, providers="both", stale_days=7)
check("refused", second_id is None, second_note)

print("\nAn urgent renewal must jump the whole sweep")
with transaction() as conn:
    urgent_conn = conn.execute(
        "SELECT id FROM connections WHERE upstream_id = 'N70100000002'").fetchone()["id"]
    # Free it from the sweep first so we are queueing a genuinely separate job.
    conn.execute("UPDATE upstream_jobs SET status='cancelled' WHERE connection_id = ?",
                 (urgent_conn,))
    urgent_job = q.enqueue_job(conn, connection_id=urgent_conn, action="renew",
                               needs_confirmation=False)
claimed = q.claim_next_job()
check("the renewal ran first", claimed and claimed["id"] == urgent_job,
      f"claimed #{claimed['id'] if claimed else None}, renewal was #{urgent_job}")
check("and it is the renew action", claimed and claimed["action"] == "renew")

print("\nDraining the rest of the sweep")
q.execute_job(claimed)
ran = q.drain_queue(max_jobs=50)
print(f"  ran {ran} more job(s)")
with transaction() as conn:
    progress = q.sweep_progress(conn, sweep_id)
print(f"  progress: {progress['done']} done, {progress['failed']} failed, "
      f"{progress['pending']} pending, {progress['percent']}%")
check("nothing left pending", progress["pending"] == 0, progress)
check("the box we failed as terminated is counted", progress["terminated_found"] == 1,
      progress["terminated_found"])

q.close_finished_sweeps()
with transaction() as conn:
    sweep = conn.execute("SELECT status FROM sync_sweeps WHERE id = ?", (sweep_id,)).fetchone()
check("sweep closed itself", sweep["status"] == "done", sweep["status"])

print("\nWith everything freshly synced there is nothing to do")
with transaction() as conn:
    none_id, none_queued, none_note = q.start_sweep(conn, providers="both", stale_days=7)
check("declines to queue busywork", none_id is None and none_queued == 0)
print(f"  says: {none_note}")

print("\nA sweep can start again once connections go stale, and can be stopped")
with transaction() as conn:
    cur = conn.execute(
        "INSERT INTO customers(name, status, created_at, updated_at) VALUES('Newly stale','active',?,?)",
        (stamp, stamp))
    conn.execute(
        "INSERT INTO connections(customer_id, provider, upstream_id, status, billing_type, "
        "amount_paise, expiry_date, last_synced_at, created_at, updated_at) "
        "VALUES(?, 'hathway', 'N70100000009', 'active', 'prepaid', 30000, '', NULL, ?, ?)",
        (cur.lastrowid, stamp, stamp))
    third_id, third_queued, third_note = q.start_sweep(conn, providers="both", stale_days=7)
check("new sweep allowed once the last finished", third_id is not None, third_note)
check("it picked up only the newly stale one", third_queued == 1, third_queued)
with transaction() as conn:
    dropped = q.cancel_sweep(conn, third_id)
    after = conn.execute("SELECT status FROM sync_sweeps WHERE id = ?", (third_id,)).fetchone()
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? AND status = 'queued'",
        (third_id,)).fetchone()["n"]
check("cancelling drops its queued jobs", dropped == 1, dropped)
check("and marks the sweep cancelled", after["status"] == "cancelled")
check("no queued jobs left behind", orphans == 0, orphans)

print("\nPer-customer check")
with transaction() as conn:
    cust = conn.execute("SELECT id FROM customers WHERE name = 'Placeholder id'").fetchone()["id"]
    ids, skipped = q.enqueue_customer_status(conn, cust)
check("placeholder id is refused, not queued", ids == [] and len(skipped) == 1, skipped)
with transaction() as conn:
    cust = conn.execute("SELECT id FROM customers WHERE name = 'Stale Railtel 1'").fetchone()["id"]
    ids, skipped = q.enqueue_customer_status(conn, cust)
check("a good connection is queued", len(ids) == 1, ids)
with transaction() as conn:
    again, skipped_again = q.enqueue_customer_status(conn, cust)
check("asking twice does not double-queue", again == [] and len(skipped_again) == 1, skipped_again)

print("\nSchedule settings")
with transaction() as conn:
    q.save_sync_schedule(conn, {
        "sync_schedule_enabled": "1", "sync_schedule_hour": "3",
        "sync_schedule_providers": "hathway", "sync_schedule_stale_days": "14",
        "sync_schedule_limit": "200"})
    saved = q.sync_schedule(conn)
check("schedule persists", saved["sync_schedule_hour"] == "3"
      and saved["sync_schedule_providers"] == "hathway"
      and saved["sync_schedule_stale_days"] == "14", saved)

print("\n" + ("All sweep checks passed." if not failures else f"{len(failures)} FAILED: {failures}"))
sys.exit(1 if failures else 0)
