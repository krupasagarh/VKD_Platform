"""FastAPI application for VK Platform."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from .config import APP_DIR, DEMO_PORT, is_demo_instance, settings
from .db import init_db
from .messaging import (
    balance_reminder_whatsapp_url,
    expired_whatsapp_url,
    expiring_tonight_whatsapp_url,
    invoice_whatsapp_url,
    payment_received_whatsapp_url,
    renew_followup_whatsapp_url,
    renewed_whatsapp_url,
)
from .money import (
    days_until,
    fmt_date,
    fmt_date_display,
    fmt_date_input,
    fmt_datetime_human,
    fmt_receipt_datetime,
    fmt_relative,
    fmt_rupees,
    from_paise,
    rupees_in_words,
    today as calendar_today,
    whole_rupees,
)
from .upstream import jobs as job_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("vk_platform")

TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["rupees"] = fmt_rupees
templates.env.filters["rupees_plain"] = lambda p: str(whole_rupees(p))
templates.env.filters["nice_date"] = fmt_date_display
templates.env.filters["date_input"] = fmt_date_input
templates.env.filters["nice_datetime"] = fmt_datetime_human
templates.env.filters["relative_time"] = fmt_relative
templates.env.filters["days_until"] = days_until
templates.env.filters["receipt_datetime"] = fmt_receipt_datetime
templates.env.filters["rupees_words"] = rupees_in_words

from .upstream.providers import provider_label, providers_csv_label

templates.env.filters["provider_label"] = provider_label
templates.env.filters["providers_csv_label"] = providers_csv_label
def _whatsapp_expired_filter(name, phone, provider=None, providers_csv=None):
    """Jinja filter — never raises; expired list pages depend on this."""
    try:
        return expired_whatsapp_url(name, phone, provider, providers_csv=providers_csv)
    except Exception:
        log.exception("WhatsApp compose link failed for %r", name)
        return None


templates.env.filters["whatsapp_expired"] = _whatsapp_expired_filter
templates.env.globals["whatsapp_expired_url"] = _whatsapp_expired_filter


def _whatsapp_expiring_tonight_filter(name, phone, provider=None, providers_csv=None):
    try:
        return expiring_tonight_whatsapp_url(
            name, phone, provider, providers_csv=providers_csv
        )
    except Exception:
        log.exception("WhatsApp expiring-tonight link failed for %r", name)
        return None


templates.env.globals["whatsapp_expiring_tonight_url"] = _whatsapp_expiring_tonight_filter


def _whatsapp_renew_followup_filter(name, phone, provider=None, providers_csv=None):
    try:
        return renew_followup_whatsapp_url(
            name, phone, provider, providers_csv=providers_csv
        )
    except Exception:
        log.exception("WhatsApp renew follow-up link failed for %r", name)
        return None


templates.env.filters["whatsapp_renew_followup"] = _whatsapp_renew_followup_filter
templates.env.globals["whatsapp_renew_followup_url"] = _whatsapp_renew_followup_filter


def _whatsapp_payment_filter(name, phone, provider=None, providers_csv=None):
    try:
        return payment_received_whatsapp_url(
            name, phone, provider, providers_csv=providers_csv
        )
    except Exception:
        log.exception("WhatsApp payment link failed for %r", name)
        return None


def _whatsapp_renewed_filter(name, phone, provider=None, providers_csv=None):
    try:
        return renewed_whatsapp_url(name, phone, provider, providers_csv=providers_csv)
    except Exception:
        log.exception("WhatsApp renewed link failed for %r", name)
        return None


def _whatsapp_balance_filter(name, phone, due_paise, provider=None, providers_csv=None):
    try:
        return balance_reminder_whatsapp_url(name, phone, due_paise, provider, providers_csv)
    except Exception:
        log.exception("WhatsApp balance reminder link failed for %r", name)
        return None


templates.env.globals["balance_reminder_whatsapp_url"] = _whatsapp_balance_filter
templates.env.globals["payment_received_whatsapp_url"] = _whatsapp_payment_filter
templates.env.globals["renewed_whatsapp_url"] = _whatsapp_renewed_filter
templates.env.globals["wa_auto_send"] = settings.whatsapp_web_auto_send


def _whatsapp_invoice_filter(name, phone, invoice_no, download_url, provider="railtel"):
    try:
        return invoice_whatsapp_url(
            name, phone, invoice_no=invoice_no, download_url=download_url, provider=provider
        )
    except Exception:
        log.exception("WhatsApp invoice link failed for %r", name)
        return None


templates.env.filters["whatsapp_invoice"] = _whatsapp_invoice_filter
templates.env.globals["whatsapp_invoice_url"] = _whatsapp_invoice_filter


def _complaint_whatsapp_links_filter(complaint_id, assigned_agent_id=None, event=None):
    try:
        from .whatsapp_notify import complaint_whatsapp_links_for_ui

        aid = int(assigned_agent_id) if assigned_agent_id else None
        return complaint_whatsapp_links_for_ui(
            int(complaint_id),
            assigned_agent_id=aid,
            event=(event or "").strip() or None,
        )
    except Exception:
        log.exception("Complaint WhatsApp links failed for #%s", complaint_id)
        return []


def _complaint_whatsapp_primary_filter(complaint_id, assigned_agent_id=None, event=None):
    try:
        from .whatsapp_notify import complaint_whatsapp_primary_url

        aid = int(assigned_agent_id) if assigned_agent_id else None
        return complaint_whatsapp_primary_url(
            int(complaint_id),
            assigned_agent_id=aid,
            event=(event or "").strip() or None,
        )
    except Exception:
        log.exception("Complaint WhatsApp link failed for #%s", complaint_id)
        return None


templates.env.globals["complaint_whatsapp_links"] = _complaint_whatsapp_links_filter
templates.env.globals["complaint_whatsapp_primary_url"] = _complaint_whatsapp_primary_filter


def _was_simulated(result_json: str | None) -> bool:
    """True when a finished job's result came from simulate mode, not a real portal."""
    if not result_json:
        return False
    try:
        return bool(json.loads(result_json).get("simulated"))
    except (ValueError, AttributeError):
        return False


