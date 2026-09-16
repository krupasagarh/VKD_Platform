"""Mobile v2 UI — parallel to classic server-rendered pages."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, repo
from ..db import connection
from ..upstream.providers import PROVIDERS, PROVIDER_LABELS
from .pages import _render

router = APIRouter(prefix="/v2", redirect_slashes=True)

UI_COOKIE = "vk_ui"
UI_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _render_v2(request: Request, template: str, **context) -> HTMLResponse:
    return _render(request, f"v2/{template}", **context)


@router.get("/switch")
async def switch_to_v2(request: Request):
    """Set mobile UI preference and open the v2 home screen."""
    response = RedirectResponse("/v2/", status_code=303)
    response.set_cookie(UI_COOKIE, "v2", max_age=UI_COOKIE_MAX_AGE, httponly=False, samesite="lax")
    return response


@router.get("/classic")
async def switch_to_classic(request: Request):
    """Clear mobile UI preference and return to classic dashboard."""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(UI_COOKIE)
    return response


@router.get("/", response_class=HTMLResponse)
async def v2_home(request: Request):
    feed = (request.query_params.get("feed") or "followup").strip().lower()
    if feed not in {"followup", "expiring", "expired"}:
        feed = "followup"

    with connection() as conn:
        stats = repo.dashboard_stats(conn)
        if feed == "followup":
            items = [dict(row) for row in repo.list_collect_later(conn, limit=50)]
        elif feed == "expiring":
            items = [dict(row) for row in repo.expiring_connections(conn, limit=50)]
        else:
            result = repo.search_customers(
                conn, view="expired", page=1, page_size=50,
            )
            items = [dict(row) for row in result["rows"]]

    return _render_v2(
        request,
        "home.html",
        stats=stats,
        items=items,
        feed=feed,
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

    with connection() as conn:
        result = repo.search_customers(
            conn,
            query=q,
            view=view,
            page=int(params.get("page", 1) or 1),
        )

    return _render_v2(
        request,
        "customers.html",
        result=result,
        q=q,
        view=view,
        provider_labels=PROVIDER_LABELS,
        providers=PROVIDERS,
    )


@router.get("/menu", response_class=HTMLResponse)
async def v2_menu(request: Request):
    return _render_v2(request, "menu.html")
