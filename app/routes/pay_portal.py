"""Public customer pay portal (/pay) and staff confirmation (/pay/admin)."""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, public_pay
from ..config import settings
from ..db import connection, transaction
from ..money import now_iso, to_paise
from .pages import _redirect, _render

router = APIRouter()

_MOBILE_UA = re.compile(r"Mobile|Android|iPhone|iPod|webOS|BlackBerry|IEMobile|Opera Mini", re.I)


def _mobile_client(request: Request) -> bool:
    ua = request.headers.get("user-agent") or ""
    if _MOBILE_UA.search(ua):
        return True
    ch = (request.headers.get("sec-ch-ua-mobile") or "").strip()
    return ch == "?1"


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host or ""
    return ""


def _pay_render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("operator", settings.operator)
    context.setdefault("flash", request.query_params.get("flash", ""))
    context.setdefault("flash_level", request.query_params.get("level", "ok"))
    return _render(request, template, **context)


def _public_pay_url(request: Request) -> str:
    if settings.public_base_url:
        return f"{settings.public_base_url.rstrip('/')}/pay"
    base = str(request.base_url).rstrip("/")
    return f"{base}/pay"


def pay_admin_context(request: Request) -> dict:
    with connection() as conn:
        intents = public_pay.list_open_intents(conn)
        vpa = public_pay.effective_upi_vpa(conn)
        payee = public_pay.effective_payee_name(conn)
        enabled = public_pay.public_pay_enabled(conn)
    public_url = _public_pay_url(request)
    return {
        "intents": intents,
        "vpa": vpa,
        "payee": payee,
        "enabled": enabled,
        "public_url": public_url,
        "public_qr_url": (
            "https://api.qrserver.com/v1/create-qr-code/?size=280x280&margin=10&data="
            + quote(public_url, safe="")
        ),
    }


@router.get("/pay", response_class=HTMLResponse)
async def pay_home(request: Request):
    mode = (request.query_params.get("mode") or "mobile").strip().lower()
    if mode not in {"mobile", "stb"}:
        mode = "mobile"
    with connection() as conn:
        enabled = public_pay.public_pay_enabled(conn)
        upi_ok = bool(public_pay.effective_upi_vpa(conn))
    return _pay_render(
        request,
        "pay/home.html",
        enabled=enabled,
        upi_ok=upi_ok,
        mode=mode,
        phone=request.query_params.get("phone", ""),
        service_id=request.query_params.get("service_id", ""),
        public_url=_public_pay_url(request),
    )


@router.post("/pay/lookup")
async def pay_lookup(
    request: Request,
    lookup: str = Form("mobile"),
    phone: str = Form(""),
    service_id: str = Form(""),
):
    lookup = (lookup or "mobile").strip().lower()
    if lookup not in {"mobile", "stb"}:
        lookup = "mobile"

    if not public_pay.check_lookup_rate_limit(_client_ip(request)):
        return _pay_render(
            request,
            "pay/home.html",
            enabled=True,
            mode=lookup,
            error="Too many tries. Please wait a few minutes and try again.",
            phone=phone,
            service_id=service_id,
            public_url=_public_pay_url(request),
        )

    with connection() as conn:
        if not public_pay.public_pay_enabled(conn):
            return _pay_render(
                request,
                "pay/home.html",
                enabled=False,
                mode=lookup,
                public_url=_public_pay_url(request),
            )

        if lookup == "stb":
            matches = public_pay.find_connections_by_service_id(conn, service_id)
            if not matches:
                return _pay_render(
                    request,
                    "pay/home.html",
                    enabled=True,
                    mode="stb",
                    error="No TV / broadband account found for that STB or Railtel ID. Check and try again.",
                    service_id=service_id,
                    public_url=_public_pay_url(request),
                )
            row = matches[0]
            cid = int(row["customer_id"])
            conn_id = int(row["connection_id"])
            return RedirectResponse(f"/pay/account/{cid}?c={conn_id}", status_code=303)

        last10 = public_pay.normalize_phone(phone)
        if not last10:
            return _pay_render(
                request,
                "pay/home.html",
                enabled=True,
                mode="mobile",
                error="Enter your 10-digit mobile number.",
                phone=phone,
                public_url=_public_pay_url(request),
            )
        matches = public_pay.find_customers_by_phone(conn, last10)

    if not matches:
        return _pay_render(
            request,
            "pay/home.html",
            enabled=True,
            mode="mobile",
            error="No account found for this number. Try STB / Railtel ID tab or contact support.",
            phone=phone,
            public_url=_public_pay_url(request),
        )

    if len(matches) == 1:
        return RedirectResponse(f"/pay/account/{matches[0]['id']}", status_code=303)

    choices = [
        {
            "id": int(m["id"]),
            "label": public_pay.mask_name(m["name"]),
            "phone_hint": public_pay.mask_phone_last4(m["phone"]),
        }
        for m in matches
    ]
    return _pay_render(
        request,
        "pay/pick.html",
        choices=choices,
        phone=last10,
    )


