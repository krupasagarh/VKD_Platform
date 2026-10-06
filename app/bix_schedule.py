"""Scheduled Bix history extract + import (runs inside the platform worker thread)."""
from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .config import WORKSPACE_DIR, settings
from .db import get_setting, log_activity, set_setting, transaction

log = logging.getLogger("vk_platform.bix_schedule")

BIX_HISTORY_SCHEDULE_DEFAULTS = {
    "bix_history_schedule_enabled": "0",
    "bix_history_schedule_hour": "4",
    "bix_history_auto_extract": "0",
    "bix_history_last_run_date": "",
    "bix_history_last_archive_mtime": "0",
}


def history_schedule(conn: sqlite3.Connection) -> dict:
    out = {}
    for key, default in BIX_HISTORY_SCHEDULE_DEFAULTS.items():
        out[key] = get_setting(conn, key, default) or default
    return out


def save_history_schedule(conn: sqlite3.Connection, values: dict) -> None:
    for key in BIX_HISTORY_SCHEDULE_DEFAULTS:
        if key in values:
            set_setting(conn, key, str(values[key]))


def _archive_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        return 0.0


def _run_server_extract() -> dict:
    """Pull balance history from Bix — Playwright (preferred) or cookies.json fallback."""
    extract_dir = WORKSPACE_DIR / "bix42_export"
    if str(extract_dir) not in sys.path:
        sys.path.insert(0, str(extract_dir))

    playwright_script = extract_dir / "playwright_extract.py"
    if playwright_script.is_file():
        try:
            import playwright_extract  # type: ignore

            result = playwright_extract.run_extract_playwright(headless=True)
            if result.get("ok"):
                return result
            log.warning("Playwright Bix extract: %s", result.get("error"))
        except ImportError:
            log.info("Playwright not installed — using cookies.json extract if available")
        except Exception as exc:  # noqa: BLE001
            log.exception("Playwright Bix extract failed")

    script = extract_dir / "server_extractor.py"
    if not script.is_file():
        return {"ok": False, "error": f"Missing {script}"}
    cookies = extract_dir / "cookies.json"
    if not cookies.is_file():
        return {
            "ok": False,
            "error": (
                "Bix extract failed — run once: python bix42_export/auto_extract.py --login "
                "(or set BIX_MOBILE + BIX_PASSWORD in .env)"
            ),
        }
    try:
        import server_extractor  # type: ignore

        return server_extractor.run_extract()
    except Exception as exc:  # noqa: BLE001
        log.exception("Bix server extract failed")
        return {"ok": False, "error": str(exc)}


def _import_if_changed(conn: sqlite3.Connection, *, actor: str) -> dict | None:
    from . import bix_history

    path = bix_history.default_archive_path()
    if not path.is_file():
        return None
    mtime = _archive_mtime(path)
    last = get_setting(conn, "bix_history_last_archive_mtime", "0") or "0"
    try:
        if float(last) >= mtime:
            return {"skipped": True, "reason": "archive unchanged"}
    except ValueError:
        pass
    summary = bix_history.import_archive(conn, path, actor=actor)
    set_setting(conn, "bix_history_last_archive_mtime", str(mtime))
    return summary


def run_history_sync(*, actor: str = "scheduler", force_extract: bool = False) -> dict:
    """Extract (optional) then import Bix history into the platform."""
    result: dict = {"extract": None, "import": None}
    with transaction() as conn:
        sched = history_schedule(conn)
        if force_extract or sched.get("bix_history_auto_extract") in ("1", "true", "yes", "on"):
            result["extract"] = _run_server_extract()
        result["import"] = _import_if_changed(conn, actor=actor)
    if result["import"] and not result["import"].get("skipped"):
        imp = result["import"]
        log.info(
            "Bix history import: %s new row(s), %s matched",
            imp.get("txns_new"),
            imp.get("matched"),
        )
    return result


def run_scheduled_bix_history() -> None:
    """Once per day at the configured hour — optional extract, then import if archive grew."""
    now = datetime.now()
    with transaction() as conn:
        sched = history_schedule(conn)
        if sched["bix_history_schedule_enabled"] not in ("1", "true", "yes", "on"):
            return
        try:
            hour = int(sched["bix_history_schedule_hour"])
        except ValueError:
            hour = 4
        today_str = now.strftime("%Y-%m-%d")
        if now.hour != hour or sched["bix_history_last_run_date"] == today_str:
            return
        set_setting(conn, "bix_history_last_run_date", today_str)

    summary = run_history_sync(actor="scheduler")
    parts = []
    ext = summary.get("extract") or {}
    if ext:
        if ext.get("ok"):
            parts.append(
                f"extracted {ext.get('customers', 0)} customer(s), "
                f"{ext.get('new_rows', 0)} new row(s)"
            )
        else:
            parts.append(f"extract skipped: {ext.get('error', 'failed')}")
    imp = summary.get("import") or {}
    if imp.get("skipped"):
        parts.append("import unchanged")
    elif imp:
        parts.append(
            f"imported {imp.get('txns_new', 0)} new row(s), "
            f"{imp.get('matched', 0)} matched"
        )
    message = "Bix history schedule — " + ("; ".join(parts) if parts else "nothing to do")
    with transaction() as conn:
        log_activity(conn, "bix_history_schedule", message, actor="scheduler")
