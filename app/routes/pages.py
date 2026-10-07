"""Server-rendered pages and form handlers."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, billing, bix_history, bix_schedule, bix_sync, field, hathway_expiry_sync, inventory, iptv_plans, list_exports, ott_plans, public_pay, repo, settlements
from ..csv_export import EXPORT_ROW_LIMIT, export_url, wants_csv
from ..config import settings
from ..db import connection, get_setting, log_activity, set_setting, transaction
from ..money import add_days, fmt_rupees, normalise_expiry_input, now_iso, parse_date, to_paise, today
from ..plans import price_plan
from ..upstream import jobs as job_queue
from ..upstream.providers import (
    ACCOUNT_ACTIONS,
    ACTION_LABELS,
    DESTRUCTIVE_ACTIONS,
    PROVIDER_ACTIONS,
    PROVIDER_LABELS,
    PROVIDER_LABELS_VIEW,
    PROVIDERS,
    provider_label,
    id_problem,
    normalise_upstream_id,
)

router = APIRouter()


def _job_by(request: Request) -> str:
    return auth.job_requested_by(getattr(request.state, "agent", None))

PAYMENT_MODES = ("cash", "upi", "scanner", "owner_upi", "bank", "gateway", "cheque")
CONNECTION_STATUSES = ("active", "suspended", "inactive", "terminated")
PLAN_TERMS = (
    (30, "30 days (1 month)"),
    (100, "100 days (x3)"),
    (180, "180 days (6 months, no free month)"),
    (210, "210 days (6 months + 1 month free)"),
    (360, "360 days (x10 / year)"),
)


def _parse_term_days(raw: str) -> int:
    text = (raw or "").strip().lower()
    if not text or text in {"0", "catalog", "default"}:
        return 0
    if text.isdigit():
        return max(0, int(text))
    return 0

_GEO_AT = re.compile(r"@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)")
_GEO_PAIR = re.compile(r"(-?\d{1,2}\.\d+)\s*[, ]\s*(-?\d{1,3}\.\d+)")


def parse_geo(text: str) -> tuple[float, float] | None:
    """Read lat,lng from typed coordinates or a Google Maps link."""
    raw = unquote((text or "").strip())
    if not raw:
        return None
    match = _GEO_AT.search(raw) or _GEO_PAIR.search(raw)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    if abs(lat) > 90 or abs(lng) > 180:
        return None
    if abs(lat) < 0.001 and abs(lng) < 0.001:
        return None
    return lat, lng


def maps_nav_url(lat, lng) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"


def maps_view_url(lat, lng) -> str:
    return f"https://www.google.com/maps?q={lat},{lng}"


def maps_search_url(
    *,
    name: str = "",
    sub_area: str = "",
    area: str = "Tiptur",
) -> str:
    """Open Google Maps search — for houses not geo-tagged yet."""
    parts = [p.strip() for p in (name, sub_area, area) if (p or "").strip()]
    if not parts:
        return "https://www.google.com/maps"
    return f"https://www.google.com/maps/search/?api=1&query={quote(', '.join(parts))}"


def _redirect_target(next_url: str, fallback: str) -> str:
    target = (next_url or "").strip()
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _resolve_package_id(conn, provider: str, name: str) -> tuple[int | None, bool]:
    """Look a plan up by name. Returns (package_id, name_was_given_but_unknown)."""
    name = (name or "").strip()
    if not name:
        return None, False
    row = conn.execute(
        "SELECT id FROM packages WHERE provider = ? AND lower(name) = lower(?) LIMIT 1",
        (provider, name),
    ).fetchone()
    return (int(row["id"]), False) if row else (None, True)


def _templates():
    from ..main import templates
    from ..repo import job_created_by_label

    if "job_created_by" not in templates.env.filters:
        templates.env.filters["job_created_by"] = job_created_by_label
    return templates


def _with_job_creator(rows):
    """Attach Created-by text so job tables work even if a Jinja filter is missing."""
    out = []
    for row in rows or []:
        item = dict(row)
        item["created_by_label"] = repo.job_created_by_label(item)
        out.append(item)
    return out


def _strip_flash_params(path: str) -> str:
    base, _, query = path.partition("?")
    if not query:
        return path
    pairs = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key not in {"flash", "level", "wa"}
    ]
    clean = urlencode(pairs)
    return f"{base}?{clean}" if clean else base


def _redirect(
    path: str, *, flash: str = "", level: str = "ok", whatsapp_url: str = ""
) -> RedirectResponse:
    path = _strip_flash_params(path)
    params = {}
    if flash:
        params["flash"] = flash
        params["level"] = level
    if whatsapp_url:
        params["wa"] = whatsapp_url
    if params:
        path = f"{path}{'&' if '?' in path else '?'}{urlencode(params)}"
    return RedirectResponse(path, status_code=303)


def _collect_paid_at(raw: str, *, can_set_date: bool) -> str:
    """Use now unless the agent may backdate, then keep today's time on the chosen day."""
    stamp = now_iso()
    if not can_set_date:
        return stamp
    day = parse_date((raw or "").strip())
    if day is None:
        return stamp
    if day > today():
        return stamp
    clock = stamp.split(" ", 1)[-1] if " " in stamp else "12:00:00"
    return f"{day.isoformat()} {clock}"


def _safe_next(next_url: str, fallback: str) -> str:
    path = (next_url or "").strip()
    if path in {"/", "/jobs", "/providers", "/iptv", "/ott", "/payments"}:
        return path
    if path.startswith(("/iptv", "/ott", "/customers/", "/field", "/jobs", "/providers", "/payments")):
        base, _, query = path.partition("?")
        if base == "/payments/follow-up" and query in {"kind=manual", "kind=renew"}:
            return f"{base}?{query}"
        if base.startswith("/customers/") and query:
            qs = parse_qs(query)
            keep = {}
            tab = (qs.get("tab") or [""])[0]
            if tab in {"connections", "statement", "complaints", "jobs", "plan"}:
                keep["tab"] = tab
            if (qs.get("filter") or [""])[0] == "payments":
                keep["filter"] = "payments"
            if keep:
                return f"{base}?{urlencode(keep)}"
        return base
    return fallback


def _job_flash(action: str, job_id: int, provider: str, *, busy_note: str = "") -> str:
    label = ACTION_LABELS.get(action, action)
    if (provider or "").lower() == "iptv":
        if settings.is_live:
            msg = (
                f"{label} started as job #{job_id}. Stay on this page — "
                f"the yellow bar will ask for the ANT login phone, then the WhatsApp OTP."
            )
        else:
            msg = f"{label} started as job #{job_id} (simulate — no portal, no OTP)."
    elif (provider or "").lower() == "ott":
        if settings.is_live:
            if action == "renew":
                msg = (
                    f"{label} started as job #{job_id}. "
                    "SmartPlay cash Pay will debit the dealer wallet."
                )
            else:
                msg = f"{label} started as job #{job_id}."
        else:
            msg = f"{label} started as job #{job_id} (simulate — no portal)."
    else:
        msg = f"{label} queued as job #{job_id}. Confirm it to run."
    return msg + (busy_note or "")


def _exclusive_busy_note(conn, action: str, job_id: int) -> str:
    """If a renew/sync is already on the portal, say the new one will wait."""
    if action not in job_queue.EXCLUSIVE_PORTAL_ACTIONS:
        return ""
    other = job_queue.blocking_exclusive_job(conn, except_job_id=job_id)
    if not other:
        return ""
    who = (other["upstream_id"] or other["customer_name"] or other["provider"] or "").strip()
    label = ACTION_LABELS.get(other["action"], other["action"])
    bit = f"{label} {who}".strip()
    return (
        f" It waits until job #{other['id']} ({bit}) finishes — "
        f"only renewals and portal sync share that login."
    )


def _forbidden(message: str = "You do not have access to that."):
    return _redirect("/", flash=message, level="err")


def _parse_late_sort(params) -> tuple[str, int | None]:
    sort = (params.get("sort") or "").strip()
    if sort != "late":
        return "", None
    raw = (params.get("late_days") or "").strip()
    late_days = int(raw) if raw.isdigit() and int(raw) > 0 else None
    return "late", late_days


def _list_qs(**params) -> str:
    return urlencode({k: str(v) for k, v in params.items() if v not in (None, "", [])})


def _render(request: Request, template: str, **context) -> HTMLResponse:
    agent = getattr(request.state, "agent", None)
    with connection() as conn:
        nav = {
            "jobs_awaiting": conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE status = 'awaiting_confirm'"
            ).fetchone()["n"],
            "jobs_otp": conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE status = 'awaiting_otp'"
            ).fetchone()["n"],
            "jobs_iptv_pending": conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE provider = 'iptv' "
                "AND status IN ('queued', 'running', 'awaiting_otp')"
            ).fetchone()["n"],
            "jobs_failed": conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE status = 'failed'"
            ).fetchone()["n"],
            "jobs_running": conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE status = 'running' "
                f"AND {job_queue.exclusive_job_sql()}"
            ).fetchone()["n"],
            "jobs_queued": conn.execute(
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE status = 'queued'"
            ).fetchone()["n"],
            "complaints_open": 0,
        }
        try:
            if agent and agent.get("role") == "admin":
                nav["complaints_open"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM complaints WHERE status != 'fixed'"
                ).fetchone()["n"]
            elif agent:
                nav["complaints_open"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM complaints WHERE status != 'fixed' "
                    "AND assigned_agent_id = ?",
                    (agent.get("id"),),
                ).fetchone()["n"]
        except Exception:
            nav["complaints_open"] = 0
        nav["followups"] = 0
        try:
            scope = auth.agent_provider_scope(agent) or ""
            nav["followups"] = repo.collect_later_stats(
                conn, provider=scope, collector_id=auth.collector_id_for(agent) if agent else None
            )["count"]
        except Exception:
            nav["followups"] = 0
        nav["pay_portal_open"] = 0
        try:
            if agent and auth.can(agent, "payments"):
                nav["pay_portal_open"] = public_pay.count_open_pay_intents(conn)
        except Exception:
            nav["pay_portal_open"] = 0
        nav["unpaid_renewals"] = 0
        try:
            month_start = today().replace(day=1).strftime("%Y-%m-%d")
            scope = auth.agent_provider_scope(agent) or ""
            nav["unpaid_renewals"] = repo.collect_later_stats(
                conn,
                kind="renew",
                since=month_start,
                provider=scope,
                collector_id=auth.collector_id_for(agent) if agent else None,
            )["customers"]
        except Exception:
            nav["unpaid_renewals"] = 0
        otp_rows = conn.execute(
            "SELECT j.id, j.action, j.provider, j.error, c.name AS customer_name, "
            "cn.upstream_id FROM upstream_jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "LEFT JOIN connections cn ON cn.id = j.connection_id "
            "WHERE j.status = 'awaiting_otp' ORDER BY j.id"
        ).fetchall()
        path = request.url.path or ""
        progress_sql = (
            "SELECT j.id, j.action, j.status, j.error, c.name AS customer_name, "
            "cn.upstream_id FROM upstream_jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "LEFT JOIN connections cn ON cn.id = j.connection_id "
            "WHERE j.provider = 'iptv' AND j.status IN ('queued', 'running') "
        )
        progress_args: tuple = ()
        if path.startswith("/jobs"):
            progress_sql += "ORDER BY j.id"
        else:
            cust_m = re.match(r"^/customers/(\d+)$", path)
            if cust_m:
                progress_sql += "AND j.customer_id = ? ORDER BY j.id"
                progress_args = (int(cust_m.group(1)),)
            else:
                progress_sql = ""
        progress_rows = (
            conn.execute(progress_sql, progress_args).fetchall() if progress_sql else []
        )
        running_rows = conn.execute(
            "SELECT j.id, j.action, j.provider, j.status, c.name AS customer_name, "
            "cn.upstream_id FROM upstream_jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "LEFT JOIN connections cn ON cn.id = j.connection_id "
            f"WHERE j.status = 'running' AND {job_queue.exclusive_job_sql('j')} "
            "ORDER BY j.id"
        ).fetchall()
    context.setdefault("otp_jobs", [dict(row) for row in otp_rows])
    context.setdefault("iptv_progress", [dict(row) for row in progress_rows])
    context.setdefault("running_jobs", [dict(row) for row in running_rows])
    context.setdefault("flash", request.query_params.get("flash", ""))
    context.setdefault("flash_level", request.query_params.get("level", "ok"))
    context.setdefault("whatsapp_open_url", request.query_params.get("wa", ""))
    context.setdefault("nav", nav)
    context.setdefault("provider_labels", PROVIDER_LABELS_VIEW)
    from ..railtel_accounts import RAILTEL_DEALER_LABELS, RAILTEL_DEALER_SHORT, RAILTEL_DEALERS

    context.setdefault("railtel_dealers", RAILTEL_DEALERS)
    context.setdefault("railtel_dealer_labels", RAILTEL_DEALER_LABELS)
    context.setdefault("railtel_dealer_short", RAILTEL_DEALER_SHORT)
    context.setdefault("fixed_provider", auth.agent_provider_scope(agent))
    context.setdefault("action_labels", ACTION_LABELS)
    context.setdefault("live_mode", settings.is_live)
    context.setdefault("owner_name", settings.operator or "Owner")
    context["current_agent"] = agent
    context["can"] = lambda perm: auth.can(agent, perm)
    context["can_settlements"] = auth.can_use_settlements(agent)
    context["needs_field_duty"] = field.needs_field_duty(agent)
    context["field_duty_on"] = field.is_field_duty_on(agent)
    context["ui_v2"] = request.cookies.get("vk_ui") == "v2"
    context["request"] = request
    return _templates().TemplateResponse(request, template, context)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return _templates().TemplateResponse(
        request,
        "login.html",
        {"error": "", "next": request.query_params.get("next", "/"), "settings": settings},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    username: str = Form(""),
    next: str = Form("/"),
):
    ip = auth.request_ip(request)
    locked = auth.login_lock_message(ip, username)
    if locked:
        return _templates().TemplateResponse(
            request,
            "login.html",
            {"error": locked, "next": next, "settings": settings},
            status_code=429,
        )
    with transaction() as conn:
        agent = auth.authenticate(conn, username, password)
        if agent is not None:
            auth.mark_agent_login(conn, agent["id"])
    if agent is None:
        auth.record_login_failure(ip, username)
        again = auth.login_lock_message(ip, username)
        return _templates().TemplateResponse(
            request,
            "login.html",
            {"error": again or "Wrong username or password.", "next": next, "settings": settings},
            status_code=401,
        )
    auth.clear_login_failures(ip, username)
    from .pages_v2 import UI_COOKIE, UI_COOKIE_MAX_AGE
    from .pay_portal import _mobile_client

    target = next if next.startswith("/") else "/"
    mobile = _mobile_client(request) and target in ("/", "")
    response = RedirectResponse("/v2/" if mobile else target, status_code=303)
    auth.set_login_cookie(response, agent["id"])
    if mobile:
        response.set_cookie(
            UI_COOKIE, "v2", max_age=UI_COOKIE_MAX_AGE, httponly=False, samesite="lax", path="/",
        )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    auth.clear_login_cookie(response)
    return response


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    collector_id = auth.collector_id_for(request.state.agent)
    with connection() as conn:
        stats = repo.dashboard_stats(
            conn,
            agent_scope=auth.agent_provider_scope(request.state.agent) or "",
            collector_id=collector_id,
        )
        expiring = repo.expiring_connections(
            conn,
            limit=15,
            provider=auth.agent_provider_scope(request.state.agent) or "",
        )
        job_provider = auth.agent_provider_scope(request.state.agent) or ""
        awaiting = _with_job_creator(repo.list_jobs(
            conn, status="awaiting_confirm", limit=10, provider=job_provider
        ))
        awaiting_otp = _with_job_creator(repo.list_jobs(
            conn, status="awaiting_otp", limit=10, provider=job_provider
        ))
        failed = _with_job_creator(repo.list_jobs(
            conn, status="failed", limit=5, provider=job_provider
        ))
        activity = repo.recent_activity(
            conn,
            limit=12,
            provider=auth.agent_provider_scope(request.state.agent) or "",
        )
        if collector_id:
            hidden = repo.owner_customer_ids(conn)
            expiring = [r for r in expiring if int(r["customer_id"] or 0) not in hidden]
            activity = [r for r in activity if int(r["customer_id"] or 0) not in hidden]
        recent_fixed = repo.recent_fixed_complaints(conn, limit=8)
    return _render(
        request,
        "dashboard.html",
        stats=stats,
        expiring=expiring,
        awaiting=awaiting,
        awaiting_otp=awaiting_otp,
        failed=failed,
        activity=activity,
        recent_fixed=recent_fixed,
    )


# --------------------------------------------------------------------------- #
# Field agents
# --------------------------------------------------------------------------- #

def _forbid_field(request: Request, target_id: int | None = None):
    agent = request.state.agent
    if target_id is None:
        if not field.can_see_roster(agent):
            return _forbidden("You cannot see field tracking.")
        return None
    if not field.can_see_agent(agent, target_id):
        return _forbidden("You can only see your own field day.")
    return None


@router.get("/field", response_class=HTMLResponse)
async def field_roster(request: Request):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    day = (request.query_params.get("day") or today().strftime("%Y-%m-%d")).strip()
    viewer = request.state.agent
    only = None if field.sees_everyone(viewer) else int(viewer["id"])
    with connection() as conn:
        rows = field.agent_summaries(conn, day=day, only_agent_id=only)
        office = field.office_summary(conn, day, only_agent_id=only)
    map_pins = [
        {
            "id": row["id"],
            "name": row["name"],
            "lat": row["lat"],
            "lng": row["lng"],
            "last_at": row["last_at"],
            "in_app": row.get("in_app"),
            "maps_view": row.get("maps_view") or "",
        }
        for row in rows
        if row.get("lat") is not None and row.get("lng") is not None
    ]
    return _render(
        request,
        "field.html",
        rows=rows,
        day=day,
        office=office,
        everyone=field.sees_everyone(viewer),
        self_id=int(viewer["id"]),
        map_pins=map_pins,
    )


def _require_field_duty(request: Request, next_url: str):
    agent = request.state.agent
    if not field.needs_field_duty(agent):
        return None
    if field.is_field_duty_on(agent):
        return None
    return _redirect(next_url, flash="Turn on Field login first.", level="err")


@router.post("/field/duty")
async def field_duty_toggle(
    request: Request,
    on: str = Form("1"),
    next: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
):
    agent = request.state.agent or {}
    agent_id = agent.get("id")
    if not agent_id:
        return _forbidden()
    enable = (on or "").strip().lower() not in {"0", "off", "false", "no"}
    coords = field.parse_coords(lat, lng)
    with transaction() as conn:
        stamp = field.set_field_duty(conn, int(agent_id), enable)
        if enable and coords:
            field.record_location(
                conn,
                agent_id=int(agent_id),
                lat=coords[0],
                lng=coords[1],
                accuracy=field.parse_accuracy(accuracy),
                source="ping",
            )
    dest = (next or "").strip() or f"/field/{int(agent_id)}"
    if not dest.startswith("/"):
        dest = f"/field/{int(agent_id)}"
    return _redirect(dest)


