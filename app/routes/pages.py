"""Server-rendered pages and form handlers."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlencode, unquote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, billing, bix_history, bix_sync, field, iptv_plans, list_exports, ott_plans, repo
from ..csv_export import EXPORT_ROW_LIMIT, export_url, wants_csv
from ..config import settings
from ..db import connection, log_activity, transaction
from ..money import now_iso, to_paise, today
from ..plans import price_plan
from ..upstream import jobs as job_queue
from ..upstream.providers import (
    ACCOUNT_ACTIONS,
    ACTION_LABELS,
    DESTRUCTIVE_ACTIONS,
    PROVIDER_ACTIONS,
    PROVIDER_LABELS,
    PROVIDERS,
    id_problem,
    normalise_upstream_id,
)

router = APIRouter()

PAYMENT_MODES = ("cash", "upi", "bank", "gateway", "cheque")
CONNECTION_STATUSES = ("active", "suspended", "inactive", "terminated")
PLAN_TERMS = (
    (30, "30 days (1 month)"),
    (100, "100 days (x3)"),
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

    return templates


def _redirect(path: str, *, flash: str = "", level: str = "ok") -> RedirectResponse:
    if flash:
        query = urlencode({"flash": flash, "level": level})
        path = f"{path}{'&' if '?' in path else '?'}{query}"
    return RedirectResponse(path, status_code=303)


def _safe_next(next_url: str, fallback: str) -> str:
    path = (next_url or "").strip()
    if path in {"/", "/jobs", "/providers", "/iptv", "/ott", "/payments"}:
        return path
    if path.startswith(("/iptv", "/ott", "/customers/", "/field", "/jobs", "/providers", "/payments")):
        base, _, query = path.partition("?")
        if base == "/payments/follow-up" and query in {"kind=manual", "kind=renew"}:
            return f"{base}?{query}"
        return base
    return fallback


def _job_flash(action: str, job_id: int, provider: str) -> str:
    label = ACTION_LABELS.get(action, action)
    if (provider or "").lower() == "iptv":
        if settings.is_live:
            return (
                f"{label} started as job #{job_id}. Stay on this page — "
                f"the yellow bar will ask for the ANT login phone, then the WhatsApp OTP."
            )
        return f"{label} started as job #{job_id} (simulate — no portal, no OTP)."
    if (provider or "").lower() == "ott":
        if settings.is_live:
            if action == "renew":
                return (
                    f"{label} started as job #{job_id}. "
                    "SmartPlay cash Pay will debit the dealer wallet."
                )
            return f"{label} started as job #{job_id}."
        return f"{label} started as job #{job_id} (simulate — no portal)."
    return f"{label} queued as job #{job_id}. Confirm it to run."


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
                "SELECT COUNT(*) AS n FROM upstream_jobs WHERE status = 'running'"
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
            nav["followups"] = conn.execute(
                "SELECT COUNT(*) AS n FROM bills WHERE collect_later = 1 "
                "AND status IN ('pending', 'partial')"
            ).fetchone()["n"]
        except Exception:
            nav["followups"] = 0
        otp_rows = conn.execute(
            "SELECT j.id, j.action, j.provider, j.error, c.name AS customer_name, "
            "cn.upstream_id FROM upstream_jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "LEFT JOIN connections cn ON cn.id = j.connection_id "
            "WHERE j.status = 'awaiting_otp' ORDER BY j.id"
        ).fetchall()
        progress_rows = conn.execute(
            "SELECT j.id, j.action, j.status, j.error, c.name AS customer_name, "
            "cn.upstream_id FROM upstream_jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "LEFT JOIN connections cn ON cn.id = j.connection_id "
            "WHERE j.provider = 'iptv' AND j.status IN ('queued', 'running') "
            "ORDER BY j.id"
        ).fetchall()
        running_rows = conn.execute(
            "SELECT j.id, j.action, j.provider, j.status, c.name AS customer_name, "
            "cn.upstream_id FROM upstream_jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "LEFT JOIN connections cn ON cn.id = j.connection_id "
            "WHERE j.status = 'running' ORDER BY j.id"
        ).fetchall()
    context.setdefault("otp_jobs", [dict(row) for row in otp_rows])
    context.setdefault("iptv_progress", [dict(row) for row in progress_rows])
    context.setdefault("running_jobs", [dict(row) for row in running_rows])
    context.setdefault("flash", request.query_params.get("flash", ""))
    context.setdefault("flash_level", request.query_params.get("level", "ok"))
    context.setdefault("nav", nav)
    context.setdefault("provider_labels", PROVIDER_LABELS)
    context.setdefault("action_labels", ACTION_LABELS)
    context.setdefault("live_mode", settings.is_live)
    context["current_agent"] = agent
    context["can"] = lambda perm: auth.can(agent, perm)
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
        {"error": "", "next": request.query_params.get("next", "/")},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    username: str = Form(""),
    next: str = Form("/"),
):
    with connection() as conn:
        agent = auth.authenticate(conn, username, password)
    if agent is None:
        return _templates().TemplateResponse(
            request,
            "login.html",
            {"error": "Wrong username or password.", "next": next},
            status_code=401,
        )
    target = next if next.startswith("/") else "/"
    if target == "/" and request.cookies.get("vk_ui") == "v2":
        target = "/v2/"
    response = RedirectResponse(target, status_code=303)
    auth.set_login_cookie(response, agent["id"])
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
    if request.cookies.get("vk_ui") == "v2":
        return RedirectResponse("/v2/", status_code=303)
    with connection() as conn:
        stats = repo.dashboard_stats(conn)
        expiring = repo.expiring_connections(conn, limit=15)
        awaiting = repo.list_jobs(conn, status="awaiting_confirm", limit=10)
        awaiting_otp = repo.list_jobs(conn, status="awaiting_otp", limit=10)
        failed = repo.list_jobs(conn, status="failed", limit=5)
        activity = repo.recent_activity(conn, limit=12)
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
        office = field.office_summary(conn, day)
    return _render(
        request,
        "field.html",
        rows=rows,
        day=day,
        office=office,
        everyone=field.sees_everyone(viewer),
        self_id=int(viewer["id"]),
    )


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


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

@router.get("/customers")
async def customers_list(request: Request):
    params = request.query_params
    q = params.get("q", "")
    provider = params.get("provider", "")
    status = params.get("status", "")
    area = params.get("area", "")
    view = params.get("view", "")
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
            page=int(params.get("page", 1) or 1),
            sort=sort,
            late_days=late_days,
            export_all=export,
        )
        areas = repo.list_customer_areas(conn)
    if export:
        return list_exports.customers_csv(result["rows"])
    list_params = {
        "q": q,
        "provider": provider,
        "status": status,
        "area": area,
        "view": view,
        "sort": sort,
        "late_days": late_days or "",
    }
    list_qs = _list_qs(**list_params)
    filter_q = _list_qs(
        q=q, provider=provider, status=status, area=area, sort=sort,
        late_days=late_days or "",
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
        sort=sort,
        late_days=late_days,
        providers=PROVIDERS,
        areas=areas,
        none_area=repo.NONE_AREA,
        filter_q=filter_q,
        list_qs=list_qs,
        export_href=export_url("/customers", **list_params),
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
    job_id = None
    try:
        with transaction() as conn:
            result = iptv_plans.subscribe_iptv(
                conn,
                name=name,
                phone=phone,
                pack=package_name,
                expiry=expiry_date.strip(),
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
        job_id = job_queue.enqueue_provider_job(conn, provider="ott", action="sync")
    return _redirect(
        "/ott",
        flash=_job_flash("sync", job_id, "ott")
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
    try:
        with transaction() as conn:
            result = ott_plans.subscribe_ott(
                conn,
                name=name,
                phone=phone,
                pack=package_name,
                expiry=expiry_date.strip(),
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
    return _render(request, "customer_form.html", customer=None, packages=packages,
                   providers=PROVIDERS)


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
        connections = repo.customer_connections(conn, customer_id)
        ledger = billing.customer_ledger(conn, customer_id)
        bills = repo.customer_bills(conn, customer_id)
        payments = repo.customer_payments(conn, customer_id)
        jobs = repo.customer_jobs(conn, customer_id)
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
        usual_collect_paise = billing.customer_collect_paise(conn, customer_id)
        custom_plan = billing.customer_custom_plan(conn, customer_id)
        plan_cover = billing.customer_cover(
            conn, customer_id, connections=connections, custom_plan=custom_plan
        )

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
        custom_plan=custom_plan,
        plan_cover=plan_cover,
        plan_bundles=billing.PLAN_BUNDLES,
        plan_terms=PLAN_TERMS,
        maps_nav=maps_nav_url(customer["lat"], customer["lng"])
        if customer["lat"] is not None and customer["lng"] is not None else "",
        maps_view=maps_view_url(customer["lat"], customer["lng"])
        if customer["lat"] is not None and customer["lng"] is not None else "",
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
            f"/customers/{customer_id}",
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
        f"/customers/{customer_id}",
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

    stamp = now_iso()
    try:
        with transaction() as conn:
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
                    expiry_date.strip(),
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
    except Exception as exc:  # unique constraint on (provider, upstream_id)
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Could not add connection: {exc}",
            level="err",
        )
    if unknown_plan:
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Connection added, but no plan is named '{package_name.strip()}'. "
                  f"Pick one from the list or add it under Plans, otherwise it cannot be billed.",
            level="err",
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
):
    if not auth.can(request.state.agent, "customers_edit"):
        return _forbidden()
    with transaction() as conn:
        row = conn.execute(
            "SELECT customer_id, provider FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            return _redirect("/customers", flash="Connection not found.", level="err")
        stb = normalise_upstream_id(row["provider"], upstream_id)
        pkg_id, unknown_plan = _resolve_package_id(conn, row["provider"], package_name)
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
                expiry_date.strip(),
                package_name.strip(),
                notes.strip(),
                now_iso(),
                connection_id,
            ),
        )
        customer_id = int(row["customer_id"])
    if unknown_plan:
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Saved, but no plan is named '{package_name.strip()}' — "
                  f"this connection has no price to bill.",
            level="err",
        )
    return _redirect(f"/customers/{customer_id}", flash="Connection saved.")


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
                needs_confirmation=action not in ("status", "download_bill"),
            )
        except job_queue.RenewNotAllowed as exc:
            return _redirect(
                _safe_next(next, f"/customers/{customer_id}"),
                flash=str(exc),
                level="err",
            )
    dest = _safe_next(next, f"/customers/{customer_id}")
    return _redirect(dest, flash=_job_flash(action, job_id, provider))


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
    later_note = " After it succeeds they appear on Payment follow-up."
    return _redirect(
        _safe_next(next, f"/customers/{customer_id}"),
        flash=_job_flash(action, job_id, row["provider"]) + later_note,
    )


# --------------------------------------------------------------------------- #
# Collect payment
# --------------------------------------------------------------------------- #

@router.post("/customers/{customer_id}/check-all")
async def customer_check_all(request: Request, customer_id: int):
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot run provider portal actions.")
    with transaction() as conn:
        job_ids, skipped = job_queue.enqueue_customer_status(conn, customer_id)

    if not job_ids:
        detail = skipped[0] if len(skipped) == 1 else f"{len(skipped)} connection(s) skipped"
        return _redirect(
            f"/customers/{customer_id}",
            flash=f"Nothing to check — {detail}." if skipped else "This customer has no connections.",
            level="err",
        )

    note = f"Checking {len(job_ids)} connection(s) against the provider portals now."
    if skipped:
        note += f" Skipped {len(skipped)}: {skipped[0]}"
    return _redirect(f"/customers/{customer_id}", flash=note)


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
    lat: str = Form(""),
    lng: str = Form(""),
    accuracy: str = Form(""),
):
    if not auth.can(request.state.agent, "payments"):
        return _forbidden("You cannot collect payments.")

    conn_id = int(connection_id) if connection_id.strip() else None
    wants_renew = (renew or "").strip().lower() in {"1", "on", "true", "yes"}
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
            paid_at=paid_at.strip() or now_iso(),
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
        plan_sync = False
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
                    needs_confirmation=True,
                )
            except job_queue.RenewNotAllowed as exc:
                return _redirect(
                    f"/customers/{customer_id}",
                    flash=f"Payment {receipt} saved, but renew was blocked — {exc}",
                    level="err",
                )
        if auth.can(request.state.agent, "portal_actions"):
            for row in conn.execute(
                "SELECT id FROM connections WHERE customer_id = ? AND provider = 'railtel' "
                "AND (last_synced_at IS NULL OR last_synced_at = '')",
                (customer_id,),
            ):
                rid = int(row["id"])
                if wants_renew and conn_id == rid:
                    continue
                job_queue.enqueue_job(
                    conn,
                    connection_id=rid,
                    action="status",
                    payment_id=payment_id,
                    needs_confirmation=True,
                )
                plan_sync = True

    if job_id:
        flash = f"Payment {receipt} saved. {_job_flash('renew', job_id, renew_provider)}"
    elif wants_renew:
        flash = f"Payment {receipt} saved. Pick a connection to queue a renewal."
    else:
        flash = f"Payment {receipt} saved."
    if plan_sync:
        flash += " Queued a one-time Railtel status check to read the live plan."
    return _redirect(f"/customers/{customer_id}", flash=flash)


@router.post("/customers/{customer_id}/balance")
async def customer_set_balance(
    request: Request,
    customer_id: int,
    amount: str = Form(...),
    reason: str = Form(""),
):
    if not (auth.can(request.state.agent, "customers_edit") or auth.can(request.state.agent, "bills")):
        return _forbidden("You cannot change a customer's balance.")
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
                f"Balance set to ₹{target / 100:.2f} "
                f"(was ₹{result['from_paise'] / 100:.2f})"
                + (f" — {reason.strip()}" if reason.strip() else ""),
                customer_id=customer_id,
            )
    if not result["changed"]:
        return _redirect(f"/customers/{customer_id}", flash="Balance is already that amount.")
    return _redirect(
        f"/customers/{customer_id}",
        flash=f"Balance changed from ₹{result['from_paise'] / 100:.2f} "
              f"to ₹{result['to_paise'] / 100:.2f}. It is on the statement.",
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
    if len(digits) == 10:
        row = conn.execute(
            "SELECT * FROM customers WHERE replace(replace(phone, ' ', ''), '-', '') LIKE ? "
            "OR replace(replace(COALESCE(alt_phone, ''), ' ', ''), '-', '') LIKE ? LIMIT 2",
            (f"%{digits}", f"%{digits}"),
        ).fetchall()
        if len(row) == 1:
            return row[0], ""
        if len(row) > 1:
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
    export = wants_csv(request)
    with connection() as conn:
        rows = repo.list_collect_later(conn, kind=kind, limit=EXPORT_ROW_LIMIT if export else 200)
        stats = repo.dashboard_stats(conn)
    if export:
        return list_exports.followup_csv(rows)
    return _render(
        request,
        "payments_followup.html",
        rows=rows,
        stats=stats,
        payments_tab="followup",
        followup_kind=kind,
        export_href=export_url("/payments/follow-up", kind=kind),
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
    if not auth.can(request.state.agent, "portal_actions"):
        return _forbidden("You cannot send portal bills on WhatsApp.")
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
            "provider_labels": PROVIDER_LABELS,
        },
    )


@router.get("/payments/{payment_id}/receipt", response_class=HTMLResponse)
async def payment_receipt(request: Request, payment_id: int):
    with connection() as conn:
        payment = repo.get_payment(conn, payment_id)
        if payment is None:
            return _render(request, "not_found.html", what="Payment")
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
            "SELECT customer_id, receipt_no FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None:
            return _redirect("/payments", flash="Payment not found.", level="err")
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
async def provider_action(provider: str, action: str):
    provider = provider.strip().lower()
    action = action.strip().lower()
    if action not in ACCOUNT_ACTIONS.get(provider, ()):
        return _redirect("/providers", flash="Unknown provider action.", level="err")

    with transaction() as conn:
        job_id = job_queue.enqueue_provider_job(conn, provider=provider, action=action)
    dest = "/providers/online" if action == "online" else "/providers"
    return _redirect(
        dest,
        flash=f"{ACTION_LABELS.get(action, action)} queued as job #{job_id} for "
              f"{PROVIDER_LABELS.get(provider, provider)}.",
    )


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@router.get("/jobs")
async def jobs_list(request: Request):
    status = request.query_params.get("status", "")
    export = wants_csv(request)
    with connection() as conn:
        rows = repo.list_jobs(conn, status=status, limit=EXPORT_ROW_LIMIT if export else 200)
        stats = repo.dashboard_stats(conn)
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
    export = wants_csv(request)
    with connection() as conn:
        result = repo.list_payments(
            conn,
            date_from=date_from,
            date_to=date_to,
            limit=EXPORT_ROW_LIMIT if export else 500,
        )
        stats = repo.dashboard_stats(conn)
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
        who = f" → {assigned_to}" if assigned_to else ""
        log_activity(conn, "complaint_opened", f"Complaint: {title}{who}",
                     customer_id=customer_id, actor=actor)
    return _redirect("/complaints", flash=f"Complaint logged{who}.")


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
    return _redirect("/complaints", flash=f"Assigned to {assigned_to}." if assigned_to else "Assignment cleared.")


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
    with connection() as conn:
        rows = repo.recent_activity(conn, limit=EXPORT_ROW_LIMIT if export else 200)
    if export:
        return list_exports.activity_csv(rows)
    return _render(
        request,
        "activity.html",
        rows=rows,
        export_href=export_url("/activity"),
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
    return _render(
        request,
        "settings_agents.html",
        agents=agents,
        permissions=auth.PERMISSIONS,
        settings_tab="agents",
    )


def _checked_permissions(form) -> list[str]:
    return [str(value) for value in form.getlist("perm") if str(value) in auth.PERM_KEYS]


@router.post("/settings/agents/new")
async def agent_create(
    request: Request,
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("collector"),
):
    name = name.strip()
    username = username.strip()
    role = role.strip() if role.strip() in ("admin", "collector") else "collector"
    if not name or not username or not password:
        return _redirect("/settings/agents", flash="Name, username and password are required.", level="err")
    form = await request.form()
    extra = _checked_permissions(form)
    perms = list(auth.PERM_KEYS) if role == "admin" else auth.permissions_for_role(role, extra)
    stamp = now_iso()
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO agents(name, username, password_hash, role, permissions, active, "
                "created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
                (name, username, auth.hash_password(password), role, json.dumps(perms), stamp, stamp),
            )
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
    stamp = now_iso()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            return _redirect("/settings/agents", flash="Agent not found.", level="err")
        fields = "name = ?, role = ?, permissions = ?, active = ?, updated_at = ?"
        values = [name.strip() or row["name"], role, json.dumps(perms), is_active, stamp]
        if password.strip():
            fields += ", password_hash = ?"
            values.append(auth.hash_password(password.strip()))
        values.append(agent_id)
        conn.execute(f"UPDATE agents SET {fields} WHERE id = ?", values)
        log_activity(conn, "agent_updated", f"Agent {row['username']} updated",
                     actor=me.get("name"))
    return _redirect("/settings/agents", flash="Agent saved.")


@router.get("/settings/bix", response_class=HTMLResponse)
async def bix_page(request: Request):
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
            f"Bix aligned: {summary['adjusted']} due(s) set, {summary['created']} created, "
            f"{summary.get('stbs_added', 0)} STB(s) added, {summary.get('stbs_moved', 0)} moved, "
            f"{summary.get('stbs_removed', 0)} extra removed. "
            f"{summary['skipped']} skipped (mostly Railtel). "
            f"History linked for {summary.get('history_matched', 0)}."
        ),
    )


@router.get("/settings/bix-history", response_class=HTMLResponse)
async def bix_history_page(request: Request):
    with connection() as conn:
        stats = bix_history.imported_stats(conn)
        unmatched = bix_history.unmatched_customers(conn)
    return _render(
        request,
        "settings_bix_history.html",
        archive=bix_history.archive_peek(),
        stats=stats,
        unmatched=unmatched,
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
