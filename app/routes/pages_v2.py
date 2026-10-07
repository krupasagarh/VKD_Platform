"""Mobile v2 UI — parallel to classic server-rendered pages."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, repo
from ..config import settings
from ..db import connection, transaction
from ..upstream import jobs as job_queue
from ..upstream.providers import PROVIDERS
from .pages import _list_qs, _render
from .pay_portal import pay_admin_context

router = APIRouter(prefix="/v2", redirect_slashes=True)

UI_COOKIE = "vk_ui"
UI_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _render_v2(request: Request, template: str, **context) -> HTMLResponse:
    return _render(request, f"v2/{template}", **context)


@router.get("/switch")
async def switch_to_v2(request: Request):
    """Open the mobile v2 home screen."""
    response = RedirectResponse("/v2/", status_code=303)
    response.set_cookie(
        UI_COOKIE, "v2", max_age=UI_COOKIE_MAX_AGE, httponly=False, samesite="lax", path="/",
    )
    return response


@router.get("/classic")
async def switch_to_classic(request: Request):
    """Return to classic desktop UI."""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(UI_COOKIE, path="/")
    response.set_cookie(UI_COOKIE, "", max_age=0, path="/", samesite="lax")
    return response


@router.get("/", response_class=HTMLResponse)
async def v2_home(request: Request):
    fixed = auth.agent_provider_scope(request.state.agent)
    collector_id = auth.collector_id_for(request.state.agent)
    with connection() as conn:
        stats = repo.dashboard_stats(
            conn, agent_scope=fixed or "", collector_id=collector_id
        )
        collector_stats = (
            repo.agent_collection_stats(conn, collector_id) if collector_id else None
        )

    return _render_v2(
        request,
        "home.html",
        stats=stats,
        fixed_provider=fixed,
        collector_stats=collector_stats,
        hide_collection=bool(collector_stats),
    )


@router.get("/customers", response_class=HTMLResponse)
async def v2_customers(request: Request):
    if not (
        auth.can(request.state.agent, "customers_view")
        or auth.can(request.state.agent, "payments")
    ):
        return RedirectResponse("/", status_code=303)

    params = request.query_params
    q = params.get("q", "")
    view = params.get("view", "")
    if (q or "").strip() and view in ("expired", "expiring"):
        view = ""
    provider = auth.scoped_provider(request.state.agent, params.get("provider", ""))
    area = params.get("area", "")
    status = params.get("status", "")
    railtel_account = params.get("railtel_account", "")
    fixed = auth.agent_provider_scope(request.state.agent)

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
            agent_scope=auth.agent_provider_scope(request.state.agent) or "",
            hide_owner=bool(auth.collector_id_for(request.state.agent)),
        )
        areas = repo.list_area_options(conn)

    filter_q = _list_qs(q=q, provider=provider, status=status, area=area, railtel_account=railtel_account)
    dealer_filter_q = _list_qs(q=q, provider=provider, status=status, area=area)
    list_next = "/v2/customers"
    list_bits = []
    if view:
        list_bits.append(f"view={view}")
    if filter_q:
        list_bits.append(filter_q)
    if list_bits:
        list_next += "?" + "&".join(list_bits)

    return _render_v2(
        request,
        "customers.html",
        result=result,
        q=q,
        view=view,
        provider=provider,
        area=area,
        status=status,
        filter_q=filter_q,
        dealer_filter_q=dealer_filter_q,
        railtel_account=railtel_account,
        providers=PROVIDERS,
        areas=areas,
        none_area=repo.NONE_AREA,
        list_next=list_next,
        fixed_provider=auth.agent_provider_scope(request.state.agent),
    )


@router.get("/menu", response_class=HTMLResponse)
async def v2_menu(request: Request):
    return _render_v2(request, "menu.html")


@router.get("/pay-qr", response_class=HTMLResponse)
async def v2_pay_qr(request: Request):
    if not auth.can(request.state.agent, "payments"):
        return auth.redirect_forbidden("Payments access is required for Pay QR.")
    ctx = pay_admin_context(request)
    return _render_v2(
        request,
        "pay_qr.html",
        active="pay_qr",
        **ctx,
    )


@router.get("/expired-yesterday", response_class=HTMLResponse)
async def v2_expired_yesterday(request: Request):
    provider = (request.query_params.get("provider") or "").strip().lower()
    target = "/v2/expiring-today?when=1d"
    if provider and provider in PROVIDERS:
        target += f"&provider={provider}"
    return RedirectResponse(target, status_code=303)


@router.get("/expiring-today", response_class=HTMLResponse)
async def v2_expiring_today(request: Request):
    if not (
        auth.can(request.state.agent, "customers_view")
        or auth.can(request.state.agent, "payments")
        or auth.can(request.state.agent, "portal_actions")
    ):
        return RedirectResponse("/v2/", status_code=303)

    provider = auth.scoped_provider(request.state.agent, request.query_params.get("provider", ""))
    if provider and provider not in PROVIDERS:
        provider = auth.scoped_provider(request.state.agent, "")
    fixed = auth.agent_provider_scope(request.state.agent)

    when, _window = repo.parse_expiry_when(request.query_params.get("when") or "")
    q = (request.query_params.get("q") or "").strip()

    if when == "term_month" and fixed == "hathway":
        when = "tonight"
        provider = "hathway"
    elif when == "term_month":
        provider = "railtel"

    collector_id = auth.collector_id_for(request.state.agent)
    with connection() as conn:
        rows = repo.expiry_window_connections(
            conn, when=when, provider=provider, q=q, limit=500, collector_id=collector_id
        )
        stats = repo.expiry_window_stats(
            conn, when=when, provider=provider, collector_id=collector_id
        )
        areas = repo.list_area_options(conn)
        found_customers = []
        if q and not rows:
            found = repo.search_customers(
                conn,
                query=q,
                provider=provider if when != "term_month" else "",
                page=1,
                agent_scope=fixed or "",
                hide_owner=bool(auth.collector_id_for(request.state.agent)),
            )
            found_customers = found.get("rows") or []

    list_next = f"/v2/expiring-today?when={when}"
    if provider:
        list_next += f"&provider={provider}"
    if q:
        list_next += f"&q={quote(q)}"

    return _render_v2(
        request,
        "expiring_today.html",
        rows=rows,
        stats=stats,
        provider=provider,
        providers=PROVIDERS,
        areas=areas,
        list_next=list_next,
        when=when,
        when_label=repo.EXPIRY_WHEN_LABELS.get(when, "Expiring tonight"),
        when_choices=repo.EXPIRY_WHEN_WINDOWS,
        fixed_provider=fixed,
        q=q,
        found_customers=found_customers,
    )


@router.get("/api/expiry-verification")
async def v2_expiry_verification_api(request: Request):
    """Poll verification badges on expired lists without reloading the page."""
    if not auth.is_authenticated(request):
        return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)

    raw = (request.query_params.get("ids") or "").strip()
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()][:100]
    if not ids:
        return JSONResponse({"ok": True, "items": {}})

    with connection() as conn:
        items = repo.connection_verification_status(conn, ids)
    return JSONResponse(
        {
            "ok": True,
            "items": {str(k): v for k, v in items.items()},
            "pending": any(v.get("pending") for v in items.values()),
        }
    )


@router.get("/unpaid-renewals", response_class=HTMLResponse)
async def v2_unpaid_renewals(request: Request):
    if not (
        auth.can(request.state.agent, "payments")
        or auth.can(request.state.agent, "customers_view")
    ):
        return RedirectResponse("/v2/", status_code=303)

    params = request.query_params
    period = (params.get("period") or "month").strip().lower()
    provider = auth.scoped_provider(request.state.agent, params.get("provider", ""))
    if provider and provider not in PROVIDERS:
        provider = auth.scoped_provider(request.state.agent, "")
    fixed = auth.agent_provider_scope(request.state.agent)
    since, until = repo.followup_period_bounds(period)
    collector_id = auth.collector_id_for(request.state.agent)

    with connection() as conn:
        rows = repo.list_collect_later(
            conn,
            kind="renew",
            since=since,
            until=until,
            provider=provider,
            limit=500,
            collector_id=collector_id,
        )
        grouped = repo.group_followup_by_customer(rows)
        stats = repo.collect_later_stats(
            conn, kind="renew", since=since, until=until, provider=provider,
            collector_id=collector_id,
        )
        areas = repo.list_area_options(conn)

    qs_bits = [f"period={period}"]
    if provider:
        qs_bits.append(f"provider={provider}")
    list_next = "/v2/unpaid-renewals?" + "&".join(qs_bits)

    return _render_v2(
        request,
        "unpaid_renewals.html",
        grouped=grouped,
        stats=stats,
        period=period,
        provider=provider,
        providers=PROVIDERS,
        areas=areas,
        list_next=list_next,
        payment_modes=("cash", "upi", "scanner", "owner_upi", "bank"),
        fixed_provider=fixed,
    )


def _railtel_online_job_row(conn, job_id: int | None = None):
    if job_id:
        return conn.execute(
            "SELECT id, status, error, created_at, completed_at FROM upstream_jobs "
            "WHERE id = ? AND provider = 'railtel' AND action = 'online'",
            (job_id,),
        ).fetchone()
    return conn.execute(
        "SELECT id, status, error, created_at, completed_at FROM upstream_jobs "
        "WHERE provider = 'railtel' AND action = 'online' "
        "AND status IN ('awaiting_confirm', 'queued', 'running') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()


@router.get("/railtel-online", response_class=HTMLResponse)
async def v2_railtel_online(request: Request):
    if not auth.can(request.state.agent, "providers"):
        return RedirectResponse("/v2/", status_code=303)
    if auth.agent_provider_scope(request.state.agent) == "hathway":
        return RedirectResponse("/v2/", status_code=303)

    params = request.query_params
    watch_job = int(params.get("job") or 0) or None
    q = (params.get("q") or "").strip()

    with connection() as conn:
        snapshot = repo.latest_railtel_online(conn)
        open_job = _railtel_online_job_row(conn, watch_job)
        if open_job is None and watch_job:
            open_job = _railtel_online_job_row(conn)
        rows = (
            repo.railtel_online_rows(conn, int(snapshot["id"]), q=q, view="known")
            if snapshot
            else []
        )
        rows.sort(key=lambda r: (r.get("customer_name") or r.get("username") or "").lower())

    return _render_v2(
        request,
        "railtel_online.html",
        snapshot=snapshot,
        rows=rows,
        q=q,
        open_job=open_job,
        watch_job_id=watch_job or (int(open_job["id"]) if open_job else None),
        live_mode=settings.is_live,
    )


@router.post("/railtel-online/refresh")
async def v2_railtel_online_refresh(request: Request):
    if not auth.can(request.state.agent, "providers"):
        return RedirectResponse("/v2/", status_code=303)
    actor = None
    if request.state.agent:
        actor = auth.job_requested_by(request.state.agent)
    with transaction() as conn:
        job_id = job_queue.enqueue_provider_job(
            conn,
            provider="railtel",
            action="online",
            requested_by=actor or settings.operator,
        )
    return RedirectResponse(f"/v2/railtel-online?job={job_id}", status_code=303)


@router.get("/api/railtel-online")
async def v2_railtel_online_api(request: Request):
    if not auth.is_authenticated(request):
        return JSONResponse({"ok": False, "error": "not signed in"}, status_code=401)
    if not auth.can(request.state.agent, "providers"):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    job_id = int(request.query_params.get("job") or 0) or None
    with connection() as conn:
        job = _railtel_online_job_row(conn, job_id) if job_id else _railtel_online_job_row(conn)
        snapshot = repo.latest_railtel_online(conn)
        rows = []
        if snapshot:
            rows = repo.railtel_online_rows(conn, int(snapshot["id"]), view="known")
            rows.sort(key=lambda r: (r.get("customer_name") or r.get("username") or "").lower())

    pending = bool(job and job["status"] in ("awaiting_confirm", "queued", "running"))
    payload = {
        "ok": True,
        "pending": pending,
        "job": dict(job) if job else None,
        "snapshot": {
            "fetched_at": snapshot["fetched_at"] if snapshot else "",
            "row_count": int(snapshot["row_count"] or 0) if snapshot else 0,
            "online_count": int(snapshot["online_count"] or 0) if snapshot else 0,
        }
        if snapshot
        else None,
        "rows": [
            {
                "customer_id": r.get("customer_id"),
                "customer_name": r.get("customer_name") or "",
                "username": r.get("username") or "",
                "online_duration": r.get("total_time") or "",
                "start_at": r.get("start_at") or r.get("start_time") or "",
            }
            for r in rows
        ],
    }
    return JSONResponse(payload)