@router.post("/field/ping")
async def field_ping(
    request: Request,
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
    source: str = Form("ping"),
    customer_id: str = Form(""),
):
    agent = request.state.agent or {}
    agent_id = agent.get("id")
    if not agent_id:
        return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
    if field.needs_field_duty(agent) and not field.is_field_duty_on(agent):
        return JSONResponse({"ok": False, "error": "field login off"}, status_code=403)
    coords = field.parse_coords(lat, lng)
    if coords is None:
        return JSONResponse({"ok": False, "error": "no coordinates"}, status_code=400)
    cid = int(customer_id) if customer_id.strip().isdigit() else None
    src = (source or "ping").strip().lower()
    if src not in field.SOURCE_LABELS:
        src = "ping"
    with transaction() as conn:
        loc_id = field.record_location(
            conn,
            agent_id=int(agent_id),
            lat=coords[0],
            lng=coords[1],
            accuracy=field.parse_accuracy(accuracy),
            source=src,
            customer_id=cid,
        )
    return JSONResponse({"ok": True, "id": loc_id})


@router.get("/field/{agent_id}", response_class=HTMLResponse)
async def field_agent(request: Request, agent_id: int):
    blocked = _forbid_field(request, agent_id)
    if blocked:
        return blocked
    day = (request.query_params.get("day") or today().strftime("%Y-%m-%d")).strip()
    with connection() as conn:
        detail = field.agent_day(conn, agent_id, day=day)
    if detail is None:
        return _redirect("/field", flash="Agent not found.", level="err")
    return _render(
        request,
        "field_agent.html",
        detail=detail,
        day=day,
        is_self=int((request.state.agent or {}).get("id") or 0) == agent_id,
        everyone=field.sees_everyone(request.state.agent),
        source_labels=field.SOURCE_LABELS,
    )


@router.post("/field/{agent_id}/location")
async def field_share_location(
    request: Request,
    agent_id: int,
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
    paste: str = Form(""),
):
    blocked = _forbid_field(request, agent_id)
    if blocked:
        return blocked
    viewer = request.state.agent or {}
    if int(viewer.get("id") or 0) != agent_id and not field.sees_everyone(viewer):
        return _forbidden("You can only share your own location.")
    coords = field.parse_coords(lat, lng)
    if coords is None:
        coords = parse_geo(paste)
    if coords is None:
        return _redirect(
            f"/field/{agent_id}",
            flash="Could not read a location. Allow GPS, or paste coordinates / a Google Maps link.",
            level="err",
        )
    with transaction() as conn:
        field.record_location(
            conn,
            agent_id=agent_id,
            lat=coords[0],
            lng=coords[1],
            accuracy=field.parse_accuracy(accuracy),
            source="manual",
        )
    return _redirect(f"/field/{agent_id}", flash="Location saved.")


def _settlement_range(request: Request) -> tuple[str, str]:
    params = request.query_params
    day = (params.get("day") or "").strip()
    raw_from = (params.get("from") or "").strip()
    raw_to = (params.get("to") or "").strip()
    if day and not raw_from and not raw_to:
        raw_from = raw_to = day
    today_s = today().strftime("%Y-%m-%d")
    month_start = today().replace(day=1).strftime("%Y-%m-%d")
    start = parse_date(raw_from)
    end = parse_date(raw_to)
    day_from = start.strftime("%Y-%m-%d") if start else month_start
    day_to = end.strftime("%Y-%m-%d") if end else today_s
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    return day_from, day_to


def _settlement_agent_id(request: Request, raw: str = "") -> int | None:
    viewer = request.state.agent or {}
    if not auth.sees_all_settlements(viewer):
        return int(viewer["id"]) if viewer.get("id") else None
    text = (raw or "").strip()
    if text.isdigit():
        return int(text)
    if viewer.get("id"):
        return int(viewer["id"])
    return None


def _proof_save_flash(*, created: bool, attached: int, skipped: int, removed: int = 0) -> str:
    parts = ["Settlement report saved." if created else "Settlement report updated."]
    if attached:
        parts.append(f"{attached} proof photo{'s' if attached != 1 else ''} attached.")
    if removed:
        parts.append(f"Removed {removed} photo{'s' if removed != 1 else ''}.")
    if skipped:
        parts.append(f"{skipped} file(s) skipped (use a JPG/PNG/WebP under 8 MB).")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Agent settlements
# --------------------------------------------------------------------------- #

@router.get("/settlements", response_class=HTMLResponse)
async def settlements_list(request: Request):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent
    everyone = auth.sees_all_settlements(viewer)
    day_from, day_to = _settlement_range(request)
    selected = None
    raw_agent = (request.query_params.get("agent_id") or "").strip()
    if raw_agent.isdigit():
        selected = int(raw_agent)
        if not field.can_see_agent(viewer, selected):
            return _forbidden("You can only see your own settlements.")
    only = selected if everyone else int(viewer["id"])
    if not everyone:
        selected = only
    with connection() as conn:
        summary = settlements.live_summaries(
            conn, day_from=day_from, day_to=day_to, only_agent_id=only
        )
        handovers = settlements.handover_statement(
            conn, day_from=day_from, day_to=day_to, agent_id=only
        )
        agents = settlements.active_agents(conn) if everyone else []
    return _render(
        request,
        "settlements.html",
        rows=summary["rows"],
        office=summary["office"],
        handovers=handovers,
        day_from=day_from,
        day_to=day_to,
        everyone=everyone,
        agents=agents,
        selected_agent_id=selected,
        payment_mode_labels=settlements.payment_mode_labels(),
        viewer_is_owner=everyone,
    )


@router.get("/settlements/new", response_class=HTMLResponse)
async def settlements_new(request: Request):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent
    everyone = auth.sees_all_settlements(viewer)
    today_s = today().strftime("%Y-%m-%d")
    period_from, period_to = _settlement_range(request)
    selected = _settlement_agent_id(request, request.query_params.get("agent_id") or "")
    if selected and not field.can_see_agent(viewer, selected):
        selected = int(viewer["id"])
    agent_id = selected or int(viewer["id"])
    with connection() as conn:
        agents = settlements.active_agents(conn) if everyone else []
        collection = settlements.collection_for_agent(
            conn, agent_id=agent_id, day_from=period_from, day_to=period_to
        )
        cash_recipients = settlements.cash_recipient_options(conn, settings.operator)
    return _render(
        request,
        "settlement_form.html",
        report=None,
        collection=collection,
        everyone=everyone,
        agents=agents,
        selected_agent_id=agent_id,
        self_id=int(viewer["id"]),
        period_from=period_from,
        period_to=period_to,
        settled_on=today_s,
        cash_recipients=cash_recipients,
        expense_kinds=settlements.EXPENSE_KINDS,
        proof_kinds=settlements.proof_kinds(),
        proof_labels=dict(settlements.proof_kinds()),
        payment_mode_labels=settlements.payment_mode_labels(),
        form_action="/settlements/new",
    )


@router.post("/settlements/new")
async def settlements_create(request: Request):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent or {}
    form = await request.form()
    try:
        payload = settlements.parse_form(form)
    except settlements.SettlementError as exc:
        return _redirect("/settlements/new", flash=str(exc), level="err")
    agent_id = _settlement_agent_id(request, str(form.get("agent_id") or ""))
    if not agent_id or not field.can_see_agent(viewer, agent_id):
        return _forbidden("You can only submit your own settlement.")
    actor = viewer.get("name") or ""
    uploads, skipped = await settlements.read_proof_uploads(form)
    try:
        with transaction() as conn:
            sid = settlements.save(conn, agent_id=agent_id, payload=payload, actor=actor)
            attached = settlements.save_proofs(conn, sid, uploads, actor=actor)
            log_activity(
                conn,
                "agent_settlement",
                f"{actor} submitted settlement #{sid} "
                f"({payload['period_from']} to {payload['period_to']})",
                actor=actor,
                meta_json=json.dumps({"settlement_id": sid, "agent_id": agent_id, "proofs": attached}),
            )
    except settlements.SettlementError as exc:
        return _redirect("/settlements/new", flash=str(exc), level="err")
    return _redirect(
        f"/settlements/{sid}",
        flash=_proof_save_flash(created=True, attached=attached, skipped=skipped),
    )


@router.get("/settlements/search-customers")
async def settlements_search_customers(request: Request):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    q = (request.query_params.get("q") or "").strip()
    with connection() as conn:
        rows = settlements.search_cash_customers(conn, q)
    return JSONResponse({"customers": rows})