@router.get("/pay/account/{customer_id}", response_class=HTMLResponse)
async def pay_account(request: Request, customer_id: int):
    raw_c = (request.query_params.get("c") or "").strip()
    focus_id = int(raw_c) if raw_c.isdigit() else None
    with connection() as conn:
        if not public_pay.public_pay_enabled(conn):
            return _pay_render(request, "pay/home.html", enabled=False, public_url=_public_pay_url(request))
        account = public_pay.build_account_view(
            conn, customer_id, focus_connection_id=focus_id
        )
        upi_ok = bool(public_pay.effective_upi_vpa(conn))

    if account is None:
        return RedirectResponse("/pay", status_code=303)

    return _pay_render(
        request,
        "pay/account.html",
        account=account,
        upi_ok=upi_ok,
    )


@router.post("/pay/account/{customer_id}/start")
async def pay_start(
    request: Request,
    customer_id: int,
    connection_id: str = Form(""),
    amount: str = Form(""),
):
    with transaction() as conn:
        if not public_pay.public_pay_enabled(conn):
            return RedirectResponse("/pay", status_code=303)
        raw_c = (connection_id or "").strip()
        focus_id = int(raw_c) if raw_c.isdigit() else None
        account = public_pay.build_account_view(
            conn, customer_id, focus_connection_id=focus_id
        )
        if account is None:
            return RedirectResponse("/pay", status_code=303)

        default_paise = int(account["pay_paise"])
        amount_paise = to_paise(amount) if (amount or "").strip() else default_paise
        if amount_paise <= 0:
            return _pay_render(
                request,
                "pay/account.html",
                account=account,
                upi_ok=bool(public_pay.effective_upi_vpa(conn)),
                error="Enter an amount above zero, or nothing is due on this account.",
            )

        conn_id = public_pay.suggest_connection_id(account, connection_id)
        if account.get("renew_single") and not conn_id:
            return _pay_render(
                request,
                "pay/account.html",
                account=account,
                upi_ok=bool(public_pay.effective_upi_vpa(conn)),
                error="Could not link payment to this STB. Start again from the pay page.",
            )
        try:
            intent = public_pay.create_pay_intent(
                conn,
                customer_id=customer_id,
                connection_id=conn_id,
                amount_paise=amount_paise,
                ip_hint=_client_ip(request)[:64],
            )
        except ValueError as exc:
            return _pay_render(
                request,
                "pay/account.html",
                account=account,
                upi_ok=bool(public_pay.effective_upi_vpa(conn)),
                error=str(exc),
            )

    return RedirectResponse(f"/pay/i/{intent['token']}", status_code=303)


@router.get("/pay/i/{token}", response_class=HTMLResponse)
async def pay_intent_page(request: Request, token: str):
    with connection() as conn:
        intent = public_pay.get_intent_by_token(conn, token)
        if intent is None:
            return _pay_render(request, "pay/status.html", state="missing")
        payload = public_pay.intent_upi_payload(conn, intent)
        focus = int(intent["connection_id"]) if intent["connection_id"] else None
        account = public_pay.build_account_view(
            conn, int(intent["customer_id"]), focus_connection_id=focus
        )

    qr_url = (
        "https://api.qrserver.com/v1/create-qr-code/?size=240x240&margin=8&data="
        + quote(payload["upi_uri"], safe="")
    )
    return _pay_render(
        request,
        "pay/intent.html",
        intent=intent,
        account=account,
        upi=payload,
        qr_url=qr_url,
    )


@router.post("/pay/i/{token}/done")
async def pay_intent_marked(request: Request, token: str):
    with transaction() as conn:
        intent = public_pay.get_intent_by_token(conn, token)
        if intent is None:
            return RedirectResponse("/pay", status_code=303)
        public_pay.mark_customer_paid(conn, int(intent["id"]))
        intent = conn.execute("SELECT * FROM pay_intents WHERE id = ?", (intent["id"],)).fetchone()

    return _pay_render(
        request,
        "pay/status.html",
        state="marked",
        intent=intent,
        reference=intent["reference"] if intent else "",
    )


@router.get("/pay/qr", response_class=HTMLResponse)
async def pay_qr_staff_mobile(request: Request):
    """Staff mobile Pay QR screen (same content as /v2/pay-qr). Requires login."""
    if not auth.can(request.state.agent, "payments"):
        return auth.redirect_forbidden()
    ctx = pay_admin_context(request)
    return _render(request, "v2/pay_qr.html", active="pay_qr", **ctx)


