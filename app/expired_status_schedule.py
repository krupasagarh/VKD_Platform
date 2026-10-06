"""Expired-list portal checks — auto enqueue is disabled.

Jobs only start from explicit Check / Renew / Collect-later / provider buttons.
The scheduler thread and page-view refresh are no-ops so opening an expired list
does not queue Hathway/Railtel/ANT work.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .db import get_setting, log_activity, set_setting, transaction
from .money import now_iso, parse_datetime

log = logging.getLogger("vk_platform.expired_status")

EXPIRED_STATUS_DEFAULTS = {
    "expired_status_refresh_enabled": "0",
    "expired_status_last_run_1d": "",
    "expired_status_last_run_2d": "",
    "expired_status_last_run_7d": "",
    "expired_status_interval_1d_hours": "2",
    "expired_status_interval_2d_hours": "5",
    "expired_status_interval_7d_hours": "5",
    "expired_status_limit_per_bucket": "50",
}

BUCKETS: tuple[dict, ...] = (
    {"when": "1d", "setting_hours": "expired_status_interval_1d_hours", "default_hours": 2},
    {"when": "2d", "setting_hours": "expired_status_interval_2d_hours", "default_hours": 5},
    {"when": "7d", "setting_hours": "expired_status_interval_7d_hours", "default_hours": 5},
)

# Do not start another bucket within this gap — keeps portal logins serial and light.
MIN_BUCKET_GAP = timedelta(minutes=45)

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_bucket_started: datetime | None = None
_bucket_lock = threading.Lock()
_poll_seconds = 90


def schedule(conn) -> dict:
    out = {}
    for key, default in EXPIRED_STATUS_DEFAULTS.items():
        out[key] = get_setting(conn, key, default) or default
    return out


def _enabled(conn) -> bool:
    return False


def _interval_hours(conn, spec: dict) -> int:
    raw = get_setting(conn, spec["setting_hours"], str(spec["default_hours"])) or str(
        spec["default_hours"]
    )
    try:
        return max(1, int(raw))
    except ValueError:
        return int(spec["default_hours"])


def _last_run(conn, when: str) -> datetime | None:
    raw = (get_setting(conn, f"expired_status_last_run_{when}", "") or "").strip()
    if not raw:
        return None
    return parse_datetime(raw)


def _seed_staggered_last_runs(conn) -> None:
    """First boot: spread buckets so 2d/7d do not fire with 1d."""
    now = datetime.now()
    if not get_setting(conn, "expired_status_last_run_1d", ""):
        set_setting(conn, "expired_status_last_run_1d", now_iso())
    if not get_setting(conn, "expired_status_last_run_2d", ""):
        set_setting(
            conn,
            "expired_status_last_run_2d",
            (now - timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        )
    if not get_setting(conn, "expired_status_last_run_7d", ""):
        set_setting(
            conn,
            "expired_status_last_run_7d",
            (now - timedelta(hours=1, minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        )


def _bucket_due(conn, when: str, interval_hours: int) -> bool:
    last = _last_run(conn, when)
    if last is None:
        return True
    return datetime.now() - last >= timedelta(hours=interval_hours)


def enqueue_stale_expired_connections(
    conn,
    *,
    when: str,
    limit: int,
    min_stale_hours: float,
    trigger: str,
) -> int:
    from . import repo
    from .upstream.jobs import JOB_OPEN_STATUSES, PRIORITY_EXPIRED_REFRESH, enqueue_job, enqueue_status_batch
    from .upstream.providers import id_problem

    open_sql = ", ".join(f"'{s}'" for s in JOB_OPEN_STATUSES)
    rows = repo.expiry_window_connections(conn, when=when, limit=limit)
    cutoff = (datetime.now() - timedelta(hours=min_stale_hours)).strftime("%Y-%m-%d %H:%M:%S")

    stale_by_provider: dict[str, list] = {"railtel": [], "hathway": [], "other": []}
    for row in rows:
        if id_problem(row["provider"], row["upstream_id"] or ""):
            continue
        open_job = conn.execute(
            "SELECT id FROM upstream_jobs WHERE connection_id = ? AND action IN ('status', 'status_batch') "
            f"AND status IN ({open_sql}) LIMIT 1",
            (int(row["id"]),),
        ).fetchone()
        if open_job:
            continue
        verified = (row["last_status_at"] or row["last_synced_at"] or "").strip()
        if verified and verified >= cutoff:
            continue
        prov = (row["provider"] or "").strip().lower()
        if prov in stale_by_provider:
            stale_by_provider[prov].append(row)
        else:
            stale_by_provider["other"].append(row)

    queued = 0
    requested_by = f"expired-{when}-{trigger}"
    for provider in ("railtel", "hathway"):
        prov_rows = stale_by_provider.get(provider) or []
        if not prov_rows:
            continue
        job_id = enqueue_status_batch(
            conn,
            provider=provider,
            connection_ids=[int(r["id"]) for r in prov_rows],
            requested_by=requested_by,
            priority=PRIORITY_EXPIRED_REFRESH,
        )
        if job_id:
            queued += len(prov_rows)

    for row in stale_by_provider.get("other") or []:
        enqueue_job(
            conn,
            connection_id=int(row["id"]),
            action="status",
            requested_by=requested_by,
            needs_confirmation=False,
            max_attempts=2,
            priority=PRIORITY_EXPIRED_REFRESH,
            quiet=True,
        )
        queued += 1
    return queued


def _run_one_due_bucket(*, trigger: str) -> int:
    global _last_bucket_started
    with _bucket_lock:
        if _last_bucket_started and datetime.now() - _last_bucket_started < MIN_BUCKET_GAP:
            return 0

        with transaction() as conn:
            if not _enabled(conn):
                return 0
            _seed_staggered_last_runs(conn)
            limit = max(1, int(schedule(conn).get("expired_status_limit_per_bucket") or 50))

            for spec in BUCKETS:
                when = spec["when"]
                hours = _interval_hours(conn, spec)
                if not _bucket_due(conn, when, hours):
                    continue
                queued = enqueue_stale_expired_connections(
                    conn,
                    when=when,
                    limit=limit,
                    min_stale_hours=float(hours),
                    trigger=trigger,
                )
                set_setting(conn, f"expired_status_last_run_{when}", now_iso())
                if queued:
                    log_activity(
                        conn,
                        "expired_status_refresh",
                        f"Queued {queued} portal check(s) in batch mode for {when} expired list ({trigger})",
                        actor="expired-scheduler",
                        meta_json=f'{{"when":"{when}","queued":{queued},"trigger":"{trigger}"}}',
                    )
                log.info(
                    "Expired status refresh (%s, %s): queued %s connection(s)",
                    when,
                    trigger,
                    queued,
                )
                _last_bucket_started = datetime.now()
                return queued
    return 0


def _scheduler_loop() -> None:
    log.info("Expired status scheduler started")
    while not _stop_event.is_set():
        try:
            _run_one_due_bucket(trigger="scheduler")
        except Exception:  # noqa: BLE001
            log.exception("Expired status scheduler tick failed")
        _stop_event.wait(_poll_seconds)
    log.info("Expired status scheduler stopped")


def cancel_auto_expired_jobs() -> int:
    """Cancel leftover auto jobs (page-open or scheduler) that are still waiting."""
    from .upstream.jobs import cancel_job

    cancelled = 0
    with transaction() as conn:
        set_setting(conn, "expired_status_refresh_enabled", "0")
        set_setting(conn, "sync_schedule_enabled", "0")
        set_setting(conn, "bix_history_schedule_enabled", "0")
        rows = conn.execute(
            "SELECT id FROM upstream_jobs "
            "WHERE status IN ('queued', 'awaiting_confirm', 'awaiting_otp') "
            "AND (requested_by LIKE 'expired-%' OR requested_by = 'scheduler')"
        ).fetchall()
        for row in rows:
            if cancel_job(conn, int(row["id"]), actor="auto-job-cleanup"):
                cancelled += 1
    if cancelled:
        log.info("Cancelled %s leftover auto expired/sweep job(s)", cancelled)
    return cancelled


def start_expired_status_scheduler() -> None:
    """No-op: expired lists no longer auto-queue portal jobs."""
    log.info("Expired status scheduler is disabled — jobs only start from explicit actions")
    return


def stop_expired_status_scheduler(timeout: float = 3.0) -> None:
    _stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=timeout)


def request_expired_window_refresh(when: str) -> None:
    """No-op: opening an expired list no longer queues portal jobs."""
    return