@router.get("/settlements/{settlement_id}", response_class=HTMLResponse)
async def settlements_detail(request: Request, settlement_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
    if report is None:
        return _redirect("/settlements", flash="Settlement not found.", level="err")
    if not field.can_see_agent(request.state.agent, int(report["agent_id"])):
        return _forbidden("You can only see your own settlements.")
    return _render(
        request,
        "settlement_detail.html",
        report=report,
        can_edit=settlements.can_edit(request.state.agent, report),
        expense_labels=dict(settlements.EXPENSE_KINDS),
        proof_labels=dict(settlements.proof_kinds()),
        proof_kinds=settlements.proof_kinds(),
        payment_mode_labels=settlements.payment_mode_labels(),
        viewer_is_owner=auth.sees_all_settlements(request.state.agent),
    )


@router.get("/settlements/{settlement_id}/edit", response_class=HTMLResponse)
async def settlements_edit_form(request: Request, settlement_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
        agents = settlements.active_agents(conn) if auth.sees_all_settlements(viewer) else []
    if report is None:
        return _redirect("/settlements", flash="Settlement not found.", level="err")
    if not settlements.can_edit(viewer, report):
        return _forbidden("You cannot edit this settlement.")
    period_from = report["period_from"]
    period_to = report["period_to"]
    cash_recipients: list[str] = []
    if (request.query_params.get("from") or "").strip() or (request.query_params.get("to") or "").strip():
        period_from, period_to = _settlement_range(request)
        with connection() as conn:
            report["collection"] = settlements.collection_for_agent(
                conn,
                agent_id=int(report["agent_id"]),
                day_from=period_from,
                day_to=period_to,
            )
            cash_recipients = settlements.cash_recipient_options(conn, settings.operator)
    else:
        with connection() as conn:
            cash_recipients = settlements.cash_recipient_options(conn, settings.operator)
    return _render(
        request,
        "settlement_form.html",
        report=report,
        collection=report.get("collection"),
        everyone=auth.sees_all_settlements(viewer),
        agents=agents,
        selected_agent_id=int(report["agent_id"]),
        self_id=int(viewer["id"]),
        period_from=period_from,
        period_to=period_to,
        settled_on=report["settled_on"],
        cash_recipients=cash_recipients,
        expense_kinds=settlements.EXPENSE_KINDS,
        proof_kinds=settlements.proof_kinds(),
        proof_labels=dict(settlements.proof_kinds()),
        payment_mode_labels=settlements.payment_mode_labels(),
        form_action=f"/settlements/{settlement_id}/edit",
    )


@router.post("/settlements/{settlement_id}/edit")
async def settlements_edit_save(request: Request, settlement_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent or {}
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
    if report is None:
        return _redirect("/settlements", flash="Settlement not found.", level="err")
    if not settlements.can_edit(viewer, report):
        return _forbidden("You cannot edit this settlement.")
    form = await request.form()
    try:
        payload = settlements.parse_form(form)
    except settlements.SettlementError as exc:
        return _redirect(f"/settlements/{settlement_id}/edit", flash=str(exc), level="err")
    agent_id = _settlement_agent_id(request, str(form.get("agent_id") or "")) or int(report["agent_id"])
    if not field.can_see_agent(viewer, agent_id):
        agent_id = int(report["agent_id"])
    actor = viewer.get("name") or ""
    uploads, skipped = await settlements.read_proof_uploads(form)
    remove_ids = settlements.parse_remove_proof_ids(form)
    try:
        with transaction() as conn:
            settlements.save(
                conn,
                agent_id=agent_id,
                payload=payload,
                actor=actor,
                settlement_id=settlement_id,
            )
            removed = settlements.delete_proofs(conn, settlement_id, remove_ids)
            attached = settlements.save_proofs(conn, settlement_id, uploads, actor=actor)
            log_activity(
                conn,
                "agent_settlement",
                f"{actor} updated settlement #{settlement_id}",
                actor=actor,
                meta_json=json.dumps(
                    {
                        "settlement_id": settlement_id,
                        "agent_id": agent_id,
                        "proofs": attached,
                        "removed_proofs": removed,
                    }
                ),
            )
    except settlements.SettlementError as exc:
        return _redirect(f"/settlements/{settlement_id}/edit", flash=str(exc), level="err")
    return _redirect(
        f"/settlements/{settlement_id}",
        flash=_proof_save_flash(
            created=False, attached=attached, skipped=skipped, removed=removed
        ),
    )


@router.post("/settlements/{settlement_id}/delete")
async def settlements_delete(request: Request, settlement_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent or {}
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
    if report is None:
        return _redirect("/settlements", flash="Settlement not found.", level="err")
    if not settlements.can_edit(viewer, report):
        return _forbidden("You cannot delete this settlement.")
    actor = viewer.get("name") or ""
    with transaction() as conn:
        settlements.delete(conn, settlement_id)
        log_activity(
            conn,
            "agent_settlement",
            f"{actor} deleted settlement #{settlement_id} for {report.get('agent_name')}",
            actor=actor,
            meta_json=json.dumps({"settlement_id": settlement_id, "agent_id": report["agent_id"]}),
        )
    form = await request.form()
    back = str(form.get("next") or "")
    if not back.startswith("/settlements") or back.startswith(f"/settlements/{settlement_id}"):
        back = "/settlements"
    return _redirect(back, flash=f"Settlement #{settlement_id} by {report.get('agent_name')} deleted.")


@router.get("/settlements/{settlement_id}/proofs/{proof_id}")
async def settlements_proof_file(request: Request, settlement_id: int, proof_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
        proof = settlements.get_proof(conn, settlement_id, proof_id) if report else None
    if report is None or proof is None:
        return _redirect("/settlements", flash="Proof photo not found.", level="err")
    if not field.can_see_agent(request.state.agent, int(report["agent_id"])):
        return _forbidden("You can only see your own settlements.")
    path = settlements.proof_file_path(proof["stored_name"])
    if not path.is_file():
        return _redirect(
            f"/settlements/{settlement_id}",
            flash="That proof photo is missing on disk.",
            level="err",
        )
    return FileResponse(
        path,
        media_type=proof.get("content_type") or "image/jpeg",
        filename=proof.get("original_name") or path.name,
        content_disposition_type="inline",
    )


@router.post("/settlements/{settlement_id}/proofs")
async def settlements_add_proofs(request: Request, settlement_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent or {}
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
    if report is None:
        return _redirect("/settlements", flash="Settlement not found.", level="err")
    if not settlements.can_edit(viewer, report):
        return _forbidden("You cannot add proof to this settlement.")
    form = await request.form()
    uploads, skipped = await settlements.read_proof_uploads(form)
    actor = viewer.get("name") or ""
    with transaction() as conn:
        attached = settlements.save_proofs(conn, settlement_id, uploads, actor=actor)
    if not attached and skipped:
        return _redirect(
            f"/settlements/{settlement_id}",
            flash="Could not attach those files. Use a JPG, PNG or WebP under 8 MB.",
            level="err",
        )
    if not attached:
        return _redirect(f"/settlements/{settlement_id}", flash="Choose a photo to attach.", level="err")
    return _redirect(
        f"/settlements/{settlement_id}",
        flash=_proof_save_flash(created=False, attached=attached, skipped=skipped).replace(
            "Settlement report updated. ", ""
        ),
    )


@router.post("/settlements/{settlement_id}/proofs/{proof_id}/delete")
async def settlements_delete_proof(request: Request, settlement_id: int, proof_id: int):
    blocked = _forbid_field(request)
    if blocked:
        return blocked
    viewer = request.state.agent or {}
    with connection() as conn:
        report = settlements.get(conn, settlement_id)
    if report is None:
        return _redirect("/settlements", flash="Settlement not found.", level="err")
    if not settlements.can_edit(viewer, report):
        return _forbidden("You cannot remove proof from this settlement.")
    with transaction() as conn:
        ok = settlements.delete_proof(conn, settlement_id, proof_id)
    if not ok:
        return _redirect(f"/settlements/{settlement_id}", flash="Proof photo not found.", level="err")
    return _redirect(f"/settlements/{settlement_id}", flash="Proof photo removed.")


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #

def _inventory_scope(request: Request) -> str | None:
    return auth.agent_provider_scope(request.state.agent)


@router.get("/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    if not auth.can(request.state.agent, "inventory"):
        return _forbidden()
    scope = _inventory_scope(request)
    with connection() as conn:
        items = inventory.list_stock(conn, scope=scope)
        usage = inventory.recent_usage(conn, limit=25, scope=scope)
        receipts = inventory.recent_receipts(conn, limit=12) if inventory.can_receive(request.state.agent) else []
    v2 = request.cookies.get("vk_ui") == "v2"
    return _render(
        request,
        "v2/inventory.html" if v2 else "inventory.html",
        items=items,
        groups=inventory.grouped_stock(items),
        usage=usage,
        receipts=receipts,
        kinds=inventory.USAGE_KINDS,
        can_receive=inventory.can_receive(request.state.agent),
        today=today().strftime("%Y-%m-%d"),
    )


@router.get("/inventory/search-customers")
async def inventory_search_customers(request: Request):
    if not auth.can(request.state.agent, "inventory"):
        return JSONResponse({"customers": []}, status_code=403)
    q = (request.query_params.get("q") or "").strip()
    with connection() as conn:
        rows = settlements.search_cash_customers(conn, q)
    return JSONResponse({"customers": rows})


@router.post("/inventory/receive")
async def inventory_receive(
    request: Request,
    item_id: int = Form(...),
    qty: str = Form(...),
    amount: str = Form(""),
    received_on: str = Form(""),
    note: str = Form(""),
):
    if not inventory.can_receive(request.state.agent):
        return _forbidden("Only admin can add stock from an order.")
    actor = (request.state.agent or {}).get("name")
    try:
        with transaction() as conn:
            result = inventory.receive(
                conn,
                item_id=item_id,
                qty_raw=qty,
                amount_raw=amount,
                received_on=received_on,
                note=note,
                actor=actor,
            )
    except inventory.InventoryError as exc:
        return _redirect("/inventory", flash=str(exc), level="err")
    return _redirect(
        "/inventory",
        flash=f"Added {inventory.qty_label(result['qty'], result['item']['unit'])} of {result['item']['name']}.",
    )


@router.post("/inventory/receive-file")
async def inventory_receive_file(request: Request, file: UploadFile = File(...)):
    if not inventory.can_receive(request.state.agent):
        return _forbidden("Only admin can add stock from an order.")
    raw = await file.read()
    actor = (request.state.agent or {}).get("name")
    try:
        with transaction() as conn:
            rows = inventory.parse_receive_file(conn, raw)
            summary = inventory.apply_receive_rows(conn, rows, actor=actor)
    except inventory.InventoryError as exc:
        return _redirect("/inventory", flash=str(exc), level="err")
    extra = ""
    if summary["errors"]:
        extra = " " + "; ".join(summary["errors"][:3])
    level = "ok" if summary["ok"] else "err"
    return _redirect(
        "/inventory",
        flash=f"Order file: {summary['ok']} line(s) added, {summary['skipped']} skipped.{extra}",
        level=level,
    )


@router.post("/inventory/use")
async def inventory_use(
    request: Request,
    item_id: int = Form(...),
    qty: str = Form(...),
    kind: str = Form(...),
    customer_id: str = Form(""),
    note: str = Form(""),
):
    if not auth.can(request.state.agent, "inventory"):
        return _forbidden()
    cid = int(customer_id) if str(customer_id).strip().isdigit() else None
    try:
        with transaction() as conn:
            item = inventory.get_item(conn, item_id)
            if item is None:
                raise inventory.InventoryError("That item is not in the list.")
            if not inventory.item_visible(item, _inventory_scope(request)):
                raise inventory.InventoryError("That item is not in your stock list.")
            result = inventory.use_item(
                conn,
                item_id=item_id,
                qty_raw=qty,
                kind=kind,
                customer_id=cid,
                note=note,
                agent=request.state.agent,
            )
    except inventory.InventoryError as exc:
        return _redirect("/inventory", flash=str(exc), level="err")
    return _redirect(
        "/inventory",
        flash=f"Logged {inventory.qty_label(result['qty'], result['item']['unit'])} {result['item']['name']} · {result['who']}.",
    )


@router.get("/inventory/items/{item_id}", response_class=HTMLResponse)
async def inventory_item_page(request: Request, item_id: int):
    if not auth.can(request.state.agent, "inventory"):
        return _forbidden()
    with connection() as conn:
        item = inventory.get_item(conn, item_id)
        if item is None or not inventory.item_visible(item, _inventory_scope(request)):
            return _redirect("/inventory", flash="That item is not in your list.", level="err")
        receipts = (
            inventory.recent_receipts(conn, limit=40, item_id=item_id)
            if inventory.can_receive(request.state.agent)
            else []
        )
        usage = inventory.recent_usage(conn, limit=60, item_id=item_id)
    v2 = request.cookies.get("vk_ui") == "v2"
    return _render(
        request,
        "v2/inventory_item.html" if v2 else "inventory_item.html",
        item=item,
        receipts=receipts,
        usage=usage,
        kinds=inventory.USAGE_KINDS,
        can_receive=inventory.can_receive(request.state.agent),
    )


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

@router.get("/customers")
async def customers_list(request: Request):
    params = request.query_params
    q = params.get("q", "")
    provider = auth.scoped_provider(request.state.agent, params.get("provider", ""))
    status = params.get("status", "")
    area = params.get("area", "")
    view = params.get("view", "")
    # Name/login search must find the same people as the owner, not stay on Expired.
    if (q or "").strip() and view in ("expired", "expiring"):
        view = ""
    railtel_account = params.get("railtel_account", "")
    sort, late_days = _parse_late_sort(params)
    export = wants_csv(request)
    with connection() as conn:
        result = repo.search_customers(
            conn,
            query=q,
            provider=provider,
            status=status,
            area=area,
            view=view,
            railtel_account=railtel_account,
            page=int(params.get("page", 1) or 1),
            sort=sort,
            late_days=late_days,
            export_all=export,
            agent_scope=auth.agent_provider_scope(request.state.agent) or "",
            hide_owner=bool(auth.collector_id_for(request.state.agent)),
        )
        areas = repo.list_area_options(conn)
    if export:
        return list_exports.customers_csv(result["rows"])
    list_params = {
        "q": q,
        "provider": provider,
        "status": status,
        "area": area,
        "view": view,
        "railtel_account": railtel_account,
        "sort": sort,
        "late_days": late_days or "",
    }
    list_qs = _list_qs(**list_params)
    filter_q = _list_qs(
        q=q, provider=provider, status=status, area=area, railtel_account=railtel_account, sort=sort,
        late_days=late_days or "",
    )
    dealer_filter_q = _list_qs(
        q=q, provider=provider, status=status, area=area, sort=sort, late_days=late_days or "",
    )
    return _render(
        request,
        "customers.html",
        result=result,
        q=q,
        provider=provider,
        status=status,
        area=area,
        view=view,
        railtel_account=railtel_account,
        sort=sort,
        late_days=late_days,
        providers=PROVIDERS,
        areas=areas,
        none_area=repo.NONE_AREA,
        filter_q=filter_q,
        dealer_filter_q=dealer_filter_q,
        list_qs=list_qs,
        export_href=export_url("/customers", **list_params),
        fixed_provider=auth.agent_provider_scope(request.state.agent),
    )


@router.get("/stbs")
async def hathway_stbs(request: Request):
    params = request.query_params
    q = params.get("q", "")
    view = params.get("view", "running")
    export = wants_csv(request)
    with connection() as conn:
        mapping = repo.hathway_mapping_stats(conn)
        result = repo.list_hathway_stbs(
            conn,
            query=q,
            view=view,
            page=int(params.get("page", 1) or 1),
            export_all=export,
        )
    if export:
        return list_exports.stbs_csv(result["rows"])
    return _render(
        request,
        "stbs.html",
        result=result,
        mapping=mapping,
        q=q,
        view=result["view"],
        providers_tab="hathway",
        export_href=export_url("/stbs", q=q, view=result["view"]),
    )


@router.get("/stbs/expiry-report")
async def hathway_expiry_report(request: Request):
    if not auth.can(request.state.agent, "customers_view"):
        return _forbidden()
    params = request.query_params
    date_from = (params.get("from") or "").strip()
    date_to = (params.get("to") or "").strip()
    on_day = (params.get("on") or "").strip()
    if not date_from and not date_to and not on_day:
        date_from = add_days(today(), -2).strftime("%Y-%m-%d")
        date_to = add_days(today(), 14).strftime("%Y-%m-%d")
    day_rows = []
    with connection() as conn:
        buckets = hathway_expiry_sync.expiry_report_buckets(
            conn, date_from=date_from, date_to=date_to
        )
        if on_day:
            day_rows = hathway_expiry_sync.expiry_report_day(conn, on_day)
    return _render(
        request,
        "hathway_expiry_report.html",
        buckets=buckets,
        day_rows=day_rows,
        date_from=date_from,
        date_to=date_to,
        on_day=on_day,
        providers_tab="hathway",
    )


@router.get("/iptv")
async def iptv_page(request: Request):
    params = request.query_params
    q = params.get("q", "")
    view = params.get("view", "all")
    sort, late_days = _parse_late_sort(params)
    export = wants_csv(request)
    with connection() as conn:
        stats = repo.iptv_stats(conn)
        result = repo.list_iptv_subscriptions(
            conn,
            query=q,
            view=view,
            page=int(params.get("page", 1) or 1),
            sort=sort,
            late_days=late_days,
            export_all=export,
        )
        packages = repo.list_packages(conn, provider="iptv", only_active=True)
    if export:
        return list_exports.prepaid_csv(result["rows"], filename="iptv.csv", provider="iptv")
    list_params = {"q": q, "view": result["view"], "sort": sort, "late_days": late_days or ""}
    return _render(
        request,
        "iptv.html",
        result=result,
        stats=stats,
        q=q,
        view=result["view"],
        sort=sort,
        late_days=late_days,
        list_qs=_list_qs(**list_params),
        packages=packages,
        providers_tab="iptv",
        provider_actions=PROVIDER_ACTIONS.get("iptv", ()),
        export_href=export_url("/iptv", **list_params),
    )


@router.post("/iptv/subscribe")
async def iptv_subscribe(
    request: Request,
    package_name: str = Form(...),
    phone: str = Form(...),
    name: str = Form(...),
    state: str = Form("Karnataka"),
    city: str = Form("Tiptur"),
    address: str = Form(""),
    pincode: str = Form("572201"),
    expiry_date: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden("You cannot add IPTV subscriptions.")
    expiry_norm, expiry_err = normalise_expiry_input(expiry_date)
    if expiry_err:
        return _redirect("/iptv", flash=expiry_err, level="err")
    job_id = None
    try:
        with transaction() as conn:
            result = iptv_plans.subscribe_iptv(
                conn,
                name=name,
                phone=phone,
                pack=package_name,
                expiry=expiry_norm,
                address=address,
                city=city,
                state=state,
                pincode=pincode,
            )
            log_activity(
                conn,
                "iptv_subscribed",
                f"ANT IPTV {package_name} for {phone.strip()}",
                customer_id=result["customer_id"],
                connection_id=result["connection_id"],
            )
            if not result["updated"] and auth.can(request.state.agent, "portal_actions"):
                job_id = job_queue.enqueue_job(
                    conn,
                    connection_id=result["connection_id"],
                    action="subscribe",
                    requested_by=_job_by(request),
                    needs_confirmation=False,
                )
    except ValueError as exc:
        return _redirect("/iptv", flash=str(exc), level="err")
    except Exception as exc:
        return _redirect("/iptv", flash=f"Could not add subscription: {exc}", level="err")
    verb = "updated" if result["updated"] else "added"
    extra = ""
    if job_id:
        extra = " " + _job_flash("subscribe", job_id, "iptv")
    return _redirect(
        f"/customers/{result['customer_id']}",
        flash=f"IPTV subscription {verb}.{extra}",
    )


@router.get("/ott")
async def ott_page(request: Request):
    params = request.query_params
    q = params.get("q", "")
    view = params.get("view", "all")
    sort, late_days = _parse_late_sort(params)
    export = wants_csv(request)
    with connection() as conn:
        stats = repo.ott_stats(conn)
        result = repo.list_ott_subscriptions(
            conn,
            query=q,
            view=view,
            page=int(params.get("page", 1) or 1),
            sort=sort,
            late_days=late_days,
            export_all=export,
        )
        packages = repo.list_packages(conn, provider="ott", only_active=True)
    if export:
        return list_exports.prepaid_csv(result["rows"], filename="ott.csv", provider="ott")
    list_params = {"q": q, "view": result["view"], "sort": sort, "late_days": late_days or ""}
    return _render(
        request,
        "ott.html",
        result=result,
        stats=stats,
        q=q,
        view=result["view"],
        sort=sort,
        late_days=late_days,
        list_qs=_list_qs(**list_params),
        packages=packages,
        providers_tab="ott",
        provider_actions=PROVIDER_ACTIONS.get("ott", ()),
        export_href=export_url("/ott", **list_params),
    )


@router.post("/ott/sync")
async def ott_sync(request: Request):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot sync the SmartPlay portal.")
    with transaction() as conn:
        job_id = job_queue.enqueue_provider_job(
            conn, provider="ott", action="sync", requested_by=_job_by(request)
        )
        busy_note = _exclusive_busy_note(conn, "sync", job_id)
    return _redirect(
        "/ott",
        flash=_job_flash("sync", job_id, "ott", busy_note=busy_note)
        + " The page will fill once the worker finishes (one SmartPlay login).",
    )


@router.post("/ott/subscribe")
async def ott_subscribe(
    request: Request,
    package_name: str = Form(...),
    phone: str = Form(...),
    name: str = Form(...),
    state: str = Form("Karnataka"),
    city: str = Form("Tiptur"),
    address: str = Form(""),
    pincode: str = Form("572201"),
    expiry_date: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden("You cannot add OTT subscriptions.")
    expiry_norm, expiry_err = normalise_expiry_input(expiry_date)
    if expiry_err:
        return _redirect("/ott", flash=expiry_err, level="err")
    try:
        with transaction() as conn:
            result = ott_plans.subscribe_ott(
                conn,
                name=name,
                phone=phone,
                pack=package_name,
                expiry=expiry_norm,
                address=address,
                city=city,
                state=state,
                pincode=pincode,
            )
            log_activity(
                conn,
                "ott_subscribed",
                f"SmartPlay OTT {package_name} for {phone.strip()}",
                customer_id=result["customer_id"],
                connection_id=result["connection_id"],
            )
    except ValueError as exc:
        return _redirect("/ott", flash=str(exc), level="err")
    except Exception as exc:
        return _redirect("/ott", flash=f"Could not add subscription: {exc}", level="err")
    verb = "updated" if result["updated"] else "added"
    return _redirect(
        f"/customers/{result['customer_id']}",
        flash=f"OTT subscription {verb}.",
    )


@router.get("/customers/new", response_class=HTMLResponse)
async def customer_new_form(request: Request):
    with connection() as conn:
        packages = repo.list_packages(conn, only_active=True)
        areas = repo.list_area_options(conn)
    return _render(request, "customer_form.html", customer=None, packages=packages,
                   providers=PROVIDERS, areas=areas)


@router.post("/customers/new")
async def customer_create(
    request: Request,
    name: str = Form(...),
    code: str = Form(""),
    phone: str = Form(""),
    alt_phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    area: str = Form(""),
    sub_area: str = Form(""),
    pincode: str = Form(""),
    notes: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    stamp = now_iso()
    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO customers(code, name, phone, alt_phone, email, address, area, sub_area, "
            "pincode, status, notes, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                code.strip() or None,
                name.strip(),
                phone.strip(),
                alt_phone.strip(),
                email.strip(),
                address.strip(),
                area.strip() or "Tiptur",
                sub_area.strip(),
                pincode.strip(),
                notes.strip(),
                stamp,
                stamp,
            ),
        )
        customer_id = int(cursor.lastrowid)
        log_activity(conn, "customer_created", f"Customer '{name.strip()}' added",
                     customer_id=customer_id)
    return _redirect(f"/customers/{customer_id}", flash="Customer added. Now add a connection.")


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
async def customer_detail(request: Request, customer_id: int):
    bix_kind = (request.query_params.get("bix") or "").strip()
    if bix_kind not in {"payment", "bill", "adjustment", "other"}:
        bix_kind = ""
    with connection() as conn:
        customer = repo.get_customer(conn, customer_id)
        if customer is None:
            return _render(request, "not_found.html", what="Customer")
        if not auth.customer_accessible(conn, request.state.agent, customer):
            return _forbidden("This customer is outside your access.")
        connections = auth.scoped_connections(
            request.state.agent, repo.customer_connections(conn, customer_id)
        )
        ledger = billing.customer_ledger(conn, customer_id)
        bills = repo.customer_bills(conn, customer_id)
        payments = repo.customer_payments(conn, customer_id)
        jobs = _with_job_creator(repo.customer_jobs(conn, customer_id))
        packages = repo.list_packages(conn, only_active=True)
        statement = repo.customer_statement(conn, customer_id)
        complaints = repo.list_complaints(conn, customer_id=customer_id, limit=20)
        agents = repo.list_agents(conn)
        history = bix_history.customer_history(conn, customer_id, kind=bix_kind)
        followups = repo.customer_collect_later(conn, customer_id)
        railtel_invoices = repo.customer_railtel_invoices(conn, customer_id)
        collect_quotes = {}
        for cn in connections:
            pkg = {
                "price_paise": cn["package_price_paise"],
                "gst_percentage": cn["package_gst"] if "package_gst" in cn.keys() else 0,
            }
            quote = billing.quoted_charge(cn, pkg)
            collect_quotes[int(cn["id"])] = {
                "provider": cn["provider"],
                "total": quote["total_paise"],
                "base": quote["base_paise"],
                "gst": quote["gst_paise"],
                "rate": quote["gst_percentage"],
                "exclusive": quote["exclusive"],
            }
        usual_collect_paise = billing.customer_collect_paise(
            conn, customer_id, connections=connections,
        )
        default_collect_paise = billing.default_collect_amount_paise(
            conn, customer_id, connections=connections,
        )
        connection_charges = billing.connection_collect_lines(
            conn, customer_id, connections=connections,
        )
        connections_collect_total = sum(int(r["amount_paise"]) for r in connection_charges)
        custom_plan = billing.customer_custom_plan(conn, customer_id)
        plan_cover = billing.customer_cover(
            conn, customer_id, connections=connections, custom_plan=custom_plan
        )
        paid_through = billing.soonest_paid_through(connections)
        areas = repo.list_area_options(conn)
        pay_intents = public_pay.customer_pay_intents(conn, customer_id)
        pay_intents_open = [
            pi for pi in pay_intents if (pi["status"] or "") in {"pending", "customer_marked"}
        ]

    # One datalist per provider, rendered once and shared by every form on the page.
    plan_names: dict[str, list[str]] = {p: [] for p in PROVIDERS}
    for row in packages:
        plan_names.setdefault(row["provider"], []).append(row["name"])

    # Portal buttons are hidden for ids the provider could never match.
    id_problems = {
        int(row["id"]): id_problem(row["provider"], row["upstream_id"] or "")
        for row in connections
    }

    return _render(
        request,
        "customer_detail.html",
        customer=customer,
        connections=connections,
        ledger=ledger,
        bills=bills,
        payments=payments,
        jobs=jobs,
        statement=list(reversed(statement)),
        plan_names=plan_names,
        providers=PROVIDERS,
        provider_actions=PROVIDER_ACTIONS,
        destructive_actions=DESTRUCTIVE_ACTIONS,
        id_problems=id_problems,
        payment_modes=PAYMENT_MODES,
        connection_statuses=CONNECTION_STATUSES,
        complaints=complaints,
        agents=agents,
        bix_history=history,
        bix_kind=bix_kind,
        followups=followups,
        railtel_invoices=railtel_invoices,
        public_base_url=settings.public_base_url or str(request.base_url).rstrip("/"),
        collect_quotes=collect_quotes,
        usual_collect_paise=usual_collect_paise,
        default_collect_paise=default_collect_paise,
        connection_charges=connection_charges,
        connections_collect_total=connections_collect_total,
        custom_plan=custom_plan,
        plan_cover=plan_cover,
        paid_through=paid_through,
        plan_bundles=billing.PLAN_BUNDLES,
        plan_terms=PLAN_TERMS,
        free_reasons=billing.FREE_REASONS,
        owner_reasons=billing.OWNER_REASONS,
        maps_nav=maps_nav_url(customer["lat"], customer["lng"])
        if customer["lat"] is not None and customer["lng"] is not None else "",
        maps_view=maps_view_url(customer["lat"], customer["lng"])
        if customer["lat"] is not None and customer["lng"] is not None else "",
        areas=areas,
        pay_intents=pay_intents,
        pay_intents_open=pay_intents_open,
    )


@router.post("/customers/{customer_id}/whatsapp")
async def customer_send_whatsapp(
    request: Request,
    customer_id: int,
    kind: str = Form(...),
    connection_id: str = Form(""),
):
    if not auth.can(request.state.agent, "customer_whatsapp"):
        return _forbidden("You cannot send WhatsApp to customers.")
    from ..messaging import (
        expired_whatsapp_url,
        payment_received_whatsapp_url,
        renewed_whatsapp_url,
    )

    conn_id = int(connection_id) if connection_id.strip().isdigit() else None
    with connection() as conn:
        cust = repo.get_customer(conn, customer_id)
        if cust is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        provider = None
        if conn_id:
            cn = conn.execute(
                "SELECT provider FROM connections WHERE id = ? AND customer_id = ?",
                (conn_id, customer_id),
            ).fetchone()
            provider = cn["provider"] if cn else None
    csv = None if provider else (cust["providers"] or None)
    kind = (kind or "").strip().lower()
    if kind == "payment":
        wa = payment_received_whatsapp_url(cust["name"], cust["phone"], provider, csv)
    elif kind == "renewed":
        wa = renewed_whatsapp_url(cust["name"], cust["phone"], provider, csv)
    elif kind == "expired":
        wa = expired_whatsapp_url(cust["name"], cust["phone"], provider, csv)
    else:
        return _redirect(
            f"/customers/{customer_id}", flash="Unknown WhatsApp message.", level="err"
        )
    if not wa:
        return _redirect(
            f"/customers/{customer_id}",
            flash="Add a phone number on this customer first.",
            level="err",
        )
    return RedirectResponse(wa, status_code=303)


@router.post("/customers/{customer_id}/whatsapp/send")
async def customer_send_whatsapp_now(
    request: Request,
    customer_id: int,
    kind: str = Form(...),
    connection_id: str = Form(""),
    next: str = Form(""),
):
    """Send a ready-made message from the office WhatsApp — no editing on the phone."""
    if not auth.can(request.state.agent, "customer_whatsapp"):
        return _forbidden("You cannot send WhatsApp to customers.")
    from ..whatsapp_notify import SEND_NOW_KINDS, queue_customer_message

    dest = _redirect_target(next, f"/customers/{customer_id}")
    conn_id = int(connection_id) if connection_id.strip().isdigit() else None
    provider = None
    with connection() as conn:
        cust = repo.get_customer(conn, customer_id)
        if cust is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        if not auth.customer_accessible(conn, request.state.agent, cust):
            return _forbidden("This customer is outside your access.")
        if conn_id:
            cn = conn.execute(
                "SELECT provider FROM connections WHERE id = ? AND customer_id = ?",
                (conn_id, customer_id),
            ).fetchone()
            provider = cn["provider"] if cn else None
            if cn is None:
                conn_id = None
    agent = request.state.agent or {}
    reason = queue_customer_message(
        customer_id=customer_id,
        kind=kind,
        connection_id=conn_id,
        provider=provider,
        agent_id=int(agent["id"]) if agent.get("id") else None,
    )
    label = SEND_NOW_KINDS.get((kind or "").strip().lower(), "WhatsApp")
    if reason:
        return _redirect(dest, flash=f"{label} not sent: {reason}", level="err")
    return _redirect(
        dest,
        flash=f"{label} is being sent to {cust['name']} from the office WhatsApp.",
    )


@router.post("/customers/{customer_id}/area")
async def customer_set_area(
    request: Request,
    customer_id: int,
    sub_area: str = Form(""),
    sub_area_new: str = Form(""),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    value = (sub_area_new or sub_area or "").strip()
    if not value:
        return _redirect(
            _redirect_target(next, f"/customers/{customer_id}"),
            flash="Pick an area or type a new one (e.g. CN Road, BH Road).",
            level="err",
        )
    with transaction() as conn:
        if repo.get_customer(conn, customer_id) is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        conn.execute(
            "UPDATE customers SET sub_area = ?, "
            "area = COALESCE(NULLIF(trim(area), ''), 'Tiptur'), updated_at = ? WHERE id = ?",
            (value, now_iso(), customer_id),
        )
        log_activity(conn, "area_set", f"Area set to {value}", customer_id=customer_id)
    return _redirect(
        _redirect_target(next, f"/customers/{customer_id}"),
        flash=f"Area set to {value}.",
    )


@router.post("/customers/{customer_id}/edit")
async def customer_edit(
    request: Request,
    customer_id: int,
    name: str = Form(...),
    code: str = Form(""),
    phone: str = Form(""),
    alt_phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    area: str = Form(""),
    sub_area: str = Form(""),
    pincode: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
    collect_amount: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    with transaction() as conn:
        conn.execute(
            "UPDATE customers SET code = ?, name = ?, phone = ?, alt_phone = ?, email = ?, "
            "address = ?, area = ?, sub_area = ?, pincode = ?, status = ?, notes = ?, "
            "collect_paise = ?, custom_plan_amount_paise = ?, updated_at = ? "
            "WHERE id = ?",
            (
                code.strip() or None,
                name.strip(),
                phone.strip(),
                alt_phone.strip(),
                email.strip(),
                address.strip(),
                area.strip(),
                sub_area.strip(),
                pincode.strip(),
                status.strip() or "active",
                notes.strip(),
                to_paise(collect_amount) if (collect_amount or "").strip() else 0,
                to_paise(collect_amount) if (collect_amount or "").strip() else 0,
                now_iso(),
                customer_id,
            ),
        )
    return _redirect(f"/customers/{customer_id}", flash="Customer details saved.")


@router.post("/customers/{customer_id}/assigned-agent")
async def customer_assigned_agent(
    request: Request,
    customer_id: int,
    assigned_agent_id: str = Form(""),
):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden("Only admins can override collector assignment.")
    raw = assigned_agent_id.strip()
    agent_id = int(raw) if raw.isdigit() else None
    with transaction() as conn:
        customer = repo.get_customer(conn, customer_id)
        if customer is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        if agent_id:
            row = conn.execute("SELECT id, name FROM agents WHERE id = ? AND active = 1", (agent_id,)).fetchone()
            if row is None:
                return _redirect(
                    f"/customers/{customer_id}",
                    flash="Agent not found.",
                    level="err",
                )
            repo.set_customer_assigned_agent(conn, customer_id, agent_id)
            log_activity(
                conn,
                "customer_agent_override",
                f"Collector override set to {row['name']}",
                customer_id=customer_id,
                actor=(request.state.agent or {}).get("name"),
            )
            flash = f"Customer assigned to {row['name']} (override area)."
        else:
            repo.set_customer_assigned_agent(conn, customer_id, None)
            log_activity(
                conn,
                "customer_agent_override",
                "Collector override cleared — area assignment applies",
                customer_id=customer_id,
                actor=(request.state.agent or {}).get("name"),
            )
            flash = "Override cleared — customer follows area assignment."
    return _redirect(f"/customers/{customer_id}", flash=flash)


@router.post("/customers/{customer_id}/custom-plan")
async def customer_custom_plan_save(
    request: Request,
    customer_id: int,
    plan_name: str = Form(""),
    amount: str = Form(""),
    validity_days: str = Form(""),
    details: str = Form(""),
    bundle: str = Form(""),
):
    if not (
        auth.can(request.state.agent, "customers_edit")
        or auth.can(request.state.agent, "payments")
    ):
        return _forbidden("You cannot change this customer's plan.")
    with transaction() as conn:
        if repo.get_customer(conn, customer_id) is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        billing.set_customer_custom_plan(
            conn,
            customer_id,
            name=plan_name,
            amount_paise=to_paise(amount) if (amount or "").strip() else 0,
            validity_days=_parse_term_days(validity_days),
            details=details,
            bundle=bundle,
        )
        log_activity(conn, "custom_plan", "Customer plan saved", customer_id=customer_id)
    return _redirect(f"/customers/{customer_id}", flash="Customer plan saved. Collection and printed bills use this.")


@router.post("/customers/{customer_id}/location")
async def customer_save_location(
    request: Request,
    customer_id: int,
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
    paste: str = Form(""),
    next: str = Form(""),
):
    if not (auth.can(request.state.agent, "customers_view") or auth.can(request.state.agent, "customers_edit")):
        return _forbidden("You cannot save a house location.")
    parsed = None
    try:
        if lat.strip() and lng.strip():
            parsed = (float(lat.strip()), float(lng.strip()))
            if abs(parsed[0]) > 90 or abs(parsed[1]) > 180:
                parsed = None
    except ValueError:
        parsed = None
    if parsed is None:
        parsed = parse_geo(paste)
    if parsed is None:
        return _redirect(
            _redirect_target(next, f"/customers/{customer_id}"),
            flash="Could not read a location. Allow GPS, or paste coordinates / a Google Maps link.",
            level="err",
        )
    acc = None
    try:
        if accuracy.strip():
            acc = float(accuracy.strip())
    except ValueError:
        acc = None
    agent = request.state.agent or {}
    actor = agent.get("name") or settings.operator
    with transaction() as conn:
        if repo.get_customer(conn, customer_id) is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        conn.execute(
            "UPDATE customers SET lat = ?, lng = ?, geo_accuracy = ?, geo_at = ?, geo_by = ?, "
            "updated_at = ? WHERE id = ?",
            (parsed[0], parsed[1], acc, now_iso(), actor, now_iso(), customer_id),
        )
        agent_id = (request.state.agent or {}).get("id")
        if agent_id:
            field.record_location(
                conn,
                agent_id=int(agent_id),
                lat=parsed[0],
                lng=parsed[1],
                accuracy=acc,
                source="house",
                customer_id=customer_id,
            )
        log_activity(
            conn,
            "location_saved",
            f"House location saved ({parsed[0]:.5f}, {parsed[1]:.5f})",
            customer_id=customer_id,
        )
    return _redirect(
        _redirect_target(next, f"/customers/{customer_id}"),
        flash="House location saved. The next agent can open it in Google Maps.",
    )


@router.post("/customers/{customer_id}/location/clear")
async def customer_clear_location(request: Request, customer_id: int):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden("You cannot remove a house location.")
    with transaction() as conn:
        conn.execute(
            "UPDATE customers SET lat = NULL, lng = NULL, geo_accuracy = NULL, "
            "geo_at = NULL, geo_by = NULL, updated_at = ? WHERE id = ?",
            (now_iso(), customer_id),
        )
        log_activity(conn, "location_cleared", "House location removed", customer_id=customer_id)
    return _redirect(f"/customers/{customer_id}", flash="House location removed.")


@router.post("/customers/{customer_id}/delete")
async def customer_delete(request: Request, customer_id: int):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    with transaction() as conn:
        row = conn.execute("SELECT name FROM customers WHERE id = ?", (customer_id,)).fetchone()
        conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        if row:
            log_activity(conn, "customer_deleted", f"Customer '{row['name']}' deleted")
    return _redirect("/customers", flash="Customer deleted.")


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

@router.post("/customers/{customer_id}/connections")
async def connection_add(
    request: Request,
    customer_id: int,
    provider: str = Form(...),
    upstream_id: str = Form(...),
    card_number: str = Form(""),
    package_name: str = Form(""),
    label: str = Form(""),
    amount: str = Form(""),
    validity_days: str = Form(""),
    expiry_date: str = Form(""),
    notes: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        return _redirect(f"/customers/{customer_id}", flash="Unknown provider.", level="err")
    stb = normalise_upstream_id(provider, upstream_id)
    if not stb:
        return _redirect(
            f"/customers/{customer_id}",
            flash="Enter the OTT/IPTV phone, Railtel login, or Hathway STB.",
            level="err",
        )
    expiry_norm, expiry_err = normalise_expiry_input(expiry_date)
    if expiry_err:
        return _redirect(f"/customers/{customer_id}", flash=expiry_err, level="err")

    stamp = now_iso()
    try:
        with transaction() as conn:
            existing = conn.execute(
                "SELECT cn.customer_id, cn.upstream_id, c.name AS customer_name "
                "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
                "WHERE cn.provider = ? AND lower(trim(cn.upstream_id)) = lower(?)",
                (provider, stb),
            ).fetchone()
            if existing:
                if int(existing["customer_id"]) == customer_id:
                    return _redirect(
                        f"/customers/{customer_id}",
                        flash=(
                            f"{stb} is already on this customer. A second {PROVIDER_LABELS[provider]} "
                            f"line needs a different login. Household collect amount is the "
                            f"Custom plan on the Plan tab — not a second copy of the same login."
                        ),
                        level="err",
                    )
                return _redirect(
                    f"/customers/{customer_id}",
                    flash=(
                        f"{stb} is already on {existing['customer_name']} "
                        f"(#{existing['customer_id']}). Each {PROVIDER_LABELS[provider]} "
                        f"login can only exist once."
                    ),
                    level="err",
                )
            pkg_id, unknown_plan = _resolve_package_id(conn, provider, package_name)
            billing_type = billing.billing_type_for(provider)
            conn.execute(
                "INSERT INTO connections(customer_id, provider, upstream_id, card_number, "
                "package_id, label, status, billing_type, amount_paise, validity_days, "
                "expiry_date, upstream_plan_name, notes, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    customer_id,
                    provider,
                    stb,
                    card_number.strip(),
                    pkg_id,
                    label.strip(),
                    billing_type,
                    to_paise(amount),
                    _parse_term_days(validity_days),
                    expiry_norm,
                    package_name.strip(),
                    notes.strip(),
                    stamp,
                    stamp,
                ),
            )
            log_activity(
                conn,
                "connection_added",
                f"{PROVIDER_LABELS[provider]} connection {stb} added",
                customer_id=customer_id,
            )
    except sqlite3.IntegrityError:
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"{stb} is already on the platform. Use a different login for a second line.",
            level="err",
        )
    except Exception as exc:
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Could not add connection: {exc}",
            level="err",
        )
    if unknown_plan:
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Connection added. Plan '{package_name.strip()}' is not in the catalog yet — "
                  f"add it under Plans if you want automatic billing amounts.",
        )
    return _redirect(f"/customers/{customer_id}", flash="Connection added.")


@router.post("/connections/{connection_id}/edit")
async def connection_edit(
    request: Request,
    connection_id: int,
    upstream_id: str = Form(...),
    card_number: str = Form(""),
    package_name: str = Form(""),
    label: str = Form(""),
    status: str = Form("active"),
    amount: str = Form(""),
    validity_days: str = Form(""),
    expiry_date: str = Form(""),
    notes: str = Form(""),
    discount: str | None = Form(None),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, provider, upstream_id, discount_paise FROM connections WHERE id = ?",
            (connection_id,),
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        stb = normalise_upstream_id(row["provider"], upstream_id)
        pkg_id, unknown_plan = _resolve_package_id(conn, row["provider"], package_name)
        expiry_norm, expiry_err = normalise_expiry_input(expiry_date)
        if expiry_err:
            return _redirect(f"/customers/{row['customer_id']}", flash=expiry_err, level="err")
        conn.execute(
            "UPDATE connections SET upstream_id = ?, card_number = ?, package_id = ?, label = ?, "
            "status = ?, billing_type = ?, amount_paise = ?, validity_days = ?, expiry_date = ?, "
            "upstream_plan_name = ?, notes = ?, updated_at = ? WHERE id = ?",
            (
                stb,
                card_number.strip(),
                pkg_id,
                label.strip(),
                status.strip() or "active",
                billing.billing_type_for(row["provider"]),
                to_paise(amount),
                _parse_term_days(validity_days),
                expiry_norm,
                package_name.strip(),
                notes.strip(),
                now_iso(),
                connection_id,
            ),
        )
        customer_id = int(row["customer_id"])
        if discount is not None:
            old_discount = int(row["discount_paise"] or 0)
            new_discount = max(0, to_paise(discount) or 0)
            if new_discount != old_discount:
                conn.execute(
                    "UPDATE connections SET discount_paise = ? WHERE id = ?",
                    (new_discount, connection_id),
                )
                log_activity(
                    conn,
                    "connection_discount",
                    f"{row['upstream_id']} monthly discount ₹{old_discount / 100:g} → ₹{new_discount / 100:g}",
                    actor=(request.state.agent or {}).get("name") or "",
                    customer_id=customer_id,
                    connection_id=connection_id,
                )
    if unknown_plan:
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Saved. Plan '{package_name.strip()}' is not in the catalog — "
                  f"add it under Plans for catalog pricing.",
        )
    return _redirect(f"/customers/{customer_id}", flash="Connection saved.")


@router.post("/connections/{connection_id}/owner")
async def connection_owner(
    request: Request,
    connection_id: int,
    owner_reason: str = Form(""),
    owner_note: str = Form(""),
):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, upstream_id FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        customer_id = int(row["customer_id"])
        reason = (owner_reason or "").strip().lower()
        billing.set_connection_owner(conn, connection_id, reason, owner_note)
        label = billing.OWNER_REASONS.get(reason, "")
        log_activity(
            conn,
            "owner_collected",
            f"{row['upstream_id']} marked owner collected ({label})" if label
            else f"{row['upstream_id']} is no longer owner collected",
            actor=(request.state.agent or {}).get("name") or "",
            customer_id=customer_id,
            connection_id=connection_id,
        )
    if not label:
        return _redirect(f"/customers/{customer_id}", flash="Owner collected removed — agents will see this customer again.")
    return _redirect(
        f"/customers/{customer_id}",
        flash=f"Marked owner collected ({label}). Hidden from collection agents.",
    )


@router.post("/connections/{connection_id}/free")
async def connection_free(
    request: Request,
    connection_id: int,
    free_reason: str = Form(""),
    free_note: str = Form(""),
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, upstream_id FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        customer_id = int(row["customer_id"])
        reason = (free_reason or "").strip().lower()
        cancelled = billing.set_connection_free(conn, connection_id, reason, free_note)
        billing.reconcile_customer(conn, customer_id)
        label = billing.FREE_REASONS.get(reason, "")
        log_activity(
            conn,
            "free_stb",
            f"{row['upstream_id']} marked free STB ({label})" if label
            else f"{row['upstream_id']} is no longer a free STB",
            actor=(request.state.agent or {}).get("name") or "",
            customer_id=customer_id,
            connection_id=connection_id,
        )
    if not label:
        return _redirect(f"/customers/{customer_id}", flash="Free STB removed — renewals will be charged again.")
    msg = f"Marked as free STB ({label}). Renewals will not be charged."
    if cancelled:
        msg += f" {cancelled} unpaid bill{'s' if cancelled != 1 else ''} cancelled."
    return _redirect(f"/customers/{customer_id}", flash=msg)


@router.post("/connections/{connection_id}/delete")
async def connection_delete(request: Request, connection_id: int):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, upstream_id FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        customer_id = int(row["customer_id"])
        conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
        log_activity(
            conn, "connection_deleted", f"Connection {row['upstream_id']} removed",
            customer_id=customer_id,
        )
    return _redirect(f"/customers/{customer_id}", flash="Connection removed.")


@router.post("/connections/{connection_id}/action")
async def connection_action(
    request: Request,
    connection_id: int,
    action: str = Form(...),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot run provider portal actions.")
    action = action.strip().lower()
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, provider, upstream_id FROM connections WHERE id = ?",
            (connection_id,),
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        provider = row["provider"]
        customer_id = int(row["customer_id"])
        if action not in PROVIDER_ACTIONS.get(provider, ()):
            return _redirect(
                f"/customers/{customer_id}",
                flash=f"{PROVIDER_LABELS.get(provider, provider)} does not support that action.",
                level="err",
            )
        problem = id_problem(provider, row["upstream_id"] or "")
        if problem:
            return _redirect(
                f"/customers/{customer_id}",
                flash=f"Not queued — {problem}",
                level="err",
            )
        try:
            job_id = job_queue.enqueue_job(
                conn,
                connection_id=connection_id,
                action=action,
                requested_by=_job_by(request),
                needs_confirmation=action not in ("status", "download_bill"),
            )
        except job_queue.RenewNotAllowed as exc:
            return _redirect(
                _safe_next(next, f"/customers/{customer_id}"),
                flash=str(exc),
                level="err",
            )
        busy_note = _exclusive_busy_note(conn, action, job_id)
    dest = _safe_next(next, f"/customers/{customer_id}")
    return _redirect(
        dest,
        flash=_job_flash(action, job_id, provider, busy_note=busy_note),
    )


@router.post("/connections/{connection_id}/collect-later")
async def connection_collect_later(
    request: Request,
    connection_id: int,
    action: str = Form(""),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot run provider portal actions.")
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, provider, upstream_id, status FROM connections WHERE id = ?",
            (connection_id,),
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        customer_id = int(row["customer_id"])
        action = (action or "").strip().lower()
        if action not in ("renew", "activate"):
            off = (row["status"] or "").lower() in {"suspended", "inactive"}
            action = "activate" if row["provider"] == "hathway" and off else "renew"
        if action not in PROVIDER_ACTIONS.get(row["provider"], ()):
            return _redirect(
                f"/customers/{customer_id}",
                flash=f"{PROVIDER_LABELS.get(row['provider'], row['provider'])} does not support that.",
                level="err",
            )
        problem = id_problem(row["provider"], row["upstream_id"] or "")
        if problem:
            return _redirect(f"/customers/{customer_id}", flash=f"Not queued — {problem}", level="err")
        try:
            job_id = job_queue.enqueue_job(
                conn,
                connection_id=connection_id,
                action=action,
                requested_by=_job_by(request),
                collect_later=True,
                needs_confirmation=True,
            )
        except job_queue.RenewNotAllowed as exc:
            return _redirect(
                _safe_next(next, f"/customers/{customer_id}"),
                flash=str(exc),
                level="err",
            )
        log_activity(
            conn,
            "collect_later",
            f"{ACTION_LABELS.get(action, action)} now, collect later",
            customer_id=customer_id,
            connection_id=connection_id,
        )
        busy_note = _exclusive_busy_note(conn, action, job_id)
    later_note = " After it succeeds they appear on Payment follow-up."
    return _redirect(
        _safe_next(next, f"/customers/{customer_id}"),
        flash=_job_flash(action, job_id, row["provider"], busy_note=busy_note) + later_note,
    )


# --------------------------------------------------------------------------- #
# Collect payment
# --------------------------------------------------------------------------- #

@router.post("/customers/{customer_id}/check-all")
async def customer_check_all(
    request: Request,
    customer_id: int,
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot run provider portal actions.")
    with transaction() as conn:
        job_ids, skipped = job_queue.enqueue_customer_status(conn, customer_id)

    dest = _safe_next(next, f"/customers/{customer_id}")
    if not job_ids:
        detail = skipped[0] if len(skipped) == 1 else f"{len(skipped)} connection(s) skipped"
        return _redirect(
            dest,
            flash=f"Nothing to check — {detail}." if skipped else "This customer has no connections.",
            level="err",
        )

    note = f"Checking {len(job_ids)} connection(s) against the provider portals now."
    if skipped:
        note += f" Skipped {len(skipped)}: {skipped[0]}"
    return _redirect(dest, flash=note)


@router.post("/customers/{customer_id}/collect")
async def collect_payment(
    request: Request,
    customer_id: int,
    amount: str = Form(""),
    mode: str = Form("cash"),
    connection_id: str = Form(""),
    reference: str = Form(""),
    paid_at: str = Form(""),
    notes: str = Form(""),
    remember_collect: str = Form(""),
    renew: str = Form(""),
    whatsapp: str = Form(""),
    whatsapp_shown: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "payments"):
        return _forbidden("You cannot collect payments.")
    dest = (next or "").strip() or f"/customers/{customer_id}"
    blocked = _require_field_duty(request, dest)
    if blocked:
        return blocked

    conn_id = int(connection_id) if connection_id.strip() else None
    wants_renew = (renew or "").strip().lower() in {"1", "on", "true", "yes"}
    wants_wa = (whatsapp or "").strip().lower() in {"1", "on", "true", "yes"}
    if wants_wa and not auth.can(request.state.agent, "customer_whatsapp"):
        wants_wa = False
    later = (mode or "").strip().lower() in {"collect_later", "renew_collect_later"}

    if later:
        if not auth.can(request.state.agent, "portal_actions"):
            return _forbidden("You cannot run provider portal actions.")
        if not conn_id:
            return _redirect(
                f"/customers/{customer_id}",
                flash="Pick a connection for renew and collect later.",
                level="err",
            )
        with transaction() as conn:
            row = conn.execute(
                "SELECT customer_id, provider, upstream_id FROM connections WHERE id = ?",
                (conn_id,),
            ).fetchone()
            if row is None or int(row["customer_id"]) != customer_id:
                return _redirect(
                    f"/customers/{customer_id}",
                    flash="That connection is not on this customer.",
                    level="err",
                )
            problem = id_problem(row["provider"], row["upstream_id"] or "")
            if problem:
                return _redirect(
                    f"/customers/{customer_id}",
                    flash=f"Not queued — {problem}",
                    level="err",
                )
            try:
                job_id = job_queue.enqueue_job(
                    conn,
                    connection_id=conn_id,
                    action="renew",
                    requested_by=_job_by(request),
                    collect_later=True,
                    needs_confirmation=True,
                )
            except job_queue.RenewNotAllowed as exc:
                return _redirect(f"/customers/{customer_id}", flash=str(exc), level="err")
            log_activity(
                conn,
                "collect_later",
                "Renew and collect later",
                customer_id=customer_id,
                connection_id=conn_id,
            )
        return _redirect(
            f"/customers/{customer_id}",
            flash=_job_flash("renew", job_id, row["provider"])
            + " After it succeeds they appear on Payment follow-up as Renewed, not paid.",
        )

    amount_paise = to_paise(amount)
    if amount_paise <= 0:
        return _redirect(f"/customers/{customer_id}", flash="Enter an amount above zero.",
                         level="err")
    remember = (remember_collect or "").strip().lower() in {"1", "on", "true", "yes"}
    if (mode or "").strip() and (mode.strip().lower() not in PAYMENT_MODES):
        mode = "cash"
    coords = field.parse_coords(lat, lng)
    acc = field.parse_accuracy(accuracy)
    paid_stamp = _collect_paid_at(
        paid_at, can_set_date=auth.can(request.state.agent, "payment_date")
    )

    with transaction() as conn:
        agent = request.state.agent or {}
        agent_id = int(agent["id"]) if agent.get("id") else None
        billing.ensure_custom_plan_bill(conn, customer_id, connection_id=conn_id)
        payment_id = billing.record_payment(
            conn,
            customer_id=customer_id,
            connection_id=conn_id,
            amount_paise=amount_paise,
            mode=mode.strip() or "cash",
            reference=reference.strip(),
            collected_by=agent.get("name") or settings.operator,
            collected_agent_id=agent_id,
            paid_at=paid_stamp,
            notes=notes.strip(),
        )
        field.record_visit(
            conn,
            agent_id=agent_id,
            customer_id=customer_id,
            lat=coords[0] if coords else None,
            lng=coords[1] if coords else None,
            accuracy=acc,
            source="payment",
        )
        billing.reconcile_customer(conn, customer_id)
        remaining = int(billing.customer_ledger(conn, customer_id)["net_due_paise"])
        receipt = conn.execute(
            "SELECT receipt_no FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()["receipt_no"]
        log_activity(
            conn,
            "payment_recorded",
            f"Payment {receipt} received",
            customer_id=customer_id,
            connection_id=conn_id,
            meta_json=json.dumps({"payment_id": payment_id, "amount_paise": amount_paise}),
        )
        if (remember_collect or "").strip().lower() in {"1", "on", "true", "yes"}:
            billing.set_customer_collect_paise(conn, customer_id, amount_paise)

        job_id = None
        renew_provider = ""
        renew_blocked = None
        busy_note = ""
        if wants_renew and conn_id and auth.can(request.state.agent, "portal_actions"):
            cn = conn.execute(
                "SELECT provider FROM connections WHERE id = ?", (conn_id,)
            ).fetchone()
            renew_provider = cn["provider"] if cn else ""
            try:
                job_id = job_queue.enqueue_job(
                    conn,
                    connection_id=conn_id,
                    action="renew",
                    payment_id=payment_id,
                    requested_by=_job_by(request),
                    needs_confirmation=True,
                )
                busy_note = _exclusive_busy_note(conn, "renew", job_id)
            except job_queue.RenewNotAllowed as exc:
                renew_blocked = str(exc)

    wa_url = ""
    auto_wa_note = ""
    if settings.whatsapp_web_auto_send:
        from ..whatsapp_notify import queue_customer_message

        # Forms without the checkbox (agents, quick collect) still confirm automatically.
        send_confirm = wants_wa if whatsapp_shown.strip() else True
        if send_confirm and (mode or "").strip().lower() != "adjustment":
            reason = queue_customer_message(
                customer_id=customer_id,
                kind="payment",
                connection_id=conn_id,
                provider=renew_provider or None,
                agent_id=agent_id,
                amount_paise=amount_paise,
                remaining_paise=remaining,
                renew_queued=bool(job_id),
            )
            auto_wa_note = (
                " WhatsApp confirmation is being sent from the office number."
                if not reason else f" WhatsApp not sent: {reason}"
            )
        wants_wa = False
    if wants_wa:
        from ..messaging import payment_received_whatsapp_url

        with connection() as conn:
            cust = repo.get_customer(conn, customer_id)
        if cust is not None:
            wa_url = (
                payment_received_whatsapp_url(
                    cust["name"],
                    cust["phone"],
                    provider=renew_provider or None,
                    providers_csv=None if renew_provider else (cust["providers"] or None),
                    amount_paise=amount_paise,
                    remaining_paise=remaining,
                    renew_queued=bool(job_id),
                )
                or ""
            )
    collected = f"Collected ₹{fmt_rupees(amount_paise)}"
    if remaining <= 0:
        due_line = "Nothing outstanding."
    else:
        due_line = f"Due now ₹{fmt_rupees(remaining)}."
    statement = f"/customers/{customer_id}?tab=statement"
    if renew_blocked:
        return _redirect(
            statement,
            flash=f"{collected}. {due_line} Receipt {receipt}. Renew was blocked — {renew_blocked}"
            + auto_wa_note,
            level="err",
            whatsapp_url=wa_url,
        )

    flash = f"{collected}. {due_line} Receipt {receipt}."
    if job_id:
        if (renew_provider or "").lower() in {"iptv", "ott"}:
            flash += " " + _job_flash("renew", job_id, renew_provider, busy_note=busy_note)
        else:
            flash += " Renewal queued — confirm it after they leave."
            if busy_note:
                flash += busy_note
    elif wants_renew:
        flash += " Pick a connection if they also need a portal recharge."
    if wants_wa and wa_url:
        flash += " Send WhatsApp to confirm."
    elif wants_wa:
        flash += " No customer phone for WhatsApp."
    flash += auto_wa_note
    target = _redirect_target(next, statement)
    return _redirect(target, flash=flash, whatsapp_url=wa_url)


@router.post("/customers/{customer_id}/balance")
async def customer_set_balance(
    request: Request,
    customer_id: int,
    amount: str = Form(...),
    reason: str = Form(""),
):
    if not auth.can(request.state.agent, "change_due"):
        return _forbidden("You cannot change a customer's due amount.")
    target = to_paise(amount)
    if target < 0:
        return _redirect(
            f"/customers/{customer_id}",
            flash="Balance cannot be negative. Use Collect payment if they overpaid.",
            level="err",
        )
    with transaction() as conn:
        if repo.get_customer(conn, customer_id) is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        agent = request.state.agent or {}
        result = billing.set_customer_due(
            conn,
            customer_id,
            target,
            actor=agent.get("name") or settings.operator,
            reason=reason.strip(),
        )
        if result["changed"]:
            log_activity(
                conn,
                "balance_set",
                f"Balance set to ₹{fmt_rupees(target)} "
                f"(was ₹{fmt_rupees(result['from_paise'])})"
                + (f" — {reason.strip()}" if reason.strip() else ""),
                customer_id=customer_id,
            )
    if not result["changed"]:
        return _redirect(f"/customers/{customer_id}", flash="Balance is already that amount.")
    return _redirect(
        f"/customers/{customer_id}",
        flash=f"Balance changed from ₹{fmt_rupees(result['from_paise'])} "
              f"to ₹{fmt_rupees(result['to_paise'])}. It is on the statement.",
    )


def _find_followup_customer(conn, raw: str):
    text = (raw or "").strip()
    if not text:
        return None, "Enter a customer name, phone, or code."
    if text.isdigit():
        row = repo.get_customer(conn, int(text))
        if row is not None:
            return row, ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        phone_conds, phone_params = repo.phone_search_or_columns(["phone", "alt_phone"], text)
        if phone_conds:
            rows = conn.execute(
                f"SELECT * FROM customers WHERE {' OR '.join(phone_conds)} LIMIT 2",
                phone_params,
            ).fetchall()
            if len(rows) == 1:
                return rows[0], ""
            if len(rows) > 1:
                return None, "More than one customer has that phone. Use the customer page."
    like = f"%{text}%"
    rows = conn.execute(
        "SELECT * FROM customers WHERE name LIKE ? OR code LIKE ? "
        "ORDER BY name COLLATE NOCASE LIMIT 6",
        (like, like),
    ).fetchall()
    if len(rows) == 1:
        return rows[0], ""
    if not rows:
        return None, f"No customer matches '{text}'."
    names = ", ".join(r["name"] for r in rows[:5])
    return None, f"Several customers match — be more specific ({names})."


@router.get("/payments/follow-up")
async def payments_followup(request: Request):
    kind = (request.query_params.get("kind") or "").strip().lower()
    if kind not in {"", "manual", "renew"}:
        kind = ""
    period = (request.query_params.get("period") or "").strip().lower()
    if kind == "renew" and not period:
        period = "month"
    provider = auth.scoped_provider(request.state.agent, request.query_params.get("provider", ""))
    if provider and provider not in PROVIDERS:
        provider = auth.scoped_provider(request.state.agent, "")
    since, until = repo.followup_period_bounds(period)
    collector_id = auth.collector_id_for(request.state.agent)
    export = wants_csv(request)
    with connection() as conn:
        rows = repo.list_collect_later(
            conn,
            kind=kind,
            since=since,
            until=until,
            provider=provider,
            limit=EXPORT_ROW_LIMIT if export else 200,
            collector_id=collector_id,
        )
        if collector_id:
            rows = repo.without_owner_customers(conn, rows)
        stats = repo.dashboard_stats(
            conn,
            agent_scope=auth.agent_provider_scope(request.state.agent) or "",
            collector_id=collector_id,
        )
        filtered_stats = repo.collect_later_stats(
            conn,
            kind=kind or "",
            since=since,
            until=until,
            provider=provider,
            collector_id=collector_id,
        )
    if export:
        return list_exports.followup_csv(rows)
    qs_bits = []
    if kind:
        qs_bits.append(f"kind={kind}")
    if period:
        qs_bits.append(f"period={period}")
    if provider:
        qs_bits.append(f"provider={provider}")
    list_next = "/payments/follow-up" + ("?" + "&".join(qs_bits) if qs_bits else "")
    return _render(
        request,
        "payments_followup.html",
        rows=rows,
        stats=stats,
        filtered_stats=filtered_stats,
        payments_tab="followup",
        followup_kind=kind,
        followup_period=period,
        provider=provider,
        providers=PROVIDERS,
        list_next=list_next,
        export_href=export_url("/payments/follow-up", kind=kind, period=period, provider=provider),
        fixed_provider=auth.agent_provider_scope(request.state.agent),
    )


@router.post("/payments/follow-up")
async def payments_followup_add(
    request: Request,
    customer: str = Form(...),
    connection_id: str = Form(""),
    amount: str = Form(""),
    notes: str = Form(""),
):
    if not auth.can(request.state.agent, "payments"):
        return _forbidden("You cannot add payment follow-ups.")
    try:
        with transaction() as conn:
            row, problem = _find_followup_customer(conn, customer)
            if row is None:
                return _redirect("/payments/follow-up?kind=manual", flash=problem, level="err")
            conn_id = int(connection_id) if (connection_id or "").strip().isdigit() else None
            amount_paise = to_paise(amount) if (amount or "").strip() else 0
            result = billing.add_manual_followup(
                conn,
                customer_id=int(row["id"]),
                connection_id=conn_id,
                amount_paise=amount_paise,
                notes=notes.strip(),
            )
            log_activity(
                conn,
                "followup_added",
                "Manual payment follow-up added",
                customer_id=int(row["id"]),
                connection_id=conn_id,
            )
    except ValueError as exc:
        return _redirect("/payments/follow-up?kind=manual", flash=str(exc), level="err")
    if result["created"]:
        flash = f"{row['name']} added to manual follow-up with a new amount."
    else:
        flash = f"{row['name']} added to manual follow-up (existing unpaid bills)."
    return _redirect("/payments/follow-up?kind=manual", flash=flash)


@router.post("/customers/{customer_id}/follow-up")
async def customer_followup_add(
    request: Request,
    customer_id: int,
    connection_id: str = Form(""),
    amount: str = Form(""),
    notes: str = Form(""),
):
    if not auth.can(request.state.agent, "payments"):
        return _forbidden("You cannot add payment follow-ups.")
    try:
        with transaction() as conn:
            conn_id = int(connection_id) if (connection_id or "").strip().isdigit() else None
            amount_paise = to_paise(amount) if (amount or "").strip() else 0
            result = billing.add_manual_followup(
                conn,
                customer_id=customer_id,
                connection_id=conn_id,
                amount_paise=amount_paise,
                notes=notes.strip(),
            )
            log_activity(
                conn,
                "followup_added",
                "Manual payment follow-up added",
                customer_id=customer_id,
                connection_id=conn_id,
            )
    except ValueError as exc:
        return _redirect(f"/customers/{customer_id}", flash=str(exc), level="err")
    if result["created"]:
        flash = "Added to manual follow-up with a new amount."
    else:
        flash = "Added to manual follow-up."
    return _redirect(f"/customers/{customer_id}", flash=flash)


@router.post("/bills/{bill_id}/drop-followup")
async def bill_drop_followup(request: Request, bill_id: int, next: str = Form("")):
    if not auth.can(request.state.agent, "payments"):
        return _forbidden("You cannot change payment follow-ups.")
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, bill_no FROM bills WHERE id = ?", (bill_id,)
        ).fetchone()
        if row is None:
            return _redirect("/payments/follow-up", flash="Follow-up not found.", level="err")
        if not billing.drop_followup(conn, bill_id):
            return _redirect(
                _safe_next(next, "/payments/follow-up"),
                flash="That item is not on the follow-up list.",
                level="err",
            )
        log_activity(
            conn,
            "followup_dropped",
            f"Follow-up {row['bill_no']} removed from the list",
            customer_id=int(row["customer_id"]),
        )
    return _redirect(
        _safe_next(next, "/payments/follow-up"),
        flash=f"{row['bill_no']} taken off the follow-up list. The bill is still there.",
    )


@router.get("/payments/bix", response_class=HTMLResponse)
async def payments_bix_list(request: Request):
    params = request.query_params
    date_from = (params.get("from") or "").strip()
    date_to = (params.get("to") or "").strip()
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    with connection() as conn:
        result = bix_history.list_archive_payments(
            conn, date_from=date_from, date_to=date_to, limit=150
        )
        imported = bix_history.imported_stats(conn)
    return _render(
        request,
        "payments_bix.html",
        rows=result["rows"],
        range_total_paise=result["total_paise"],
        range_count=result["count"],
        date_from=date_from,
        date_to=date_to,
        stats=imported,
    )


@router.post("/railtel-invoices/{invoice_id}/whatsapp")
async def railtel_invoice_whatsapp(request: Request, invoice_id: int, next: str = Form("")):
    if not auth.can(request.state.agent, "customer_whatsapp"):
        return _forbidden("You cannot send WhatsApp to customers.")
    from .. import railtel_invoices as rt_inv

    with connection() as conn:
        inv = repo.get_railtel_invoice(conn, invoice_id)
        if inv is None:
            return _redirect("/customers", flash="Invoice not found.", level="err")
        cust = conn.execute(
            "SELECT name, phone FROM customers WHERE id = ?", (inv["customer_id"],)
        ).fetchone()
        customer_id = int(inv["customer_id"])
        connection_id = int(inv["connection_id"]) if inv["connection_id"] else None

    # Playwright sync API must not run on the asyncio event loop.
    result = await asyncio.to_thread(
        rt_inv.send_invoice_whatsapp_work,
        invoice_id,
        customer_name=cust["name"] if cust else "",
        customer_phone=cust["phone"] if cust else "",
    )
    with transaction() as conn:
        log_activity(
            conn,
            "whatsapp_sent" if result.get("ok") else "whatsapp_failed",
            result.get("message") or result.get("error") or "WhatsApp send",
            customer_id=customer_id,
            connection_id=connection_id,
        )
    flash = result.get("message") if result.get("ok") else result.get("error")
    return _redirect(
        _safe_next(next, f"/customers/{inv['customer_id']}"),
        flash=flash or "WhatsApp send attempted.",
        level="ok" if result.get("ok") else "err",
    )


@router.get("/railtel-invoices/{invoice_id}/pdf")
async def railtel_invoice_pdf(request: Request, invoice_id: int):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot download portal bills.")
    with connection() as conn:
        inv = repo.get_railtel_invoice(conn, invoice_id)
        if inv is None:
            return _render(request, "not_found.html", what="Railtel invoice")
        path = Path(inv["file_path"])
        if not path.is_file():
            return _redirect(
                f"/customers/{inv['customer_id']}",
                flash="Portal bill file is missing on disk — download again.",
                level="err",
            )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=inv["file_name"] or path.name,
    )


@router.get("/bills/{bill_id}/print", response_class=HTMLResponse)
async def bill_print(request: Request, bill_id: int):
    with connection() as conn:
        bill = repo.get_bill(conn, bill_id)
        if bill is None:
            return _render(request, "not_found.html", what="Bill")
        connections = repo.customer_connections(conn, int(bill["customer_id"]))
        ledger = billing.customer_ledger(conn, int(bill["customer_id"]))
    return _templates().TemplateResponse(
        request,
        "bill_print.html",
        {
            "bill": bill,
            "connections": connections,
            "ledger": ledger,
            "provider_labels": PROVIDER_LABELS_VIEW,
        },
    )


@router.get("/payments/{payment_id}/receipt", response_class=HTMLResponse)
async def payment_receipt(request: Request, payment_id: int):
    collector_id = auth.collector_id_for(request.state.agent)
    with connection() as conn:
        payment = repo.get_payment(conn, payment_id)
        if payment is None:
            return _render(request, "not_found.html", what="Payment")
        if collector_id and int(payment["collected_agent_id"] or 0) != int(collector_id):
            return _forbidden("You can only view receipts for payments you collected.")
        allocations = repo.payment_allocations(conn, payment_id)
        ledger = billing.customer_ledger(conn, int(payment["customer_id"]))
    return _templates().TemplateResponse(
        request,
        "receipt.html",
        {
            "payment": payment,
            "allocations": allocations,
            "ledger": ledger,
        },
    )


@router.post("/payments/{payment_id}/delete")
async def payment_delete(
    request: Request,
    payment_id: int,
    next: str = Form(""),
):
    if not (
        auth.can(request.state.agent, "payments")
        or auth.can(request.state.agent, "customers_edit")
    ):
        return _forbidden("You cannot delete payments.")
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, receipt_no, collected_agent_id FROM payments WHERE id = ?",
            (payment_id,),
        ).fetchone()
        if row is None:
            return _redirect("/payments", flash="Payment not found.", level="err")
        collector_id = auth.collector_id_for(request.state.agent)
        if collector_id and int(row["collected_agent_id"] or 0) != int(collector_id):
            return _forbidden("You can only delete payments you collected.")
        customer_id = int(row["customer_id"])
        billing.delete_payment(conn, payment_id)
        billing.reconcile_customer(conn, customer_id)
        log_activity(conn, "payment_deleted", f"Payment {row['receipt_no']} deleted",
                     customer_id=customer_id)
    dest = _safe_next(next, f"/customers/{customer_id}")
    return _redirect(dest, flash=f"Payment {row['receipt_no']} deleted. The ledger was rebuilt.")


# --------------------------------------------------------------------------- #
# Providers (dealer accounts)
# --------------------------------------------------------------------------- #

@router.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    if auth.agent_provider_scope(request.state.agent) == "hathway":
        return _forbidden("The providers page is not available for Hathway-only agents.")
    with connection() as conn:
        overview = repo.provider_overview(conn)
        broken = repo.unusable_connections(conn)
        schedule = job_queue.sync_schedule(conn)
        current = job_queue.active_sweep(conn)
        progress = job_queue.sweep_progress(conn, int(current["id"])) if current else None
        recent_sweeps = repo.recent_sweeps(conn)
        pending_counts = {
            p: len(job_queue.sweep_candidates(
                conn, providers=p, stale_days=int(schedule["sync_schedule_stale_days"])))
            for p in PROVIDERS
        }
    problems = {int(row["id"]): id_problem(row["provider"], row["upstream_id"] or "")
                for row in broken}
    return _render(
        request,
        "providers.html",
        overview=overview,
        broken=broken,
        problems=problems,
        account_actions=ACCOUNT_ACTIONS,
        schedule=schedule,
        progress=progress,
        recent_sweeps=recent_sweeps,
        pending_counts=pending_counts,
        sweep_hours=range(24),
        providers_tab="overview",
    )


@router.get("/providers/online")
async def providers_online(request: Request):
    q = (request.query_params.get("q") or "").strip()
    view = (request.query_params.get("view") or "all").strip()
    if view not in ("all", "known", "unknown"):
        view = "all"
    export = wants_csv(request)
    with connection() as conn:
        snapshot = repo.latest_railtel_online(conn)
        rows = repo.railtel_online_rows(conn, int(snapshot["id"]), q=q, view=view) if snapshot else []
        missing = repo.railtel_online_local_offline(conn, int(snapshot["id"])) if snapshot else []
        open_job = conn.execute(
            "SELECT id, status, created_at FROM upstream_jobs "
            "WHERE provider = 'railtel' AND action = 'online' "
            "AND status IN ('awaiting_confirm', 'queued', 'running') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_job = conn.execute(
            "SELECT id, status, error, completed_at FROM upstream_jobs "
            "WHERE provider = 'railtel' AND action = 'online' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        local_railtel = int(conn.execute(
            "SELECT COUNT(*) AS n FROM connections WHERE provider = 'railtel' "
            "AND status IN ('active', 'suspended')"
        ).fetchone()["n"])
    matched = sum(1 for row in rows if row.get("customer_id"))
    unknown = sum(1 for row in rows if not row.get("customer_id"))
    if export:
        return list_exports.railtel_online_csv(rows)
    return _render(
        request,
        "providers_online.html",
        snapshot=snapshot,
        rows=rows,
        missing=missing,
        q=q,
        view=view,
        open_job=open_job,
        last_job=last_job,
        matched=matched,
        unknown=unknown,
        local_railtel=local_railtel,
        providers_tab="online",
        export_href=export_url("/providers/online", q=q, view=view),
    )


@router.post("/sync/start")
async def sync_start(
    providers: str = Form("both"),
    stale_days: str = Form("7"),
    limit: str = Form("700"),
):
    try:
        stale = max(0, int(stale_days))
        cap = max(1, int(limit))
    except ValueError:
        return _redirect("/providers", flash="Days and limit must be numbers.", level="err")

    with transaction() as conn:
        sweep_id, queued, note = job_queue.start_sweep(
            conn, providers=providers, stale_days=stale, limit=cap, trigger="manual"
        )
    return _redirect("/providers", flash=note, level="ok" if sweep_id else "err")


@router.post("/sync/{sweep_id}/cancel")
async def sync_cancel(sweep_id: int):
    with transaction() as conn:
        dropped = job_queue.cancel_sweep(conn, sweep_id)
    return _redirect(
        "/providers",
        flash=f"Sweep #{sweep_id} stopped. {dropped} pending check(s) dropped; "
              f"anything already running will finish.",
    )


@router.post("/sync/schedule")
async def sync_schedule_save(
    enabled: str = Form(""),
    hour: str = Form("2"),
    providers: str = Form("both"),
    stale_days: str = Form("7"),
    limit: str = Form("700"),
):
    with transaction() as conn:
        job_queue.save_sync_schedule(conn, {
            "sync_schedule_enabled": "1" if enabled else "0",
            "sync_schedule_hour": hour,
            "sync_schedule_providers": providers,
            "sync_schedule_stale_days": stale_days,
            "sync_schedule_limit": limit,
        })
    state = "on" if enabled else "off"
    return _redirect("/providers", flash=f"Schedule saved and turned {state}.")


@router.post("/providers/{provider}/{action}")
async def provider_action(request: Request, provider: str, action: str):
    provider = provider.strip().lower()
    action = action.strip().lower()
    if action not in ACCOUNT_ACTIONS.get(provider, ()):
        return _redirect("/providers", flash="Unknown provider action.", level="err")

    with transaction() as conn:
        job_id = job_queue.enqueue_provider_job(
            conn, provider=provider, action=action, requested_by=_job_by(request)
        )
        busy_note = _exclusive_busy_note(conn, action, job_id)
    dest = "/providers/online" if action == "online" else "/providers"
    return _redirect(
        dest,
        flash=f"{ACTION_LABELS.get(action, action)} queued as job #{job_id} for "
              f"{PROVIDER_LABELS.get(provider, provider)}.{busy_note}",
    )


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@router.get("/jobs")
async def jobs_list(request: Request):
    status = request.query_params.get("status", "")
    export = wants_csv(request)
    job_provider = auth.agent_provider_scope(request.state.agent) or ""
    with connection() as conn:
        rows = _with_job_creator(repo.list_jobs(
            conn,
            status=status,
            limit=EXPORT_ROW_LIMIT if export else 200,
            provider=job_provider,
        ))
        stats = repo.dashboard_stats(conn, agent_scope=job_provider)
    if export:
        return list_exports.jobs_csv(rows)
    return _render(
        request,
        "jobs.html",
        rows=rows,
        status=status,
        stats=stats,
        export_href=export_url("/jobs", status=status),
    )


@router.post("/jobs/{job_id}/otp")
async def job_submit_otp(
    request: Request,
    job_id: int,
    otp: str = Form(...),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot run provider portal actions.")
    dest = _safe_next(next, f"/jobs/{job_id}")
    with transaction() as conn:
        ok = job_queue.submit_otp(conn, job_id, otp)
    if not ok:
        return _redirect(
            dest,
            flash="Enter 6 digits (OTP) or 10 digits (WhatsApp number), and job #{0} must be waiting.".format(job_id),
            level="err",
        )
    return _redirect(dest, flash=f"OTP submitted for job #{job_id}. ANT will continue.")


@router.post("/jobs/{job_id}/{operation}")
async def job_operation(job_id: int, operation: str):
    handlers = {
        "confirm": job_queue.confirm_job,
        "cancel": job_queue.cancel_job,
        "retry": job_queue.retry_job,
    }
    handler = handlers.get(operation)
    if handler is None:
        return _redirect("/jobs", flash="Unknown job operation.", level="err")

    with transaction() as conn:
        changed = handler(conn, job_id)

    if not changed:
        return _redirect("/jobs", flash=f"Job #{job_id} could not be {operation}ed in its current state.",
                         level="err")
    verb = {"confirm": "confirmed and queued", "cancel": "cancelled", "retry": "queued again"}[operation]
    return _redirect("/jobs", flash=f"Job #{job_id} {verb}.")


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    with connection() as conn:
        job = repo.get_job(conn, job_id)
        if job is None:
            return _render(request, "not_found.html", what="Job")
        job = _with_job_creator([job])[0]
    pretty = ""
    if job["result_json"]:
        try:
            pretty = json.dumps(json.loads(job["result_json"]), indent=2)
        except Exception:
            pretty = job["result_json"]
    return _render(request, "job_detail.html", job=job, result_pretty=pretty)


# --------------------------------------------------------------------------- #
# Bills & payments
# --------------------------------------------------------------------------- #

@router.get("/bills")
async def bills_list(request: Request):
    status = request.query_params.get("status", "open")
    export = wants_csv(request)
    with connection() as conn:
        rows = repo.list_bills(conn, status=status, limit=EXPORT_ROW_LIMIT if export else 200)
    if export:
        return list_exports.bills_csv(rows)
    return _render(
        request,
        "bills.html",
        rows=rows,
        status=status,
        export_href=export_url("/bills", status=status),
    )


@router.post("/bills/run-checker")
async def bills_run_checker(request: Request):
    if not auth.can(request.state.agent, "bills"):
        return _forbidden()
    with transaction() as conn:
        summary = billing.run_bill_checker(conn)
        log_activity(
            conn,
            "bill_checker",
            f"Bill checker generated {summary['generated']} bill(s)",
            meta_json=json.dumps(summary["skipped"]),
        )
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(summary["skipped"].items())) or "none"
    return _redirect(
        "/bills",
        flash=f"Generated {summary['generated']} bill(s). Skipped — {reasons}.",
    )


@router.post("/bills/{bill_id}/cancel")
async def bill_cancel(request: Request, bill_id: int, next: str = Form("")):
    if not (
        auth.can(request.state.agent, "bills")
        or auth.can(request.state.agent, "customers_edit")
    ):
        return _forbidden("You cannot cancel bills.")
    with transaction() as conn:
        row = conn.execute("SELECT customer_id, bill_no FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if row is None:
            return _redirect("/bills", flash="Bill not found.", level="err")
        billing.cancel_bill(conn, bill_id)
        billing.reconcile_customer(conn, int(row["customer_id"]))
        log_activity(conn, "bill_cancelled", f"Bill {row['bill_no']} cancelled",
                     customer_id=int(row["customer_id"]))
    dest = _safe_next(next, "/bills")
    return _redirect(dest, flash=f"Bill {row['bill_no']} cancelled. The ledger was rebuilt.")


@router.get("/payments")
async def payments_list(request: Request):
    params = request.query_params
    date_from = (params.get("from") or "").strip()
    date_to = (params.get("to") or "").strip()
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    collector_id = auth.collector_id_for(request.state.agent)
    export = wants_csv(request)
    with connection() as conn:
        result = repo.list_payments(
            conn,
            date_from=date_from,
            date_to=date_to,
            limit=EXPORT_ROW_LIMIT if export else 500,
            collector_id=collector_id,
        )
        stats = repo.dashboard_stats(conn, collector_id=collector_id)
    if export:
        return list_exports.payments_csv(result["rows"])
    return _render(
        request,
        "payments.html",
        rows=result["rows"],
        range_total_paise=result["total_paise"],
        range_count=result["count"],
        date_from=date_from,
        date_to=date_to,
        stats=stats,
        export_href=export_url("/payments", **{"from": date_from, "to": date_to}),
    )


# --------------------------------------------------------------------------- #
# Complaints
# --------------------------------------------------------------------------- #

def _agent_name_for(conn, agent_id: int | None) -> str:
    if not agent_id:
        return ""
    row = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return row["name"] if row else ""


@router.get("/complaints")
async def complaints_page(request: Request):
    params = request.query_params
    status = (params.get("status") or "").strip()
    if status not in repo.COMPLAINT_STATUSES:
        status = ""
    mine = (params.get("mine") or "").strip() in {"1", "on", "yes"}
    me = request.state.agent or {}
    agent_id = int(me["id"]) if mine and me.get("id") else None
    assigned = (params.get("agent") or "").strip()
    if assigned.isdigit() and not agent_id:
        agent_id = int(assigned)
    export = wants_csv(request)
    with connection() as conn:
        rows = repo.list_complaints(
            conn,
            status=status,
            agent_id=agent_id,
            limit=EXPORT_ROW_LIMIT if export else 200,
        )
        agents = repo.list_agents(conn)
        recent_fixed = repo.recent_fixed_complaints(conn, limit=6)
        complaint_group = get_setting(conn, "complaint_whatsapp_group", "")
    if export:
        return list_exports.complaints_csv(rows)
    export_params = {"status": status}
    if mine:
        export_params["mine"] = "1"
    elif assigned:
        export_params["agent"] = assigned
    return _render(
        request,
        "complaints.html",
        rows=rows,
        agents=agents,
        status=status,
        mine=mine,
        assigned=assigned,
        recent_fixed=recent_fixed,
        statuses=repo.COMPLAINT_STATUSES,
        complaint_whatsapp_group=complaint_group or settings.complaint_whatsapp_group,
        export_href=export_url("/complaints", **export_params),
    )


@router.post("/customers/{customer_id}/complaints")
async def complaint_create(
    request: Request,
    customer_id: int,
    title: str = Form(...),
    details: str = Form(""),
    assigned_agent_id: str = Form(""),
):
    if not auth.can(request.state.agent, "complaints"):
        return _forbidden()
    title = title.strip()
    if not title:
        return _redirect(f"/customers/{customer_id}", flash="Write a short complaint title.", level="err")
    actor = (request.state.agent or {}).get("name")
    stamp = now_iso()
    raw_agent = assigned_agent_id.strip()
    agent_id = int(raw_agent) if raw_agent.isdigit() else None
    with transaction() as conn:
        exists = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if exists is None:
            return _redirect("/customers", flash="Customer not found.", level="err")
        assigned_to = _agent_name_for(conn, agent_id)
        status = "in_progress" if agent_id else "open"
        conn.execute(
            "INSERT INTO complaints(customer_id, title, details, status, assigned_agent_id, "
            "assigned_to, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (customer_id, title, details.strip(), status, agent_id, assigned_to, actor, stamp, stamp),
        )
        complaint_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        who = f" → {assigned_to}" if assigned_to else ""
        log_activity(conn, "complaint_opened", f"Complaint: {title}{who}",
                     customer_id=customer_id, actor=actor)
    from ..whatsapp_notify import complaint_whatsapp_primary_url, notify_complaint

    notify_complaint("opened", complaint_id=complaint_id, actor=actor, assigned_agent_id=agent_id)
    wa_url = complaint_whatsapp_primary_url(complaint_id, assigned_agent_id=agent_id) or ""
    flash = f"Complaint logged{who}."
    if wa_url:
        flash += " Tap Open WhatsApp to alert the technician group."
    return _redirect("/complaints", flash=flash, whatsapp_url=wa_url)


@router.post("/complaints/{complaint_id}/assign")
async def complaint_assign(
    request: Request,
    complaint_id: int,
    assigned_agent_id: str = Form(""),
):
    if not auth.can(request.state.agent, "complaints"):
        return _forbidden()
    actor = (request.state.agent or {}).get("name")
    raw = assigned_agent_id.strip()
    agent_id = int(raw) if raw.isdigit() else None
    stamp = now_iso()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
        if row is None:
            return _redirect("/complaints", flash="Complaint not found.", level="err")
        assigned_to = _agent_name_for(conn, agent_id)
        status = "in_progress" if agent_id else "open"
        if row["status"] == "fixed":
            status = "fixed"
        conn.execute(
            "UPDATE complaints SET assigned_agent_id = ?, assigned_to = ?, status = ?, "
            "updated_at = ? WHERE id = ?",
            (agent_id, assigned_to, status, stamp, complaint_id),
        )
        log_activity(
            conn, "complaint_assigned",
            f"Complaint #{complaint_id} assigned to {assigned_to or 'nobody'}",
            customer_id=int(row["customer_id"]), actor=actor,
        )
    from ..whatsapp_notify import complaint_whatsapp_primary_url, notify_complaint

    notify_complaint(
        "assigned",
        complaint_id=complaint_id,
        actor=actor,
        assigned_agent_id=agent_id,
    )
    wa_url = complaint_whatsapp_primary_url(complaint_id, assigned_agent_id=agent_id) or ""
    flash = f"Assigned to {assigned_to}." if assigned_to else "Assignment cleared."
    if wa_url:
        flash += " Tap Open WhatsApp to alert the technician group."
    return _redirect("/complaints", flash=flash, whatsapp_url=wa_url)


@router.post("/complaints/{complaint_id}/note")
async def complaint_note(
    request: Request,
    complaint_id: int,
    last_note: str = Form(...),
):
    if not auth.can(request.state.agent, "complaints"):
        return _forbidden()
    note = last_note.strip()
    if not note:
        return _redirect("/complaints", flash="Write a follow-up note.", level="err")
    actor = (request.state.agent or {}).get("name")
    stamp = now_iso()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
        if row is None:
            return _redirect("/complaints", flash="Complaint not found.", level="err")
        conn.execute(
            "UPDATE complaints SET last_note = ?, status = CASE WHEN status = 'fixed' "
            "THEN status ELSE 'in_progress' END, updated_at = ? WHERE id = ?",
            (note, stamp, complaint_id),
        )
        log_activity(conn, "complaint_note", f"Follow-up on complaint #{complaint_id}",
                     customer_id=int(row["customer_id"]), actor=actor)
    from ..whatsapp_notify import notify_complaint

    notify_complaint("note", complaint_id=complaint_id, actor=actor, note=note)
    return _redirect("/complaints", flash="Follow-up saved.")


@router.post("/complaints/{complaint_id}/fix")
async def complaint_fix(
    request: Request,
    complaint_id: int,
    resolution: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
):
    if not auth.can(request.state.agent, "complaints"):
        return _forbidden()
    blocked = _require_field_duty(request, "/complaints")
    if blocked:
        return blocked
    actor = (request.state.agent or {}).get("name") or "agent"
    agent_id = (request.state.agent or {}).get("id")
    stamp = now_iso()
    coords = field.parse_coords(lat, lng)
    acc = field.parse_accuracy(accuracy)
    with transaction() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
        if row is None:
            return _redirect("/complaints", flash="Complaint not found.", level="err")
        conn.execute(
            "UPDATE complaints SET status = 'fixed', resolution = ?, resolved_by = ?, "
            "resolved_agent_id = ?, resolved_at = ?, updated_at = ? WHERE id = ?",
            (
                resolution.strip() or row["last_note"] or "Fixed",
                actor,
                int(agent_id) if agent_id else None,
                stamp,
                stamp,
                complaint_id,
            ),
        )
        field.record_visit(
            conn,
            agent_id=int(agent_id) if agent_id else None,
            customer_id=int(row["customer_id"]),
            lat=coords[0] if coords else None,
            lng=coords[1] if coords else None,
            accuracy=acc,
            source="complaint",
        )
        log_activity(
            conn, "complaint_fixed",
            f"Complaint #{complaint_id} fixed by {actor}: {row['title']}",
            customer_id=int(row["customer_id"]), actor=actor,
        )
    from ..whatsapp_notify import notify_complaint

    notify_complaint(
        "fixed",
        complaint_id=complaint_id,
        actor=actor,
        resolution=resolution.strip() or row["last_note"] or "Fixed",
    )
    return _redirect(
        "/complaints",
        flash=f"Marked fixed by {actor}. The office will see this on the dashboard.",
    )


# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #

PACKAGE_LIST_LIMIT = 250


@router.get("/packages")
async def packages_list(request: Request):
    params = request.query_params
    view = params.get("view", "in_use")
    in_use = {"in_use": True, "unused": False}.get(view)
    query = params.get("q", "")
    provider = params.get("provider", "")
    export = wants_csv(request)

    with connection() as conn:
        rows = repo.list_packages(
            conn,
            provider=provider,
            query=query,
            in_use=in_use,
            limit=None if export else PACKAGE_LIST_LIMIT,
        )
        counts = repo.count_packages(conn)

    if export:
        return list_exports.packages_csv(rows)
    return _render(
        request,
        "packages.html",
        rows=rows,
        counts=counts,
        view=view,
        provider=provider,
        q=query,
        providers=PROVIDERS,
        truncated=len(rows) >= PACKAGE_LIST_LIMIT,
        limit=PACKAGE_LIST_LIMIT,
        export_href=export_url("/packages", q=query, provider=provider, view=view),
    )


@router.post("/packages/new")
async def package_create(
    provider: str = Form(...),
    name: str = Form(...),
    price: str = Form("0"),
    validity_days: str = Form(""),
    gst_percentage: str = Form("0"),
    notes: str = Form(""),
):
    provider = provider.strip().lower()
    plan_name = name.strip()
    price_paise = to_paise(price)
    validity = int(validity_days) if validity_days.strip().isdigit() else 0
    if not validity:
        # Derive the term from the plan name (Railtel " x6" style suffixes).
        validity = price_plan(plan_name, price)["validity_days"]

    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
                "gst_percentage, active, notes, created_at) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    provider,
                    plan_name,
                    price_paise,
                    validity,
                    billing.billing_type_for(provider),
                    float(gst_percentage or 0),
                    notes.strip(),
                    now_iso(),
                ),
            )
    except Exception as exc:
        return _redirect("/packages", flash=f"Could not add plan: {exc}", level="err")
    return _redirect("/packages", flash=f"Plan '{plan_name}' added.")


@router.post("/packages/{package_id}/edit")
async def package_edit(
    package_id: int,
    name: str = Form(...),
    price: str = Form("0"),
    validity_days: str = Form("30"),
    gst_percentage: str = Form("0"),
    active: str = Form(""),
    notes: str = Form(""),
):
    with transaction() as conn:
        conn.execute(
            "UPDATE packages SET name = ?, price_paise = ?, validity_days = ?, gst_percentage = ?, "
            "active = ?, notes = ? WHERE id = ?",
            (
                name.strip(),
                to_paise(price),
                int(validity_days) if validity_days.strip().isdigit() else 30,
                float(gst_percentage or 0),
                1 if (active or "").strip().lower() in {"1", "on", "true", "yes"} else 0,
                notes.strip(),
                package_id,
            ),
        )
    return _redirect("/packages", flash="Plan saved.")


@router.post("/packages/{package_id}/delete")
async def package_delete(package_id: int):
    with transaction() as conn:
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM connections WHERE package_id = ?", (package_id,)
        ).fetchone()["n"]
        if used:
            return _redirect(
                "/packages",
                flash=f"{used} connection(s) still use this plan. Move them first.",
                level="err",
            )
        conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))
    return _redirect("/packages", flash="Plan deleted.")


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #

@router.get("/activity")
async def activity_page(request: Request):
    export = wants_csv(request)
    params = request.query_params
    viewer = request.state.agent or {}
    is_owner = auth.sees_all_settlements(viewer)
    day_to = (params.get("to") or "").strip() or today().strftime("%Y-%m-%d")
    day_from = (params.get("from") or "").strip() or add_days(today(), -6).strftime("%Y-%m-%d")
    group = (params.get("group") or "").strip()
    try:
        agent_filter = int(params.get("agent_id") or 0) or None
    except ValueError:
        agent_filter = None
    if not is_owner:
        agent_filter = int(viewer.get("id") or 0) or None
    with connection() as conn:
        rows = repo.activity_log_rows(
            conn,
            agent_id=agent_filter,
            group=group,
            day_from=day_from,
            day_to=day_to,
            limit=EXPORT_ROW_LIMIT if export else 500,
        )
        agents = repo.list_agents(conn, active_only=False) if is_owner else []
    if export:
        return list_exports.activity_csv(rows, with_location=is_owner)
    return _render(
        request,
        "activity.html",
        rows=rows,
        agents=agents,
        groups=repo.ACTIVITY_GROUPS,
        is_owner=is_owner,
        agent_filter=agent_filter,
        group=group,
        day_from=day_from,
        day_to=day_to,
        export_href=export_url(
            "/activity", agent_id=agent_filter if is_owner else None,
            group=group, **{"from": day_from, "to": day_to},
        ),
    )


# --------------------------------------------------------------------------- #
# Settings: agents + Bix sync
# --------------------------------------------------------------------------- #

@router.get("/settings")
async def settings_home(request: Request):
    dest = "/settings/agents" if auth.can(request.state.agent, "agents") else "/settings/bix"
    if not auth.can(request.state.agent, "agents") and not auth.can(request.state.agent, "bix_sync"):
        return _forbidden()
    return RedirectResponse(dest, status_code=303)


@router.get("/settings/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    with connection() as conn:
        agents = [auth.agent_from_row(row)
                  for row in conn.execute("SELECT * FROM agents ORDER BY role, name")]
        complaint_group = get_setting(conn, "complaint_whatsapp_group", "")
        area_options = repo.agent_area_options(conn)
        agent_areas = repo.list_all_agent_areas(conn)
    return _render(
        request,
        "settings_agents.html",
        agents=agents,
        permissions=auth.PERMISSIONS,
        provider_scopes=auth.PROVIDER_SCOPES,
        settings_tab="agents",
        complaint_whatsapp_group=complaint_group or settings.complaint_whatsapp_group,
        area_options=area_options,
        agent_areas=agent_areas,
    )


@router.post("/settings/complaint-whatsapp")
async def settings_complaint_whatsapp(request: Request, group_name: str = Form("")):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    with transaction() as conn:
        set_setting(conn, "complaint_whatsapp_group", group_name.strip())
        log_activity(
            conn,
            "settings_updated",
            "Complaint WhatsApp group updated",
            actor=(request.state.agent or {}).get("name"),
        )
    return _redirect("/complaints", flash="Complaint WhatsApp group saved.")


def _checked_permissions(form) -> list[str]:
    return [str(value) for value in form.getlist("perm") if str(value) in auth.PERM_KEYS]


def _checked_areas(form) -> list[str]:
    return [str(value).strip() for value in form.getlist("area") if str(value).strip()]


@router.post("/settings/agents/new")
async def agent_create(
    request: Request,
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("collector"),
    phone: str = Form(""),
):
    name = name.strip()
    username = username.strip()
    role = role.strip() if role.strip() in ("admin", "collector") else "collector"
    if not name or not username or not password:
        return _redirect("/settings/agents", flash="Name, username and password are required.", level="err")
    policy = auth.password_policy_error(password)
    if policy:
        return _redirect("/settings/agents", flash=policy, level="err")
    form = await request.form()
    extra = _checked_permissions(form)
    perms = list(auth.PERM_KEYS) if role == "admin" else auth.permissions_for_role(role, extra)
    provider_scope = auth.parse_provider_scope(form.get("provider_scope"))
    stamp = now_iso()
    areas = _checked_areas(form)
    try:
        with transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO agents(name, username, password_hash, role, permissions, phone, "
                "provider_scope, active, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    name,
                    username,
                    auth.hash_password(password),
                    role,
                    json.dumps(perms),
                    phone.strip(),
                    provider_scope,
                    stamp,
                    stamp,
                ),
            )
            repo.set_agent_areas(conn, int(cursor.lastrowid), areas)
            log_activity(conn, "agent_created", f"Agent {username} created ({role})",
                         actor=(request.state.agent or {}).get("name"))
    except Exception:
        return _redirect("/settings/agents", flash="That username is already taken.", level="err")
    return _redirect("/settings/agents", flash=f"Agent {username} created.")


@router.post("/settings/agents/{agent_id}/edit")
async def agent_edit(
    request: Request,
    agent_id: int,
    name: str = Form(...),
    role: str = Form("collector"),
    active: str = Form(""),
    password: str = Form(""),
    phone: str = Form(""),
):
    me = request.state.agent or {}
    role = role.strip() if role.strip() in ("admin", "collector") else "collector"
    form = await request.form()
    extra = _checked_permissions(form)
    perms = list(auth.PERM_KEYS) if role == "admin" else extra
    if not perms and role != "admin":
        perms = auth.ROLE_DEFAULTS["collector"]
    is_active = 1 if active in {"1", "on", "true", "yes"} else 0
    if me.get("id") == agent_id:
        is_active = 1
        role = "admin" if me.get("role") == "admin" else role
    provider_scope = auth.parse_provider_scope(form.get("provider_scope"))
    stamp = now_iso()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            return _redirect("/settings/agents", flash="Agent not found.", level="err")
        fields = (
            "name = ?, role = ?, permissions = ?, phone = ?, provider_scope = ?, active = ?, updated_at = ?"
        )
        values = [
            name.strip() or row["name"],
            role,
            json.dumps(perms),
            phone.strip(),
            provider_scope,
            is_active,
            stamp,
        ]
        if password.strip():
            policy = auth.password_policy_error(password.strip())
            if policy:
                return _redirect("/settings/agents", flash=policy, level="err")
            fields += ", password_hash = ?"
            values.append(auth.hash_password(password.strip()))
        values.append(agent_id)
        conn.execute(f"UPDATE agents SET {fields} WHERE id = ?", values)
        repo.set_agent_areas(conn, agent_id, _checked_areas(form))
        log_activity(conn, "agent_updated", f"Agent {row['username']} updated",
                     actor=me.get("name"))
    scope_label = next(
        (label for key, label in auth.PROVIDER_SCOPES if key == provider_scope),
        provider_scope,
    )
    return _redirect("/settings/agents", flash=f"Agent saved — customer access: {scope_label}.")


@router.get("/settings/upi", response_class=HTMLResponse)
async def settings_upi_page(request: Request):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    with connection() as conn:
        vpa = public_pay.effective_upi_vpa(conn)
        payee = public_pay.effective_payee_name(conn)
        enabled = public_pay.public_pay_enabled(conn)
    if settings.public_base_url:
        public_url = f"{settings.public_base_url.rstrip('/')}/pay"
    else:
        public_url = f"{str(request.base_url).rstrip('/')}/pay"
    public_qr_url = (
        "https://api.qrserver.com/v1/create-qr-code/?size=280x280&margin=10&data="
        + quote(public_url, safe="")
    )
    return _render(
        request,
        "settings_upi.html",
        settings_tab="upi",
        vpa=vpa,
        payee=payee,
        enabled=enabled,
        public_url=public_url,
        public_qr_url=public_qr_url,
    )


@router.post("/settings/upi")
async def settings_upi_save(
    request: Request,
    upi_vpa: str = Form(""),
    upi_payee: str = Form(""),
    enabled: str = Form(""),
):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    portal_on = (enabled or "").strip().lower() not in {"", "0", "false", "off", "no"}
    with transaction() as conn:
        public_pay.save_public_pay_settings(
            conn,
            upi_vpa=upi_vpa,
            upi_payee=upi_payee,
            enabled=portal_on,
        )
        log_activity(
            conn,
            "settings_updated",
            "Customer pay UPI settings updated",
            actor=(request.state.agent or {}).get("name"),
        )
    return _redirect("/settings/upi", flash="UPI and pay portal settings saved.")


@router.get("/settings/whatsapp-templates", response_class=HTMLResponse)
async def settings_wa_templates_page(request: Request):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    from .. import wa_templates

    groups = []
    for group_key, group_label in wa_templates.GROUP_LABELS.items():
        items = [
            {
                **spec,
                "text": wa_templates.get_template(spec["key"]),
                "customised": wa_templates.is_customised(spec["key"]),
                "preview": wa_templates.preview(spec["key"]),
            }
            for spec in wa_templates.TEMPLATES
            if spec["group"] == group_key
        ]
        groups.append({"key": group_key, "label": group_label, "templates": items})
    return _render(
        request,
        "settings_whatsapp_templates.html",
        settings_tab="wa_templates",
        groups=groups,
        sample_values=wa_templates.SAMPLE_VALUES,
    )


@router.post("/settings/whatsapp-templates")
async def settings_wa_templates_save(request: Request):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    from .. import wa_templates

    form = await request.form()
    action = str(form.get("action") or "save")
    actor = (request.state.agent or {}).get("name")
    dest = "/settings/whatsapp-templates"

    if action.startswith("reset:"):
        key = action.split(":", 1)[1]
        if key not in wa_templates.TEMPLATES_BY_KEY:
            return _redirect(dest, flash="Unknown template.", level="err")
        label = wa_templates.TEMPLATES_BY_KEY[key]["label"]
        with transaction() as conn:
            wa_templates.reset_template(conn, key)
            log_activity(conn, "settings_updated", f"WhatsApp template reset: {label}", actor=actor)
        wa_templates.clear_cache()
        return _redirect(dest, flash=f"“{label}” restored to the default text.")

    changed: list[str] = []
    warnings: list[str] = []
    with transaction() as conn:
        for spec in wa_templates.TEMPLATES:
            field = f"tpl__{spec['key']}"
            if field not in form:
                continue
            text = str(form.get(field) or "")
            if wa_templates._normalise(text) == wa_templates._normalise(
                wa_templates.get_template(spec["key"])
            ):
                continue
            bad = wa_templates.unknown_placeholders(spec["key"], text)
            if bad:
                warnings.append(
                    f"“{spec['label']}” not saved — unknown "
                    + ", ".join("{" + b + "}" for b in bad)
                )
                continue
            wa_templates.save_template(conn, spec["key"], text)
            changed.append(spec["label"])
        if changed:
            log_activity(
                conn,
                "settings_updated",
                "WhatsApp templates updated: " + ", ".join(changed),
                actor=actor,
            )
    wa_templates.clear_cache()
    if warnings:
        msg = "; ".join(warnings)
        if changed:
            msg = "Saved " + ", ".join(changed) + ". " + msg
        return _redirect(dest, flash=msg, level="err")
    if not changed:
        return _redirect(dest, flash="No changes to save.")
    return _redirect(dest, flash="Saved: " + ", ".join(changed) + ".")


@router.get("/settings/hathway-expiry", response_class=HTMLResponse)
async def hathway_expiry_page(request: Request):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    with connection() as conn:
        batches = conn.execute(
            "SELECT id, filename, status, row_count, created_by, created_at, applied_at, summary_json "
            "FROM hathway_expiry_batches ORDER BY id DESC LIMIT 12"
        ).fetchall()
        latest = conn.execute(
            "SELECT * FROM hathway_expiry_batches WHERE status = 'preview' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        preview_rows = []
        if latest:
            try:
                preview_rows = json.loads(latest["payload_json"] or "[]")
            except ValueError:
                preview_rows = []
        preview_summary = hathway_expiry_sync.summarize(preview_rows) if preview_rows else None
        applied_summaries = []
        for batch in batches:
            extra = {}
            if batch["summary_json"]:
                try:
                    extra = json.loads(batch["summary_json"])
                except ValueError:
                    extra = {}
            applied_summaries.append({**dict(batch), "summary": extra})
    return _render(
        request,
        "settings_hathway_expiry.html",
        batches=applied_summaries,
        preview=latest,
        preview_rows=preview_rows[:80],
        preview_total=len(preview_rows),
        preview_summary=preview_summary,
        settings_tab="hathway_expiry",
    )


@router.post("/settings/hathway-expiry/upload")
async def hathway_expiry_upload(request: Request, file: UploadFile = File(...)):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    filename = (file.filename or "PlanExpiry.xls").strip()
    suffix = Path(filename).suffix.lower() or ".xls"
    raw = await file.read()
    if not raw:
        return _redirect("/settings/hathway-expiry", flash="The file was empty.", level="err")
    tmp = Path(tempfile.mkdtemp(prefix="vkp_hwexp_")) / f"upload{suffix}"
    tmp.write_bytes(raw)
    try:
        items = hathway_expiry_sync.parse_plan_expiry_file(tmp)
    except ValueError as exc:
        return _redirect("/settings/hathway-expiry", flash=str(exc), level="err")
    except Exception as exc:
        return _redirect("/settings/hathway-expiry", flash=f"Could not read that file: {exc}", level="err")

    with transaction() as conn:
        preview_rows = hathway_expiry_sync.preview(conn, items)
        conn.execute(
            "INSERT INTO hathway_expiry_batches(filename, status, row_count, payload_json, "
            "created_by, created_at) VALUES(?, 'preview', ?, ?, ?, ?)",
            (
                filename,
                len(preview_rows),
                json.dumps(preview_rows),
                (request.state.agent or {}).get("name"),
                now_iso(),
            ),
        )
    counts = hathway_expiry_sync.summarize(preview_rows)
    return _redirect(
        "/settings/hathway-expiry",
        flash=(
            f"Read {counts['total']} STB(s): {counts['expiry_change']} expiry to update, "
            f"{counts['pack_change']} pack(s) to set, {counts['unchanged']} already matching, "
            f"{counts['unmatched']} not on this platform. Al-la-carte ignored. "
            "Bix billing plans are not changed."
        ),
    )


@router.post("/settings/hathway-expiry/{batch_id}/apply")
async def hathway_expiry_apply(request: Request, batch_id: int):
    if not auth.can(request.state.agent, "agents"):
        return _forbidden()
    actor = (request.state.agent or {}).get("name")
    with transaction() as conn:
        batch = conn.execute(
            "SELECT * FROM hathway_expiry_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None or batch["status"] != "preview":
            return _redirect(
                "/settings/hathway-expiry",
                flash="That preview is no longer waiting.",
                level="err",
            )
        try:
            rows = json.loads(batch["payload_json"] or "[]")
        except ValueError:
            return _redirect(
                "/settings/hathway-expiry",
                flash="The preview data is damaged.",
                level="err",
            )
        summary = hathway_expiry_sync.apply_preview(conn, rows, actor=actor)
        conn.execute(
            "UPDATE hathway_expiry_batches SET status = 'applied', applied_at = ?, summary_json = ? "
            "WHERE id = ?",
            (now_iso(), json.dumps(summary), batch_id),
        )
    return _redirect(
        "/settings/hathway-expiry",
        flash=(
            f"Hathway expiry: {summary['updated']} STB(s) updated "
            f"({summary['expiry_change']} expiry, {summary['pack_change']} pack). "
            f"{summary['unmatched']} left unmatched. Customer plans and bills were not changed."
        ),
    )


@router.get("/settings/bix", response_class=HTMLResponse)
async def bix_page(request: Request):
    master_path = bix_sync.master_accounts_path()
    master_exists = master_path.is_file()
    master_count = 0
    if master_exists:
        try:
            master_count = len(bix_sync.load_master_items())
        except Exception:
            master_count = 0
    with connection() as conn:
        batches = conn.execute(
            "SELECT id, filename, status, row_count, created_by, created_at, applied_at, summary_json "
            "FROM bix_sync_batches ORDER BY id DESC LIMIT 12"
        ).fetchall()
        latest = conn.execute(
            "SELECT * FROM bix_sync_batches WHERE status = 'preview' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        preview_rows = []
        if latest:
            try:
                preview_rows = json.loads(latest["payload_json"] or "[]")
            except ValueError:
                preview_rows = []
    return _render(
        request,
        "settings_bix.html",
        batches=batches,
        preview=latest,
        preview_rows=preview_rows[:80],
        preview_total=len(preview_rows),
        settings_tab="bix",
        master_path=str(master_path),
        master_exists=master_exists,
        master_count=master_count,
    )


@router.post("/settings/bix/upload")
async def bix_upload(request: Request, file: UploadFile = File(...)):
    filename = (file.filename or "bix.xls").strip()
    suffix = Path(filename).suffix.lower() or ".xls"
    raw = await file.read()
    if not raw:
        return _redirect("/settings/bix", flash="The file was empty.", level="err")
    tmp = Path(tempfile.mkdtemp(prefix="vkp_bix_")) / f"upload{suffix}"
    tmp.write_bytes(raw)
    try:
        items = bix_sync.parse_bix_customers(tmp)
    except ValueError as exc:
        return _redirect("/settings/bix", flash=str(exc), level="err")
    except Exception as exc:
        return _redirect("/settings/bix", flash=f"Could not read that file: {exc}", level="err")

    with transaction() as conn:
        preview_rows = bix_sync.preview(conn, items)
        conn.execute(
            "INSERT INTO bix_sync_batches(filename, status, row_count, payload_json, "
            "created_by, created_at) VALUES(?, 'preview', ?, ?, ?, ?)",
            (
                filename,
                len(preview_rows),
                json.dumps(preview_rows),
                (request.state.agent or {}).get("name"),
                now_iso(),
            ),
        )
    adjust = sum(1 for r in preview_rows if r["action"] == "adjust")
    create = sum(1 for r in preview_rows if r["action"] == "create")
    same = sum(1 for r in preview_rows if r["action"] == "unchanged")
    return _redirect(
        "/settings/bix",
        flash=f"Read {len(preview_rows)} Bix customer(s): {adjust} due to update, "
              f"{same} already matching, {create} not in this platform.",
    )


@router.post("/settings/bix/{batch_id}/apply")
async def bix_apply(
    request: Request,
    batch_id: int,
    create_missing: str = Form(""),
):
    create = create_missing in {"1", "on", "true", "yes"}
    actor = (request.state.agent or {}).get("name")
    with transaction() as conn:
        batch = conn.execute(
            "SELECT * FROM bix_sync_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None or batch["status"] != "preview":
            return _redirect("/settings/bix", flash="That preview is no longer waiting.", level="err")
        try:
            rows = json.loads(batch["payload_json"] or "[]")
        except ValueError:
            return _redirect("/settings/bix", flash="The preview data is damaged.", level="err")
        summary = bix_sync.apply_preview(conn, rows, create_missing=create, actor=actor)
        conn.execute(
            "UPDATE bix_sync_batches SET status = 'applied', applied_at = ?, summary_json = ? "
            "WHERE id = ?",
            (now_iso(), json.dumps(summary), batch_id),
        )
    return _redirect(
        "/settings/bix",
        flash=(
            f"Bix dues: {summary['adjusted']} updated, {summary['unchanged']} already matching, "
            f"{summary['skipped']} left out. Hathway boxes were not added or removed. "
            f"History linked for {summary.get('history_matched', 0)}."
        ),
    )


@router.post("/settings/bix/sync-master/preview")
async def bix_sync_master_preview(request: Request):
    try:
        items = bix_sync.load_master_items()
    except FileNotFoundError as exc:
        return _redirect("/settings/bix", flash=str(exc), level="err")
    with transaction() as conn:
        preview_rows = bix_sync.preview(conn, items)
        conn.execute(
            "INSERT INTO bix_sync_batches(filename, status, row_count, payload_json, "
            "created_by, created_at) VALUES(?, 'preview', ?, ?, ?, ?)",
            (
                "bix_accounts.csv (master)",
                len(preview_rows),
                json.dumps(preview_rows),
                (request.state.agent or {}).get("name"),
                now_iso(),
            ),
        )
    adjust = sum(1 for r in preview_rows if r["action"] == "adjust")
    create = sum(1 for r in preview_rows if r["action"] == "create")
    same = sum(1 for r in preview_rows if r["action"] == "unchanged")
    return _redirect(
        "/settings/bix",
        flash=f"Master file: {len(preview_rows)} household(s) — {adjust} due update, "
              f"{same} same, {create} not on platform.",
    )


@router.post("/settings/bix/sync-master/apply")
async def bix_sync_master_apply(
    request: Request,
    create_missing: str = Form(""),
):
    create = create_missing in {"1", "on", "true", "yes"}
    actor = (request.state.agent or {}).get("name")
    with transaction() as conn:
        batch = conn.execute(
            "SELECT * FROM bix_sync_batches WHERE filename = ? AND status = 'preview' "
            "ORDER BY id DESC LIMIT 1",
            ("bix_accounts.csv (master)",),
        ).fetchone()
        if batch is None:
            return _redirect(
                "/settings/bix",
                flash="Preview the master file first.",
                level="err",
            )
        try:
            rows = json.loads(batch["payload_json"] or "[]")
        except ValueError:
            return _redirect("/settings/bix", flash="The preview data is damaged.", level="err")
        summary = bix_sync.apply_preview(conn, rows, create_missing=create, actor=actor)
        conn.execute(
            "UPDATE bix_sync_batches SET status = 'applied', applied_at = ?, summary_json = ? "
            "WHERE id = ?",
            (now_iso(), json.dumps(summary), batch["id"]),
        )
    return _redirect(
        "/settings/bix",
        flash=(
            f"Master dues: {summary['adjusted']} updated, {summary['skipped']} left out. "
            f"Hathway boxes were not added or removed. "
            f"History linked for {summary.get('history_matched', 0)}."
        ),
    )


@router.post("/settings/bix/sync-master/now")
async def bix_sync_master_now(
    request: Request,
    create_missing: str = Form(""),
):
    """Preview + apply the on-disk Bix master in one step."""
    create = create_missing in {"1", "on", "true", "yes"}
    actor = (request.state.agent or {}).get("name")
    try:
        items = bix_sync.load_master_items()
    except FileNotFoundError as exc:
        return _redirect("/settings/bix", flash=str(exc), level="err")
    with transaction() as conn:
        preview_rows = bix_sync.preview(conn, items)
        summary = bix_sync.apply_preview(conn, preview_rows, create_missing=create, actor=actor)
        conn.execute(
            "INSERT INTO bix_sync_batches(filename, status, row_count, payload_json, "
            "summary_json, created_by, created_at, applied_at) "
            "VALUES(?, 'applied', ?, ?, ?, ?, ?, ?)",
            (
                "bix_accounts.csv (master)",
                len(preview_rows),
                json.dumps(preview_rows),
                json.dumps(summary),
                actor,
                now_iso(),
                now_iso(),
            ),
        )
    return _redirect(
        "/settings/bix",
        flash=(
            f"Bix dues for households already here: {summary['adjusted']} updated, "
            f"{summary['skipped']} left out. Hathway boxes were not added or removed."
        ),
    )


@router.get("/settings/bix-history", response_class=HTMLResponse)
async def bix_history_page(request: Request):
    cookies_path = settings.bix_history_db.parent / "cookies.json"
    with connection() as conn:
        stats = bix_history.imported_stats(conn)
        unmatched = bix_history.unmatched_customers(conn)
        schedule = bix_schedule.history_schedule(conn)
    return _render(
        request,
        "settings_bix_history.html",
        archive=bix_history.archive_peek(),
        stats=stats,
        unmatched=unmatched,
        schedule=schedule,
        cookies_exists=cookies_path.is_file(),
        cookies_path=str(cookies_path),
        settings_tab="bix_history",
    )


def _import_history(path: Path, actor: str) -> RedirectResponse:
    try:
        with transaction() as conn:
            summary = bix_history.import_archive(conn, path, actor=actor)
    except FileNotFoundError as exc:
        return _redirect("/settings/bix-history", flash=str(exc), level="err")
    except ValueError as exc:
        return _redirect("/settings/bix-history", flash=str(exc), level="err")
    except Exception as exc:
        return _redirect("/settings/bix-history", flash=f"Could not import: {exc}", level="err")
    return _redirect(
        "/settings/bix-history",
        flash=(
            f"Imported {summary['txns_new']} new row(s) of {summary['txns_seen']} in the file. "
            f"{summary['matched']} customer(s) matched by phone, "
            f"{summary['unmatched']} not on this platform."
        ),
    )


@router.post("/settings/bix-history/import")
async def bix_history_import(request: Request):
    actor = (request.state.agent or {}).get("name") or ""
    return _import_history(bix_history.default_archive_path(), actor)


@router.post("/settings/bix-history/upload")
async def bix_history_upload(request: Request, file: UploadFile = File(...)):
    filename = (file.filename or "vk_digital_history.db").strip()
    suffix = Path(filename).suffix.lower()
    if suffix not in {".db", ".sqlite", ".sqlite3"}:
        return _redirect("/settings/bix-history", flash="Upload the Bix history .db file.", level="err")
    raw = await file.read()
    if not raw:
        return _redirect("/settings/bix-history", flash="The file was empty.", level="err")
    tmp = Path(tempfile.mkdtemp(prefix="vkp_bixh_")) / f"upload{suffix}"
    tmp.write_bytes(raw)
    actor = (request.state.agent or {}).get("name") or ""
    return _import_history(tmp, actor)


@router.post("/settings/bix-history/schedule")
async def bix_history_schedule_save(
    request: Request,
    enabled: str = Form(""),
    hour: str = Form("4"),
    auto_extract: str = Form(""),
):
    with transaction() as conn:
        bix_schedule.save_history_schedule(conn, {
            "bix_history_schedule_enabled": "1" if enabled else "0",
            "bix_history_schedule_hour": hour.strip() or "4",
            "bix_history_auto_extract": "1" if auto_extract else "0",
        })
    return _redirect("/settings/bix-history", flash="Bix history schedule saved.")


@router.post("/settings/bix-history/run-now")
async def bix_history_run_now(
    request: Request,
    extract: str = Form(""),
):
    actor = (request.state.agent or {}).get("name") or "manual"
    do_extract = extract in {"1", "on", "true", "yes"}
    summary = bix_schedule.run_history_sync(actor=actor, force_extract=do_extract)
    parts = []
    ext = summary.get("extract") or {}
    if do_extract:
        if ext.get("ok"):
            parts.append(
                f"Extracted {ext.get('customers', 0)} customer(s), "
                f"{ext.get('new_rows', 0)} new row(s)"
            )
        else:
            return _redirect(
                "/settings/bix-history",
                flash=f"Extract failed: {ext.get('error', 'unknown error')}",
                level="err",
            )
    imp = summary.get("import") or {}
    if imp.get("skipped"):
        parts.append("Platform copy already up to date")
    elif imp:
        parts.append(
            f"Imported {imp.get('txns_new', 0)} new row(s), "
            f"{imp.get('matched', 0)} matched by phone"
        )
    return _redirect(
        "/settings/bix-history",
        flash=" · ".join(parts) if parts else "Nothing to do.",
    )
