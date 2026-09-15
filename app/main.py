"""FastAPI application for VK Platform."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from .config import APP_DIR, settings
from .db import init_db
from .messaging import expired_whatsapp_url, invoice_whatsapp_url, renew_followup_whatsapp_url
from .money import (
    days_until,
    fmt_date_display,
    fmt_datetime_human,
    fmt_rupees,
    from_paise,
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
templates.env.filters["rupees_plain"] = lambda p: f"{from_paise(p):.2f}"
templates.env.filters["nice_date"] = fmt_date_display
templates.env.filters["nice_datetime"] = fmt_datetime_human
templates.env.filters["days_until"] = days_until
def _whatsapp_expired_filter(name, phone, provider=None):
    """Jinja filter — never raises; expired list pages depend on this."""
    try:
        return expired_whatsapp_url(name, phone, provider)
    except Exception:
        log.exception("WhatsApp compose link failed for %r", name)
        return None


templates.env.filters["whatsapp_expired"] = _whatsapp_expired_filter
templates.env.globals["whatsapp_expired_url"] = _whatsapp_expired_filter


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


def _was_simulated(result_json: str | None) -> bool:
    """True when a finished job's result came from simulate mode, not a real portal."""
    if not result_json:
        return False
    try:
        return bool(json.loads(result_json).get("simulated"))
    except (ValueError, AttributeError):
        return False


templates.env.filters["was_simulated"] = _was_simulated
templates.env.globals["settings"] = settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    requeued = job_queue.reset_stuck_jobs()
    if requeued:
        log.info("Requeued %s job(s) that were interrupted by a restart", requeued)
    job_queue.start_worker()
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
        job_queue.stop_worker()
        if settings.whatsapp_web_auto_send:
            from .whatsapp_send import close_whatsapp_session

            close_whatsapp_session()


def create_app() -> FastAPI:
    app = FastAPI(title="VK Platform", version="0.1.0", lifespan=lifespan)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        request.state.agent = None
        if not auth.is_public_path(request.url.path):
            agent_id = auth.read_token(request.cookies.get(auth.COOKIE_NAME))
            if agent_id is not None:
                from .db import connection as db_connection

                with db_connection() as conn:
                    request.state.agent = auth.load_agent(conn, agent_id)
            if request.state.agent is None:
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
                return auth.redirect_to_login(request)
            needed = auth.path_permission(request.url.path)
            if needed and not auth.can(request.state.agent, needed):
                return auth.redirect_forbidden()
        return await call_next(request)

    from .routes import api, pages  # imported here so templates are configured first

    app.include_router(pages.router)
    app.include_router(api.router)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return JSONResponse("Internal Server Error", status_code=500)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "upstream_mode": settings.upstream_mode, "build": "2026-09-15b"}

    @app.get("/favicon.ico")
    async def favicon():
        return RedirectResponse("/static/favicon.svg", status_code=307)

    return app


app = create_app()
