"""Provider job queue.

Every action that touches a provider portal goes through this queue instead of
running inside a web request, because a single renew can take minutes (login,
captcha OCR, page waits) and must not run twice concurrently on the same dealer
login.

Status flow:

    awaiting_confirm --confirm--> queued --worker--> running --> done
                                     ^                    |
                                     +----- retry ---------+--> failed
    running --ANT OTP--> awaiting_otp --submit OTP--> running
    awaiting_confirm/queued/awaiting_otp --cancel--> cancelled

A single background thread drains the queue, so provider logins are serialised.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .. import billing
from ..config import settings
from ..db import connection, get_setting, log_activity, set_setting, transaction
from ..money import fmt_datetime, now_iso, parse_date
from .providers import (
    ACCOUNT_ACTIONS,
    ACTION_LABELS,
    PROVIDERS,
    UpstreamUnsupported,
    card_number_from,
    id_problem,
    link_state_from,
    is_permanent_error,
    reports_terminated,
    run_action,
    wallet_summary,
)

log = logging.getLogger("vk_platform.jobs")

JOB_ACTIVE_STATUSES = ("awaiting_confirm", "awaiting_otp", "queued", "running")
JOB_OPEN_STATUSES = ("awaiting_confirm", "awaiting_otp", "queued", "running")
JOB_CANCELABLE_STATUSES = ("awaiting_confirm", "awaiting_otp", "queued")
_OPEN_SQL = ", ".join(f"'{s}'" for s in JOB_OPEN_STATUSES)

OTP_WAIT_SECONDS = 600

_otp_lock = threading.Lock()
_otp_waiters: dict[int, dict] = {}

# Lower number runs first. Anything an operator is waiting on beats a bulk sweep.
PRIORITY_INTERACTIVE = 100
PRIORITY_SWEEP = 900

# Connection states worth asking the provider about. A terminated box has nothing left
# to report, and re-checking it every night would waste a login each time.
SWEEPABLE_STATUSES = ("active", "suspended")

SYNC_DEFAULTS = {
    "sync_schedule_enabled": "0",
    "sync_schedule_hour": "2",
    "sync_schedule_providers": "both",
    "sync_schedule_stale_days": "7",
    "sync_schedule_limit": "700",
}

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


class RenewNotAllowed(Exception):
    """Railtel renew rejected — account is still valid on or after today."""


# --------------------------------------------------------------------------- #
# Queue management
# --------------------------------------------------------------------------- #

def enqueue_job(
    conn: sqlite3.Connection,
    *,
    connection_id: int,
    action: str,
    requested_by: str | None = None,
    payment_id: int | None = None,
    needs_confirmation: bool = True,
    max_attempts: int = 2,
    priority: int = PRIORITY_INTERACTIVE,
    sweep_id: int | None = None,
    quiet: bool = False,
    collect_later: bool = False,
) -> int:
    row = conn.execute(
        "SELECT id, customer_id, provider, expiry_date FROM connections WHERE id = ?",
        (connection_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Connection {connection_id} not found")

    if (row["provider"] or "").lower() == "railtel" and action == "renew":
        from ..railtel_sync import railtel_renew_block_reason

        reason = railtel_renew_block_reason(row["expiry_date"])
        if reason:
            raise RenewNotAllowed(reason)

    # ANT WhatsApp OTP and SmartPlay password login skip the extra confirm click.
    if (row["provider"] or "").lower() in ("iptv", "ott"):
        needs_confirmation = False

    status = "awaiting_confirm" if needs_confirmation else "queued"
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO upstream_jobs(connection_id, customer_id, provider, action, status, "
        "priority, sweep_id, payment_id, attempts, max_attempts, requested_by, "
        "scheduled_for, collect_later, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
        (
            connection_id,
            int(row["customer_id"]),
            row["provider"],
            action,
            status,
            priority,
            sweep_id,
            payment_id,
            max_attempts,
            requested_by or settings.operator,
            stamp,
            1 if collect_later else 0,
            stamp,
            stamp,
        ),
    )
    job_id = int(cursor.lastrowid)
    # A sweep queues hundreds of jobs at once; logging each one would bury everything else.
    if not quiet:
        log_activity(
            conn,
            "job_created",
            f"{ACTION_LABELS.get(action, action)} queued for {row['provider']} connection",
            actor=requested_by,
            customer_id=int(row["customer_id"]),
            connection_id=connection_id,
            meta_json=json.dumps({"job_id": job_id, "action": action, "status": status}),
        )
    return job_id


def enqueue_customer_status(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    requested_by: str | None = None,
) -> tuple[list[int], list[str]]:
    """Queue a status check on every connection this customer has, across providers.

    Returns the job ids created and a note for each connection that was skipped.
    """
    rows = conn.execute(
        "SELECT id, provider, upstream_id, status FROM connections WHERE customer_id = ? "
        "ORDER BY provider, id",
        (customer_id,),
    ).fetchall()

    job_ids: list[int] = []
    skipped: list[str] = []
    for row in rows:
        problem = id_problem(row["provider"], row["upstream_id"] or "")
        if problem:
            skipped.append(f"{row['upstream_id'] or 'no id'}: {problem}")
            continue
        open_job = conn.execute(
            "SELECT id FROM upstream_jobs WHERE connection_id = ? AND action = 'status' "
            f"AND status IN ({_OPEN_SQL}) LIMIT 1",
            (row["id"],),
        ).fetchone()
        if open_job:
            skipped.append(f"{row['upstream_id']}: already queued as job #{open_job['id']}")
            continue
        job_ids.append(
            enqueue_job(
                conn,
                connection_id=int(row["id"]),
                action="status",
                requested_by=requested_by,
                needs_confirmation=False,
                # CAPTCHA OCR misreads a login now and then. One retry covers that;
                # a genuinely dead box is refused by the permanent-error check instead.
                max_attempts=2,
                quiet=True,
            )
        )

    if job_ids:
        log_activity(
            conn,
            "job_created",
            f"Status check queued for {len(job_ids)} connection(s) of this customer",
            actor=requested_by,
            customer_id=customer_id,
            meta_json=json.dumps({"job_ids": job_ids}),
        )
    return job_ids, skipped


def enqueue_provider_job(
    conn: sqlite3.Connection,
    *,
    provider: str,
    action: str,
    requested_by: str | None = None,
    needs_confirmation: bool = False,
) -> int:
    """Queue a dealer-account job (wallet balance, STB counts) with no customer attached."""
    if action not in ACCOUNT_ACTIONS.get(provider, ()):
        raise ValueError(f"{provider} has no account action '{action}'")

    if (provider or "").lower() in ("iptv", "ott") or action == "sync":
        needs_confirmation = False

    existing = conn.execute(
        "SELECT id FROM upstream_jobs WHERE provider = ? AND action = ? "
        f"AND status IN ({_OPEN_SQL}) LIMIT 1",
        (provider, action),
    ).fetchone()
    if existing:
        return int(existing["id"])

    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO upstream_jobs(connection_id, customer_id, provider, action, status, "
        "attempts, max_attempts, requested_by, scheduled_for, created_at, updated_at) "
        "VALUES(NULL, NULL, ?, ?, ?, 0, 1, ?, ?, ?, ?)",
        (
            provider,
            action,
            "awaiting_confirm" if needs_confirmation else "queued",
            requested_by or settings.operator,
            stamp,
            stamp,
            stamp,
        ),
    )
    job_id = int(cursor.lastrowid)
    log_activity(
        conn,
        "job_created",
        f"{ACTION_LABELS.get(action, action)} queued for the {provider} dealer account",
        actor=requested_by,
        meta_json=json.dumps({"job_id": job_id, "action": action, "provider": provider}),
    )
    return job_id


def confirm_job(conn: sqlite3.Connection, job_id: int, *, actor: str | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE upstream_jobs SET status = 'queued', scheduled_for = ?, error = NULL, updated_at = ? "
        "WHERE id = ? AND status = 'awaiting_confirm'",
        (now_iso(), now_iso(), job_id),
    )
    if cursor.rowcount:
        log_activity(conn, "job_confirmed", f"Job #{job_id} approved to run", actor=actor)
        return True
    return False


def abort_otp_wait(job_id: int) -> None:
    with _otp_lock:
        waiter = _otp_waiters.get(int(job_id))
        if waiter is None:
            return
        waiter["cancelled"] = True
        waiter["event"].set()


def request_job_otp(job_id: int, prompt: str, timeout: float = OTP_WAIT_SECONDS) -> str:
    """Block the worker until the operator submits a 6-digit OTP for this job."""
    job_id = int(job_id)
    event = threading.Event()
    with _otp_lock:
        previous = _otp_waiters.get(job_id)
        if previous:
            previous["cancelled"] = True
            previous["event"].set()
        _otp_waiters[job_id] = {
            "event": event,
            "code": None,
            "cancelled": False,
            "prompt": prompt,
        }
    with transaction() as conn:
        conn.execute(
            "UPDATE upstream_jobs SET status = 'awaiting_otp', error = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('running', 'awaiting_otp')",
            (prompt, now_iso(), job_id),
        )
        log_activity(conn, "job_otp", f"Job #{job_id} waiting for WhatsApp OTP", meta_json=json.dumps({"job_id": job_id}))

    try:
        if not event.wait(timeout):
            raise TimeoutError("WhatsApp OTP was not entered in time. Retry the job.")
        with _otp_lock:
            waiter = _otp_waiters.get(job_id) or {}
            if waiter.get("cancelled"):
                raise RuntimeError("Job cancelled while waiting for OTP.")
            code = _digits_otp(waiter.get("code"))
        if len(code) not in (6, 10):
            raise RuntimeError("Enter the 6-digit OTP or the 10-digit WhatsApp number.")
        return code
    finally:
        with transaction() as conn:
            conn.execute(
                "UPDATE upstream_jobs SET status = 'running', error = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'awaiting_otp'",
                (now_iso(), job_id),
            )
        with _otp_lock:
            _otp_waiters.pop(job_id, None)


def _digits_otp(raw) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def submit_otp(conn: sqlite3.Connection, job_id: int, code: str, *, actor: str | None = None) -> bool:
    job_id = int(job_id)
    digits = _digits_otp(code)
    if len(digits) not in (6, 10):
        return False
    with _otp_lock:
        waiter = _otp_waiters.get(job_id)
        if waiter is None:
            return False
        waiter["code"] = digits
        waiter["event"].set()
    log_activity(conn, "job_otp_submitted", f"OTP entered for job #{job_id}", actor=actor)
    return True


def cancel_job(conn: sqlite3.Connection, job_id: int, *, actor: str | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE upstream_jobs SET status = 'cancelled', completed_at = ?, updated_at = ? "
        f"WHERE id = ? AND status IN ({', '.join(repr(s) for s in JOB_CANCELABLE_STATUSES)})",
        (now_iso(), now_iso(), job_id),
    )
    if cursor.rowcount:
        abort_otp_wait(job_id)
        log_activity(conn, "job_cancelled", f"Job #{job_id} cancelled", actor=actor)
        return True
    return False


def retry_job(conn: sqlite3.Connection, job_id: int, *, actor: str | None = None) -> bool:
    cursor = conn.execute(
        "UPDATE upstream_jobs SET status = 'queued', attempts = 0, error = NULL, "
        "completed_at = NULL, scheduled_for = ?, updated_at = ? "
        "WHERE id = ? AND status IN ('failed', 'cancelled')",
        (now_iso(), now_iso(), job_id),
    )
    if cursor.rowcount:
        log_activity(conn, "job_retried", f"Job #{job_id} queued again", actor=actor)
        return True
    return False


# --------------------------------------------------------------------------- #
# Bulk status sweeps
# --------------------------------------------------------------------------- #

def _wanted_providers(providers: str | None) -> list[str]:
    """'both' stays Railtel + Hathway so a daily sweep never hits ANT or SmartPlay."""
    raw = (providers or "both").strip().lower()
    if raw in ("all", "all_three"):
        return [p for p in PROVIDERS if p != "ott"]
    if raw in ("both", ""):
        return [p for p in PROVIDERS if p not in ("iptv", "ott")]
    return [raw]


def sweep_candidates(
    conn: sqlite3.Connection,
    *,
    providers: str = "both",
    stale_days: int = 7,
) -> list[sqlite3.Row]:
    """Connections a sweep would ask about, stalest first.

    Skips anything already queued, anything terminated or inactive, and any id the
    provider could not look up anyway.
    """
    wanted = _wanted_providers(providers)
    cutoff = (datetime.now() - timedelta(days=max(0, int(stale_days)))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    placeholders = ",".join("?" * len(wanted))
    statuses = ",".join("?" * len(SWEEPABLE_STATUSES))
    rows = conn.execute(
        f"SELECT cn.id, cn.provider, cn.upstream_id, cn.last_synced_at, cn.customer_id "
        f"FROM connections cn "
        f"WHERE cn.provider IN ({placeholders}) "
        f"  AND cn.status IN ({statuses}) "
        f"  AND (cn.last_synced_at IS NULL OR cn.last_synced_at < ?) "
        f"  AND NOT EXISTS (SELECT 1 FROM upstream_jobs j WHERE j.connection_id = cn.id "
        f"                  AND j.status IN ({_OPEN_SQL})) "
        f"ORDER BY cn.last_synced_at IS NOT NULL, cn.last_synced_at, cn.id",
        [*wanted, *SWEEPABLE_STATUSES, cutoff],
    ).fetchall()

    return [r for r in rows if not id_problem(r["provider"], r["upstream_id"] or "")]


def active_sweep(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sync_sweeps WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def start_sweep(
    conn: sqlite3.Connection,
    *,
    providers: str = "both",
    stale_days: int = 7,
    limit: int = 700,
    trigger: str = "manual",
    requested_by: str | None = None,
) -> tuple[int | None, int, str]:
    """Queue a status check for every stale connection. Returns (sweep_id, queued, note)."""
    running = active_sweep(conn)
    if running:
        return None, 0, f"Sweep #{running['id']} is still running — cancel it first."

    candidates = sweep_candidates(conn, providers=providers, stale_days=stale_days)[
        : max(1, int(limit))
    ]
    if not candidates:
        return None, 0, (
            f"Nothing to check: every {providers} connection has been synced within "
            f"{stale_days} day(s), or is terminated, or has an id no portal can look up."
        )

    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO sync_sweeps(providers, status, stale_days, total, trigger, "
        "requested_by, created_at, updated_at) VALUES(?, 'running', ?, ?, ?, ?, ?, ?)",
        (providers, int(stale_days), len(candidates), trigger,
         requested_by or settings.operator, stamp, stamp),
    )
    sweep_id = int(cursor.lastrowid)

    for row in candidates:
        enqueue_job(
            conn,
            connection_id=int(row["id"]),
            action="status",
            requested_by=requested_by or f"sweep #{sweep_id}",
            needs_confirmation=False,
            # A CAPTCHA misread should not cost this connection the whole sweep. The
            # permanent-error check stops terminated boxes from ever using the retry.
            max_attempts=2,
            priority=PRIORITY_SWEEP,
            sweep_id=sweep_id,
            quiet=True,
        )

    log_activity(
        conn,
        "sweep_started",
        f"Bulk status check #{sweep_id} queued {len(candidates)} connection(s) "
        f"({providers}, not synced in {stale_days} day(s), {trigger})",
        actor=requested_by,
        meta_json=json.dumps({"sweep_id": sweep_id, "total": len(candidates)}),
    )
    return sweep_id, len(candidates), (
        f"Sweep #{sweep_id} queued {len(candidates)} status check(s). "
        f"Renewals and anything you click still jump the queue."
    )


def sweep_progress(conn: sqlite3.Connection, sweep_id: int) -> dict:
    counts = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? "
            "GROUP BY status", (sweep_id,)
        )
    }
    sweep = conn.execute("SELECT * FROM sync_sweeps WHERE id = ?", (sweep_id,)).fetchone()
    total = int(sweep["total"]) if sweep else sum(counts.values())
    finished = counts.get("done", 0) + counts.get("failed", 0) + counts.get("cancelled", 0)
    pending = counts.get("queued", 0) + counts.get("running", 0)

    terminated = int(conn.execute(
        "SELECT COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? AND status = 'failed' "
        "AND LOWER(COALESCE(error, '')) LIKE '%terminated%'", (sweep_id,)
    ).fetchone()["n"])

    return {
        "sweep": sweep,
        "total": total,
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "pending": pending,
        "finished": finished,
        "terminated_found": terminated,
        "percent": int(round(100 * finished / total)) if total else 0,
        # Each job is a fresh login: roughly 25 seconds of real portal time.
        "eta_minutes": int(round(pending * 25 / 60)),
    }


def cancel_sweep(conn: sqlite3.Connection, sweep_id: int, *, reason: str = "cancelled") -> int:
    """Drop a sweep's queued jobs. A job already running is left to finish."""
    stamp = now_iso()
    cursor = conn.execute(
        "UPDATE upstream_jobs SET status = 'cancelled', updated_at = ? "
        "WHERE sweep_id = ? AND status IN ('awaiting_confirm', 'queued')",
        (stamp, sweep_id),
    )
    dropped = cursor.rowcount or 0
    conn.execute(
        "UPDATE sync_sweeps SET status = ?, finished_at = ?, updated_at = ? WHERE id = ?",
        (reason, stamp, stamp, sweep_id),
    )
    log_activity(
        conn,
        "sweep_cancelled",
        f"Bulk status check #{sweep_id} {reason} — {dropped} queued check(s) dropped",
        meta_json=json.dumps({"sweep_id": sweep_id, "dropped": dropped}),
    )
    return dropped