@router.get("/pay/admin", response_class=HTMLResponse)
async def pay_admin(request: Request):
    if not auth.can(request.state.agent, "payments"):
        return auth.redirect_forbidden()
    ctx = pay_admin_context(request)
    force_desktop = (request.query_params.get("desktop") or "").strip() in {"1", "true", "yes"}
    mobile = _mobile_client(request) and not force_desktop
    ctx["settings_next"] = "/pay/admin"
    if mobile:
        return _pay_render(request, "pay/staff_admin.html", **ctx)
    return _pay_render(request, "pay/admin.html", **ctx)


@router.post("/pay/admin/settings")
async def pay_admin_settings(
    request: Request,
    upi_vpa: str = Form(""),
    upi_payee: str = Form(""),
    enabled: str = Form(""),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "agents"):
        dest = (next or "").strip() or "/pay/admin"
        return _redirect(
            dest,
            flash="UPI settings are under Settings → Customer pay.",
            level="err",
        )
    portal_on = (enabled or "").strip().lower() not in {"", "0", "false", "off", "no"}
    with transaction() as conn:
        public_pay.save_public_pay_settings(
            conn,
            upi_vpa=upi_vpa,
            upi_payee=upi_payee,
            enabled=portal_on,
        )
    dest = (next or "").strip() or "/settings/upi"
    if dest.startswith("/pay/"):
        dest = "/settings/upi"
    return _redirect(dest, flash="Pay portal settings saved.")


@router.post("/pay/admin/intents/{intent_id}/confirm")
async def pay_admin_confirm(
    request: Request,
    intent_id: int,
    queue_renew: str = Form(""),
    next: str = Form(""),
):
    if not auth.can(request.state.agent, "payments"):
        return auth.redirect_forbidden()

    agent = request.state.agent or {}
    renew = (queue_renew or "").strip().lower() not in {"0", "off", "false", "no"}

    try:
        with transaction() as conn:
            result = public_pay.confirm_pay_intent(
                conn,
                intent_id,
                agent_name=agent.get("name") or settings.operator,
                agent_id=int(agent["id"]) if agent.get("id") else None,
                queue_renew=renew,
                can_portal=auth.can(request.state.agent, "portal_actions"),
            )
    except ValueError as exc:
        dest = (next or "").strip() or "/pay/admin"
        return _redirect(dest, flash=str(exc), level="err")

    flash = f"Payment {result['receipt_no']} recorded.{result['renew_note']}"
    dest = (next or "").strip() or f"/customers/{result['customer_id']}"
    if dest.startswith("/pay/admin") or dest.startswith("/customers/"):
        return _redirect(dest, flash=flash)
    return _redirect("/pay/admin", flash=flash)


@router.post("/pay/admin/intents/{intent_id}/cancel")
async def pay_admin_cancel(request: Request, intent_id: int, next: str = Form("")):
    if not auth.can(request.state.agent, "payments"):
        return auth.redirect_forbidden()
    with transaction() as conn:
        conn.execute(
            "UPDATE pay_intents SET status = 'cancelled', updated_at = ? "
            "WHERE id = ? AND status IN ('pending', 'customer_marked')",
            (now_iso(), intent_id),
        )
    return _redirect((next or "").strip() or "/pay/admin", flash="Request cancelled.")


@router.post("/customers/{customer_id}/pay-intents/{intent_id}/confirm")
async def customer_pay_intent_confirm(
    request: Request,
    customer_id: int,
    intent_id: int,
    queue_renew: str = Form(""),
):
    if not auth.can(request.state.agent, "payments"):
        return auth.redirect_forbidden()
    agent = request.state.agent or {}
    renew = (queue_renew or "").strip().lower() not in {"0", "off", "false", "no"}
    try:
        with transaction() as conn:
            intent = conn.execute(
                "SELECT customer_id FROM pay_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            if intent is None or int(intent["customer_id"]) != customer_id:
                raise ValueError("QR payment not found for this customer.")
            result = public_pay.confirm_pay_intent(
                conn,
                intent_id,
                agent_name=agent.get("name") or settings.operator,
                agent_id=int(agent["id"]) if agent.get("id") else None,
                queue_renew=renew,
                can_portal=auth.can(request.state.agent, "portal_actions"),
            )
    except ValueError as exc:
        return _redirect(f"/customers/{customer_id}", flash=str(exc), level="err")
    flash = f"QR payment {result['receipt_no']} recorded.{result['renew_note']}"
    return _redirect(f"/customers/{customer_id}", flash=flash)