templates.env.filters["was_simulated"] = _was_simulated


def _job_result_message(result_json: str | None) -> str:
    """Human-readable portal reply saved on a finished job (e.g. Active since …)."""
    if not result_json:
        return ""
    try:
        return str(json.loads(result_json).get("message") or "").strip()
    except (ValueError, AttributeError):
        return ""


templates.env.filters["job_result_message"] = _job_result_message


def _job_created_by_filter(row):
    from .repo import job_created_by_label

    return job_created_by_label(row)


templates.env.filters["job_created_by"] = _job_created_by_filter


def _plain_error(text: str | None) -> str:
    """Short operator-facing wording; keep the original string for a tooltip."""
    raw = (text or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if "could not read main tv bouquet" in lower or "couldn't read the plan from hathway" in lower:
        return "Hathway plan has expired — no package on this box."
    if "login failed" in lower or "check credentials" in lower:
        return "Provider login failed. Update credentials in Settings, then retry."
    if "captcha" in lower:
        return "Couldn't complete the portal login check. Please retry."
    if "timed out" in lower or "timeout" in lower:
        return "The provider portal took too long to respond. Please retry."
    if "insufficient" in lower and "wallet" in lower:
        return "Provider wallet is too low. Recharge the wallet, then retry."
    if "recharge the partner wallet" in lower:
        return "Provider wallet is too low. Recharge the wallet, then retry."
    return raw


templates.env.filters["plain_error"] = _plain_error
templates.env.globals["settings"] = settings
templates.env.globals["provider_scopes"] = auth.PROVIDER_SCOPES
from .railtel_accounts import RAILTEL_DEALER_LABELS, RAILTEL_DEALER_SHORT, RAILTEL_DEALERS

templates.env.globals["railtel_dealers"] = RAILTEL_DEALERS
templates.env.globals["railtel_dealer_labels"] = RAILTEL_DEALER_LABELS
templates.env.globals["railtel_dealer_short"] = RAILTEL_DEALER_SHORT
from .upstream.providers import is_hybrid_hathway_stb

templates.env.globals["is_hybrid_hathway_stb"] = is_hybrid_hathway_stb
templates.env.globals["is_demo_instance"] = is_demo_instance
templates.env.globals["demo_port"] = DEMO_PORT

from .routes.pages import maps_nav_url, maps_search_url, maps_view_url

templates.env.globals["maps_nav_url"] = maps_nav_url
templates.env.globals["maps_search_url"] = maps_search_url
templates.env.globals["maps_view_url"] = maps_view_url
templates.env.globals["today_iso"] = lambda: fmt_date(calendar_today())


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    requeued = job_queue.reset_stuck_jobs()
    if requeued:
        log.info("Requeued %s job(s) that were interrupted by a restart", requeued)
    job_queue.start_worker()
    from .expired_status_schedule import cancel_auto_expired_jobs

    cancelled_auto = cancel_auto_expired_jobs()
    if cancelled_auto:
        log.info("Cancelled %s auto expired-status job(s)", cancelled_auto)
    if settings.whatsapp_web_auto_send:
        import threading

        from .whatsapp_send import warm_whatsapp_session

        def _warm_whatsapp() -> None:
            try:
                warm_whatsapp_session()
            except Exception as exc:
                log.warning("WhatsApp warm-up skipped: %s", exc)

        # Non-blocking startup; Playwright runs on the dedicated whatsapp-sender thread.
        threading.Thread(
            target=_warm_whatsapp, name="whatsapp-warmup", daemon=True
        ).start()
    log.info(
        "VK Platform ready — db=%s upstream_mode=%s", settings.db_path, settings.upstream_mode
    )
    try:
        yield
    finally:
        from .expired_status_schedule import stop_expired_status_scheduler

        stop_expired_status_scheduler()
        job_queue.stop_worker()
        if settings.whatsapp_web_auto_send:
            from .whatsapp_send import close_whatsapp_session

            close_whatsapp_session()


_DUTY_FREE_PATHS = ("/field/duty", "/field/ping", "/logout", "/login")


def _field_duty_block(request: Request):
    """Field agents may only browse until they turn on Field login."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.url.path or ""
    if path.startswith(_DUTY_FREE_PATHS):
        return None
    from . import field as field_mod

    agent = request.state.agent
    if not field_mod.needs_field_duty(agent) or field_mod.is_field_duty_on(agent):
        return None
    if path.startswith("/api/") or request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"detail": "Turn on Field login first."}, status_code=403)
    referer = request.headers.get("referer") or ""
    back = urlsplit(referer)
    target = back.path or "/"
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    params = [(k, v) for k, v in parse_qsl(back.query) if k not in ("flash", "level", "wa")]
    params += [("flash", "Turn on Field login first."), ("level", "err")]
    return RedirectResponse(f"{target}?{urlencode(params)}", status_code=303)


def create_app() -> FastAPI:
    app = FastAPI(title="VK Platform", version="0.1.0", lifespan=lifespan)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        from .db import set_current_agent

        request.state.agent = None
        set_current_agent(None)
        if not auth.is_public_path(request.url.path):
            agent_id = auth.read_token(request.cookies.get(auth.COOKIE_NAME))
            if agent_id is not None:
                from .db import connection as db_connection

                with db_connection() as conn:
                    request.state.agent = auth.load_agent(conn, agent_id)
                    if request.state.agent is not None:
                        from . import field as field_mod

                        request.state.agent = field_mod.expire_stale_duty(
                            conn, request.state.agent
                        )
                        path = request.url.path or ""
                        if not path.startswith("/field/ping"):
                            auth.touch_agent_seen(conn, agent_id)
            if request.state.agent is None:
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
                return auth.redirect_to_login(request)
            needed = auth.path_permission(request.url.path)
            if needed and not auth.can(request.state.agent, needed):
                return auth.redirect_forbidden()
            blocked = _field_duty_block(request)
            if blocked is not None:
                return blocked
            set_current_agent(int(request.state.agent["id"]))
        return await call_next(request)

    from .routes import api, pages, pages_v2, pay_portal  # imported here so templates are configured first

    app.include_router(pages.router)
    app.include_router(pages_v2.router)
    app.include_router(pay_portal.router)
    app.include_router(api.router)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return JSONResponse("Internal Server Error", status_code=500)

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "upstream_mode": settings.upstream_mode,
            "build": "2026-09-16-v2",
            "mobile_ui": True,
            "mobile_path": "/v2/",
        }

    @app.get("/favicon.ico")
    async def favicon():
        return RedirectResponse("/static/favicon.png", status_code=307)

    return app


app = create_app()