def close_finished_sweeps() -> None:
    """Mark a sweep done once none of its jobs are left waiting."""
    with transaction() as conn:
        for sweep in conn.execute("SELECT id FROM sync_sweeps WHERE status = 'running'"):
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? "
                f"AND status IN ({_OPEN_SQL})",
                (sweep["id"],),
            ).fetchone()["n"]
            if pending:
                continue
            progress = sweep_progress(conn, int(sweep["id"]))
            stamp = now_iso()
            conn.execute(
                "UPDATE sync_sweeps SET status = 'done', finished_at = ?, updated_at = ? "
                "WHERE id = ?", (stamp, stamp, sweep["id"]),
            )
            log_activity(
                conn,
                "sweep_finished",
                f"Bulk status check #{sweep['id']} finished — {progress['done']} synced, "
                f"{progress['failed']} failed"
                + (f", {progress['terminated_found']} found terminated"
                   if progress["terminated_found"] else ""),
                meta_json=json.dumps({"sweep_id": int(sweep["id"]), **{
                    k: v for k, v in progress.items() if k != "sweep"}}),
            )


def claim_next_job() -> dict | None:
    """Atomically move the next due job to `running` and return a snapshot of it."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM upstream_jobs WHERE status = 'queued' "
            "AND (scheduled_for IS NULL OR scheduled_for <= ?) "
            "ORDER BY priority, id LIMIT 1",
            (now_iso(),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE upstream_jobs SET status = 'running', attempts = attempts + 1, "
            "started_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), row["id"]),
        )
        conn_row = None
        if row["connection_id"] is not None:
            conn_row = conn.execute(
                "SELECT * FROM connections WHERE id = ?", (row["connection_id"],)
            ).fetchone()
        return {
            "id": int(row["id"]),
            "connection_id": int(row["connection_id"]) if row["connection_id"] is not None else None,
            "customer_id": int(row["customer_id"]) if row["customer_id"] is not None else None,
            "provider": row["provider"],
            "action": row["action"],
            "payment_id": row["payment_id"],
            "attempts": int(row["attempts"]) + 1,
            "max_attempts": int(row["max_attempts"]),
            "upstream_id": conn_row["upstream_id"] if conn_row else "",
            "collect_later": bool(row["collect_later"]) if "collect_later" in row.keys() else False,
        }


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def _finish_failure(job: dict, error: str) -> None:
    permanent = is_permanent_error(error)
    retryable = job["attempts"] < job["max_attempts"] and not permanent
    with transaction() as conn:
        if retryable:
            retry_at = (datetime.now() + timedelta(minutes=settings.job_retry_minutes)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                "UPDATE upstream_jobs SET status = 'queued', error = ?, scheduled_for = ?, "
                "updated_at = ? WHERE id = ?",
                (error, retry_at, now_iso(), job["id"]),
            )
            message = (
                f"{ACTION_LABELS.get(job['action'], job['action'])} failed "
                f"(attempt {job['attempts']}/{job['max_attempts']}), retrying at {retry_at}"
            )
        else:
            conn.execute(
                "UPDATE upstream_jobs SET status = 'failed', error = ?, completed_at = ?, "
                "updated_at = ? WHERE id = ?",
                (error, now_iso(), now_iso(), job["id"]),
            )
            message = f"{ACTION_LABELS.get(job['action'], job['action'])} failed: {error}"
            if permanent:
                message += " — not retried, the answer would not change"

            # The portal says the box no longer exists. Stop treating it as active so the
            # bill checker does not keep charging for a service the provider has removed.
            if job["connection_id"] is not None and reports_terminated(error):
                conn.execute(
                    "UPDATE connections SET status = 'terminated', expiry_date = '', "
                    "last_synced_at = ?, updated_at = ? WHERE id = ?",
                    (now_iso(), now_iso(), job["connection_id"]),
                )
                log_activity(
                    conn,
                    "connection_updated",
                    f"{job['provider']} reports this box is terminated — marked terminated "
                    f"here so it stops being billed",
                    customer_id=job["customer_id"],
                    connection_id=job["connection_id"],
                )

            if job["connection_id"] is None:
                conn.execute(
                    "INSERT INTO provider_status(provider, checked_at, error) VALUES(?, ?, ?) "
                    "ON CONFLICT(provider) DO UPDATE SET "
                    "checked_at = excluded.checked_at, error = excluded.error",
                    (job["provider"], now_iso(), error),
                )
        log_activity(
            conn,
            "job_failed",
            message,
            customer_id=job["customer_id"],
            connection_id=job["connection_id"],
            meta_json=json.dumps({"job_id": job["id"], "error": error}),
        )


def _slim_job_result(result) -> dict:
    """Drop the bulky subscriber list before writing result_json.

    A live online list is hundreds of rows; the snapshot tables already hold them,
    and the jobs table would otherwise truncate the JSON at 20 KB mid-object.
    """
    payload = result.as_dict()
    raw = dict(payload.get("raw") or {})
    subscribers = raw.pop("subscribers", None)
    if subscribers is not None:
        raw["subscriber_count"] = len(subscribers)
        raw["subscriber_sample"] = subscribers[:3]
    payload["raw"] = raw
    return payload


def _apply_online_success(job: dict, result) -> None:
    """Store the Railtel dash.php online-subscriber list as a snapshot."""
    stamp = now_iso()
    raw = result.raw or {}
    subscribers = raw.get("subscribers") or []
    try:
        reported = int(str(raw.get("online_count") or len(subscribers)).replace(",", ""))
    except ValueError:
        reported = len(subscribers)
    nas_json = json.dumps(raw.get("nas") or [])
    partial = 1 if raw.get("partial") or (reported and len(subscribers) < reported) else 0
    note = (raw.get("message") or raw.get("note") or "").strip()

    with transaction() as conn:
        snap_id = int(conn.execute(
            "INSERT INTO railtel_online_snapshots("
            "fetched_at, job_id, online_count, row_count, upload_gb, download_gb, "
            "total_gb, nas_json, partial, note) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stamp,
                job["id"],
                reported,
                len(subscribers),
                raw.get("upload_gb") or "",
                raw.get("download_gb") or "",
                raw.get("total_gb") or "",
                nas_json,
                partial,
                note,
            ),
        ).lastrowid)

        for row in subscribers:
            username = str(row.get("username") or "").strip()
            if not username:
                continue
            start_at = fmt_datetime(row.get("start_time") or "")
            conn.execute(
                "INSERT INTO railtel_online_rows("
                "snapshot_id, session_id, username, mac, framed_ip, start_time, "
                "start_at, total_time, upload_mb, download_mb, total_mb) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snap_id,
                    str(row.get("session_id") or "").strip(),
                    username,
                    str(row.get("mac") or "").strip(),
                    str(row.get("framed_ip") or "").strip(),
                    str(row.get("start_time") or "").strip(),
                    start_at,
                    str(row.get("total_time") or "").strip(),
                    str(row.get("upload_mb") or "").strip(),
                    str(row.get("download_mb") or "").strip(),
                    str(row.get("total_mb") or "").strip(),
                ),
            )
            # Same session start the customer page shows as "Active since". Safe to
            # write: the portal just told us this login is up right now.
            conn.execute(
                "UPDATE connections SET link_state = 'online', "
                "link_since = CASE WHEN ? != '' THEN ? ELSE link_since END, "
                "last_synced_at = ?, updated_at = ? "
                "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
                (start_at, start_at, stamp, stamp, username),
            )

        slim = _slim_job_result(result)
        conn.execute(
            "UPDATE upstream_jobs SET status = 'done', result_json = ?, error = NULL, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(slim)[:20000], stamp, stamp, job["id"]),
        )
        log_activity(
            conn,
            "railtel_online",
            f"Railtel online list refreshed — {len(subscribers)} session(s)"
            + (" (partial)" if partial else "")
            + (" (simulated)" if result.simulated else ""),
            meta_json=json.dumps({
                "job_id": job["id"],
                "snapshot_id": snap_id,
                "online_count": reported,
                "row_count": len(subscribers),
            }),
        )


def _apply_railtel_subscribers_sync(job: dict, result) -> None:
    """Store My Subscribers snapshot and refresh local Railtel expiry dates."""
    from .. import railtel_sync
    from ..money import fmt_date, parse_date

    stamp = now_iso()
    rows = list(result.raw.get("subscribers") or [])
    summary = railtel_sync.summarize_subscriber_rows(rows)
    mapped = {"created": 0, "linked": 0, "updated": 0, "skipped": 0, "total": len(rows)}
    with transaction() as conn:
        if not result.simulated:
            mapped = railtel_sync.sync_railtel_subscribers(conn, rows, stamp)
            snap_id = int(conn.execute(
                "INSERT INTO railtel_subscriber_snapshots("
                "fetched_at, job_id, total_count, row_count, active_count, expired_count, "
                "late_1d, late_2d, expiring_7d, note) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stamp,
                    job["id"],
                    int(result.raw.get("total_count") or summary["total"]),
                    summary["total"],
                    summary["active"],
                    summary["expired"],
                    summary["late_1d"],
                    summary["late_2d"],
                    summary["expiring_7d"],
                    (result.raw.get("note") or result.message or "").strip(),
                ),
            ).lastrowid)

            for row in rows:
                username = str(row.get("username") or "").strip()
                if not username:
                    continue
                renewal_at = fmt_date(parse_date(row.get("renewal_date") or ""))
                conn.execute(
                    "INSERT INTO railtel_subscriber_rows("
                    "snapshot_id, subscriber_id, username, package_name, renewal_date, "
                    "renewal_at, mobile, name, status, is_red, row_class) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snap_id,
                        str(row.get("subscriber_id") or "").strip(),
                        username,
                        str(row.get("package") or "").strip(),
                        str(row.get("renewal_date") or "").strip(),
                        renewal_at,
                        str(row.get("mobile") or "").strip(),
                        str(row.get("name") or "").strip(),
                        str(row.get("status") or "").strip(),
                        1 if row.get("is_red") else 0,
                        str(row.get("row_class") or "").strip(),
                    ),
                )

            ps = conn.execute(
                "SELECT wallet_balance, operator_name FROM provider_status WHERE provider = 'railtel'"
            ).fetchone()
            conn.execute(
                "INSERT INTO provider_status(provider, wallet_balance, active_count, "
                "inactive_count, total_count, operator_name, checked_at, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(provider) DO UPDATE SET "
                "active_count = excluded.active_count, "
                "inactive_count = excluded.inactive_count, "
                "total_count = excluded.total_count, "
                "checked_at = excluded.checked_at",
                (
                    "railtel",
                    (ps["wallet_balance"] if ps else "") or "",
                    summary["active"],
                    summary["expiring_7d"],
                    summary["total"],
                    (ps["operator_name"] if ps else "") or "",
                    stamp,
                ),
            )

        conn.execute(
            "UPDATE upstream_jobs SET status = 'done', result_json = ?, error = NULL, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps({**result.as_dict(), "mapped": mapped, "summary": summary})[:20000], stamp, stamp, job["id"]),
        )
        log_activity(
            conn,
            "railtel_synced",
            (
                f"Railtel My Subscribers synced — {summary['total']} on portal, "
                f"{summary['expired']} expired, {mapped.get('updated', 0)} expiry dates updated"
                + (" (simulated)" if result.simulated else "")
            ),
            meta_json=json.dumps({"job_id": job["id"], **mapped, **summary}),
        )


def _apply_ott_sync(job: dict, result) -> None:
    """Map SmartPlay subscribers onto customers and store the dealer snapshot."""
    from .. import ott_plans

    stamp = now_iso()
    summary = wallet_summary(job["provider"], result.raw)
    mapped = {"created": 0, "linked": 0, "updated": 0, "skipped": 0, "total": 0}
    with transaction() as conn:
        if not result.simulated:
            mapped = ott_plans.sync_smartplay_subscribers(
                conn, list(result.raw.get("subscribers") or [])
            )
        conn.execute(
            "INSERT INTO provider_status(provider, wallet_balance, active_count, "
            "inactive_count, total_count, operator_name, checked_at, error) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "wallet_balance = excluded.wallet_balance, active_count = excluded.active_count, "
            "inactive_count = excluded.inactive_count, total_count = excluded.total_count, "
            "operator_name = excluded.operator_name, checked_at = excluded.checked_at, error = NULL",
            (
                job["provider"],
                summary["wallet_balance"],
                summary["active"],
                summary["inactive"],
                summary["total"],
                summary["operator"],
                stamp,
            ),
        )
        conn.execute(
            "UPDATE upstream_jobs SET status = 'done', result_json = ?, error = NULL, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps({**result.as_dict(), "mapped": mapped})[:20000], stamp, stamp, job["id"]),
        )
        log_activity(
            conn,
            "ott_synced",
            (
                f"SmartPlay OTT synced — {mapped.get('total', 0)} on portal, "
                f"{mapped.get('linked', 0)} linked, {mapped.get('created', 0)} new"
                + (" (simulated)" if result.simulated else "")
            ),
            meta_json=json.dumps({"job_id": job["id"], **mapped, **summary}),
        )


def _apply_account_success(job: dict, result) -> None:
    """Store a dealer-account snapshot (wallet balance, STB counts)."""
    if job["action"] == "online":
        _apply_online_success(job, result)
        return
    if job["action"] == "sync":
        if job["provider"] == "railtel":
            _apply_railtel_subscribers_sync(job, result)
        else:
            _apply_ott_sync(job, result)
        return

    stamp = now_iso()
    summary = wallet_summary(job["provider"], result.raw)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO provider_status(provider, wallet_balance, active_count, "
            "inactive_count, total_count, operator_name, checked_at, error) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "wallet_balance = excluded.wallet_balance, active_count = excluded.active_count, "
            "inactive_count = excluded.inactive_count, total_count = excluded.total_count, "
            "operator_name = excluded.operator_name, checked_at = excluded.checked_at, error = NULL",
            (
                job["provider"],
                summary["wallet_balance"],
                summary["active"],
                summary["inactive"],
                summary["total"],
                summary["operator"],
                stamp,
            ),
        )
        conn.execute(
            "UPDATE upstream_jobs SET status = 'done', result_json = ?, error = NULL, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(result.as_dict())[:20000], stamp, stamp, job["id"]),
        )
        balance = summary["wallet_balance"] or "not reported"
        log_activity(
            conn,
            "provider_checked",
            f"{job['provider']} dealer account refreshed — wallet {balance}"
            + (" (simulated)" if result.simulated else ""),
            meta_json=json.dumps({"job_id": job["id"], **summary}),
        )


def _apply_success(job: dict, result) -> None:
    if job["connection_id"] is None:
        _apply_account_success(job, result)
        return

    stamp = now_iso()
    with transaction() as conn:
        conn_row = conn.execute(
            "SELECT * FROM connections WHERE id = ?", (job["connection_id"],)
        ).fetchone()
        if conn_row is None:
            conn.execute(
                "UPDATE upstream_jobs SET status = 'failed', error = ?, completed_at = ?, "
                "updated_at = ? WHERE id = ?",
                ("Connection was deleted while the job was running.", stamp, stamp, job["id"]),
            )
            return

        package_row = None
        if conn_row["package_id"]:
            package_row = conn.execute(
                "SELECT * FROM packages WHERE id = ?", (conn_row["package_id"],)
            ).fetchone()

        provider_expiry = parse_date(result.expiry)
        bill_id = None
        summary = result.message or ACTION_LABELS.get(job["action"], job["action"])

        later = bool(job.get("collect_later"))

        if job["action"] == "renew":
            bill_id, period_start, new_expiry = billing.bill_for_renewal(
                conn, conn_row, package_row, provider_expiry=provider_expiry,
                job_id=job["id"], collect_later=later,
            )
            billing.bind_portal_plan(conn, int(conn_row["id"]), result.plan_name)
            new_expiry_s = new_expiry.strftime("%Y-%m-%d")
            conn.execute(
                "UPDATE connections SET expiry_date = ?, status = 'active', "
                "last_synced_at = ?, updated_at = ? WHERE id = ?",
                (
                    new_expiry_s,
                    stamp,
                    stamp,
                    conn_row["id"],
                ),
            )
            if (job["provider"] or "").lower() == "railtel" and not result.simulated:
                from .. import railtel_sync

                railtel_sync.patch_railtel_subscriber_after_renew(
                    conn,
                    conn_row["upstream_id"],
                    new_expiry_s,
                    package=result.plan_name or "",
                )
            billing.reconcile_customer(conn, job["customer_id"])
            summary = (
                f"Renewed until {new_expiry.strftime('%d %b %Y')}"
                + (" (simulated)" if result.simulated else "")
                + (" — collect payment later" if later else "")
            )

        elif job["action"] == "status":
            # A Hathway status check reports the viewing card, which the Bix export often
            # lacks, so fill it in when missing — never overwrite. Both portals put this in
            # a field called `mac`, but Railtel means the router's network MAC there, which
            # is not a card number and must not be stored as one.
            card = card_number_from(job["provider"], result.raw)
            link = link_state_from(job["provider"], result.raw)
            billing.bind_portal_plan(conn, int(conn_row["id"]), result.plan_name)
            ott_note = None
            if (job["provider"] or "").lower() == "ott":
                portal_acc = re.sub(r"\D", "", str((result.raw or {}).get("account_id") or ""))
                if portal_acc:
                    ott_note = f"SmartPlay OTT acc {portal_acc}"
            conn.execute(
                "UPDATE connections SET "
                "expiry_date = COALESCE(?, expiry_date), "
                "card_number = CASE WHEN COALESCE(card_number, '') = '' "
                "                   THEN NULLIF(?, '') ELSE card_number END, "
                "link_state = ?, link_since = ?, link_days = ?, "
                "notes = COALESCE(?, notes), "
                "last_synced_at = ?, updated_at = ? WHERE id = ?",
                (
                    provider_expiry.strftime("%Y-%m-%d") if provider_expiry else None,
                    card,
                    link["state"],
                    link["since"],
                    link["days"],
                    ott_note,
                    stamp,
                    stamp,
                    conn_row["id"],
                ),
            )
            summary = (
                f"Status synced — expiry {provider_expiry.strftime('%d %b %Y')}"
                if provider_expiry
                else "Status synced, but the portal did not report an expiry date"
            )
            if link["state"] == "offline":
                summary += " — the line is currently down"

        elif job["action"] == "deactivate":
            conn.execute(
                "UPDATE connections SET status = 'suspended', last_synced_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, conn_row["id"]),
            )
            summary = "Connection temporarily deactivated on provider portal"

        elif job["action"] == "activate":
            conn.execute(
                "UPDATE connections SET status = 'active', last_synced_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, conn_row["id"]),
            )
            summary = "Connection reactivated on provider portal"
            if later:
                bill_id = billing.bill_for_collect_later_enable(
                    conn, conn_row, package_row, job_id=job["id"]
                )
                billing.reconcile_customer(conn, job["customer_id"])
                summary = "Enabled — collect payment later"

        elif job["action"] == "subscribe":
            billing.bind_portal_plan(conn, int(conn_row["id"]), result.plan_name)
            conn.execute(
                "UPDATE connections SET "
                "expiry_date = COALESCE(?, expiry_date), "
                "status = 'active', last_synced_at = ?, updated_at = ? WHERE id = ?",
                (
                    provider_expiry.strftime("%Y-%m-%d") if provider_expiry else None,
                    stamp,
                    stamp,
                    conn_row["id"],
                ),
            )
            summary = (
                f"Subscribed on ANT — expiry {provider_expiry.strftime('%d %b %Y')}"
                if provider_expiry
                else (result.message or "Subscribed on ANT")
            )

        elif job["action"] == "retrack":
            conn.execute(
                "UPDATE connections SET last_synced_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, conn_row["id"]),
            )
            summary = "Set-top box retracked on provider portal"

        elif job["action"] == "remove_terminate":
            conn.execute(
                "UPDATE connections SET status = 'inactive', expiry_date = '', "
                "last_synced_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, conn_row["id"]),
            )
            summary = "Pack removed and set-top box terminated on provider portal"

        elif job["action"] == "download_bill":
            from .. import railtel_invoices as rt_inv

            raw = result.raw if isinstance(result.raw, dict) else {}
            screenshot_path = str(raw.get("file_path") or "").strip() or None
            if result.simulated:
                summary = result.message or "Simulated portal bill download"
            else:
                inv_id = rt_inv.save_invoice_record(
                    conn,
                    customer_id=int(job["customer_id"]),
                    connection_id=int(conn_row["id"]),
                    job_id=int(job["id"]),
                    upstream_id=conn_row["upstream_id"],
                    raw=raw,
                    stamp=stamp,
                )
                summary = (
                    f"Portal bill {raw.get('invoice_no') or 'saved'} — "
                    f"{raw.get('file_name') or 'PDF'}"
                    + (
                        "" if settings.railtel_invoice_whatsapp_after_download
                        else " (PDF saved — send WhatsApp manually)"
                    )
                )
                meta_extra = {"invoice_id": inv_id, **raw}
                conn.execute(
                    "UPDATE upstream_jobs SET screenshot_path = ? WHERE id = ?",
                    (screenshot_path, job["id"]),
                )
                log_activity(
                    conn,
                    "railtel_invoice",
                    summary,
                    customer_id=job["customer_id"],
                    connection_id=job["connection_id"],
                    meta_json=json.dumps({"job_id": job["id"], **meta_extra})[:8000],
                )

        conn.execute(
            "UPDATE upstream_jobs SET status = 'done', bill_id = ?, result_json = ?, "
            "error = NULL, completed_at = ?, updated_at = ? WHERE id = ?",
            (bill_id, json.dumps(result.as_dict())[:20000], stamp, stamp, job["id"]),
        )
        log_activity(
            conn,
            "job_done",
            summary,
            customer_id=job["customer_id"],
            connection_id=job["connection_id"],
            meta_json=json.dumps({"job_id": job["id"], "bill_id": bill_id}),
        )


def _auto_send_railtel_invoice_whatsapp(job: dict, result) -> None:
    """After a portal bill PDF is saved, attach and send it on WhatsApp Web."""
    from .. import railtel_invoices as rt_inv

    raw = result.raw if isinstance(result.raw, dict) else {}
    file_path = str(raw.get("file_path") or "").strip()
    if not file_path:
        return
    try:
        with connection() as conn:
            inv = conn.execute(
                "SELECT id FROM railtel_invoices WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                (job["id"],),
            ).fetchone()
            if inv is None:
                return
            cust = conn.execute(
                "SELECT name, phone FROM customers WHERE id = ?",
                (job["customer_id"],),
            ).fetchone()
            if cust is None:
                return
            inv_id = int(inv["id"])
            customer_name = cust["name"]
            customer_phone = cust["phone"]

        send = rt_inv.send_invoice_whatsapp_work(
            inv_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
        )
        note = send.get("message") or send.get("error") or "WhatsApp send attempted"
        with transaction() as conn:
            log_activity(
                conn,
                "whatsapp_sent" if send.get("ok") else "whatsapp_failed",
                note,
                customer_id=job["customer_id"],
                connection_id=job.get("connection_id"),
                meta_json=json.dumps({"job_id": job["id"], "invoice_id": inv_id, **send})[:4000],
            )
        if send.get("ok"):
            log.info("Invoice WhatsApp sent for job %s → %s", job["id"], note)
        else:
            log.warning("Invoice WhatsApp failed for job %s: %s", job["id"], note)
    except Exception:
        log.exception("Auto WhatsApp send failed for job %s", job["id"])


def _iptv_subscriber_fields(connection_id: int) -> dict:
    with transaction() as conn:
        row = conn.execute(
            "SELECT cn.upstream_plan_name, p.name AS package_name, "
            "c.name, c.address, c.area, c.pincode, c.notes "
            "FROM connections cn "
            "JOIN customers c ON c.id = cn.customer_id "
            "LEFT JOIN packages p ON p.id = cn.package_id "
            "WHERE cn.id = ?",
            (connection_id,),
        ).fetchone()
    if row is None:
        return {}
    notes = str(row["notes"] or "")
    state = "Karnataka"
    if "·" in notes:
        state = notes.split("·", 1)[-1].strip() or state
    return {
        "name": row["name"] or "",
        "package_name": row["package_name"] or row["upstream_plan_name"] or "",
        "address": row["address"] or "",
        "city": row["area"] or "Tiptur",
        "area": row["area"] or "Tiptur",
        "state": state,
        "pincode": row["pincode"] or "",
    }


def _saved_iptv_phone() -> str:
    phone = (getattr(settings, "iptv_phone", "") or "").strip()
    if len(re.sub(r"\D", "", phone)) == 10:
        return re.sub(r"\D", "", phone)[-10:]
    with transaction() as conn:
        stored = get_setting(conn, "ant_iptv_phone", "") or ""
    digits = re.sub(r"\D", "", stored)
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[-10:]
    return digits if len(digits) == 10 else ""


def _save_iptv_phone(phone: str) -> None:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[-10:]
    if len(digits) != 10:
        return
    with transaction() as conn:
        set_setting(conn, "ant_iptv_phone", digits)
        log_activity(conn, "setting_changed", "ANT IPTV WhatsApp login number saved")


def _ott_portal_account(connection_id: int) -> str:
    with transaction() as conn:
        row = conn.execute(
            "SELECT notes FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
    notes = str(row["notes"] or "") if row else ""
    match = re.search(r"SmartPlay OTT acc\s+(\d+)", notes, re.I)
    return match.group(1) if match else ""


def _live_extras(job: dict) -> dict | None:
    provider = (job.get("provider") or "").lower()
    if provider == "iptv":
        extras = {
            "request_otp": lambda prompt, job_id=int(job["id"]): request_job_otp(job_id, prompt),
            "save_login_phone": _save_iptv_phone,
            "state_path": str(Path(settings.db_path).parent / "ant_iptv_state.json"),
            "screenshot_dir": str(settings.screenshot_dir),
            "login_phone": _saved_iptv_phone(),
            "collect_later": bool(job.get("collect_later")),
        }
        if job.get("action") == "subscribe" and job.get("connection_id"):
            extras["subscriber"] = _iptv_subscriber_fields(int(job["connection_id"]))
        return extras
    if provider == "ott" and job.get("connection_id"):
        acc = _ott_portal_account(int(job["connection_id"]))
        if acc:
            return {"portal_account_id": acc}
    if (
        provider == "railtel"
        and job.get("action") == "download_bill"
        and job.get("connection_id")
    ):
        with connection() as conn:
            row = conn.execute(
                "SELECT c.name FROM connections cn "
                "JOIN customers c ON c.id = cn.customer_id WHERE cn.id = ?",
                (int(job["connection_id"]),),
            ).fetchone()
        return {
            "customer_name": (row["name"] if row else "") or "",
            "output_dir": str(settings.railtel_invoice_dir),
        }
    return None


def execute_job(job: dict) -> None:
    """Run one claimed job. The provider call happens outside any transaction."""
    if (
        (job.get("provider") or "").lower() == "railtel"
        and job.get("action") == "renew"
        and job.get("connection_id")
    ):
        from ..railtel_sync import railtel_renew_block_reason_from_conn

        with connection() as conn:
            reason = railtel_renew_block_reason_from_conn(conn, int(job["connection_id"]))
        if reason:
            _finish_failure(job, reason)
            return

    try:
        result = run_action(
            job["provider"], job["action"], job["upstream_id"], extras=_live_extras(job)
        )
    except UpstreamUnsupported as exc:
        with transaction() as conn:
            conn.execute(
                "UPDATE upstream_jobs SET status = 'failed', error = ?, completed_at = ?, "
                "updated_at = ? WHERE id = ?",
                (str(exc), now_iso(), now_iso(), job["id"]),
            )
        return
    except Exception as exc:  # noqa: BLE001 - worker must never die
        log.exception("Job %s crashed", job["id"])
        _finish_failure(job, f"{type(exc).__name__}: {exc}")
        return

    if result.ok:
        try:
            _apply_success(job, result)
            if (
                (job.get("provider") or "").lower() == "railtel"
                and job.get("action") == "download_bill"
                and not result.simulated
                and settings.railtel_invoice_whatsapp_after_download
            ):
                _auto_send_railtel_invoice_whatsapp(job, result)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to apply job %s result", job["id"])
            _finish_failure(job, f"Provider action succeeded but saving it failed: {exc}")
    else:
        log.warning("Job %s failed: %s", job["id"], result.error)
        _finish_failure(job, result.error or "Provider action failed")


# --------------------------------------------------------------------------- #
# Worker thread
# --------------------------------------------------------------------------- #

def sync_schedule(conn: sqlite3.Connection) -> dict:
    """The saved schedule, with defaults filled in."""
    out = {}
    for key, default in SYNC_DEFAULTS.items():
        out[key] = get_setting(conn, key, default) or default
    out["sync_last_run_date"] = get_setting(conn, "sync_last_run_date", "") or ""
    return out


def save_sync_schedule(conn: sqlite3.Connection, values: dict) -> None:
    for key in SYNC_DEFAULTS:
        if key in values:
            set_setting(conn, key, str(values[key]))


def run_scheduled_sweep() -> None:
    """Start the nightly sweep once, when its hour arrives."""
    now = datetime.now()
    with transaction() as conn:
        schedule = sync_schedule(conn)
        if schedule["sync_schedule_enabled"] not in ("1", "true", "yes", "on"):
            return
        try:
            hour = int(schedule["sync_schedule_hour"])
        except ValueError:
            hour = 2
        today_str = now.strftime("%Y-%m-%d")
        if now.hour != hour or schedule["sync_last_run_date"] == today_str:
            return

        # Claim the slot before doing the work, so a restart inside the hour cannot
        # start a second sweep.
        set_setting(conn, "sync_last_run_date", today_str)
        sweep_id, queued, note = start_sweep(
            conn,
            providers=schedule["sync_schedule_providers"],
            stale_days=int(schedule["sync_schedule_stale_days"]),
            limit=int(schedule["sync_schedule_limit"]),
            trigger="scheduled",
            requested_by="scheduler",
        )
    log.info("Scheduled sweep: %s", note)


def _worker_loop() -> None:
    log.info("Upstream worker started (mode=%s)", settings.upstream_mode)
    while not _stop_event.is_set():
        try:
            run_scheduled_sweep()
            close_finished_sweeps()
        except Exception:  # noqa: BLE001
            log.exception("Sweep bookkeeping failed")

        try:
            job = claim_next_job()
        except Exception:  # noqa: BLE001
            log.exception("Could not claim a job")
            job = None

        if job is None:
            _stop_event.wait(settings.job_poll_seconds)
            continue

        log.info("Running job %s: %s %s %s", job["id"], job["provider"], job["action"], job["upstream_id"])
        execute_job(job)
    log.info("Upstream worker stopped")


def start_worker() -> None:
    global _worker_thread
    if not settings.worker_enabled:
        log.info("Upstream worker disabled by configuration")
        return
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="upstream-worker", daemon=True)
    _worker_thread.start()


def stop_worker(timeout: float = 5.0) -> None:
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=timeout)


def reset_stuck_jobs() -> int:
    """Requeue jobs left `running` or waiting for OTP by a crash or restart."""
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE upstream_jobs SET status = 'queued', error = NULL, updated_at = ? "
            "WHERE status IN ('running', 'awaiting_otp')",
            (now_iso(),),
        )
        return cursor.rowcount or 0


def drain_queue(max_jobs: int = 50) -> int:
    """Run queued jobs inline. Used by tests and the CLI, not by the web app."""
    done = 0
    while done < max_jobs:
        job = claim_next_job()
        if job is None:
            break
        execute_job(job)
        done += 1
    return done
