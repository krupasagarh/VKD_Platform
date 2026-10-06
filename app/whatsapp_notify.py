"""Fire-and-forget WhatsApp after a live renew, a collected payment, or a complaint."""
from __future__ import annotations

import logging
import threading
import time

from .config import settings
from .db import connection, get_setting, log_activity, set_current_agent, transaction
from .messaging import (
    complaint_technician_assigned_whatsapp_text,
    complaint_whatsapp_text,
    expired_whatsapp_text,
    normalize_wa_phone,
    payment_received_whatsapp_text,
    renewed_whatsapp_text,
    simple_customer_text,
    whatsapp_share_url,
    whatsapp_text_url,
)

log = logging.getLogger("vk_platform.whatsapp")

# Office auto-send kinds a staff member can trigger from a button ("Send now").
SEND_NOW_KINDS = {
    "payment": "Payment received",
    "renewed": "Renewed",
    "expired": "Expired renewal reminder",
    "tonight": "Expiring tonight",
    "reminder": "Payment reminder",
}
_DEDUPE_SECONDS = 120
_recent_sends: dict[tuple[int, str], float] = {}
_recent_lock = threading.Lock()


def _claim_send(customer_id: int, kind: str) -> bool:
    """False when the same message went to this customer moments ago (double tap)."""
    key = (int(customer_id), kind)
    now = time.monotonic()
    with _recent_lock:
        last = _recent_sends.get(key)
        if last is not None and now - last < _DEDUPE_SECONDS:
            return False
        _recent_sends[key] = now
    return True


def customer_has_whatsapp_phone(customer_id: int) -> bool:
    cust = _customer_row(customer_id)
    return bool(cust is not None and normalize_wa_phone(cust["phone"]))


def queue_customer_message(
    *,
    customer_id: int,
    kind: str,
    connection_id: int | None = None,
    provider: str | None = None,
    agent_id: int | None = None,
    **extra,
) -> str:
    """Send a customer message from the office WhatsApp in the background.

    Returns "" when queued, otherwise a short reason it was not sent.
    """
    kind = (kind or "").strip().lower()
    if kind not in SEND_NOW_KINDS:
        return "Unknown WhatsApp message."
    if not settings.whatsapp_web_auto_send:
        return "Office WhatsApp auto-send is off."
    if not customer_has_whatsapp_phone(customer_id):
        return "No customer phone for WhatsApp."
    if not _claim_send(customer_id, kind):
        return "Already sent a moment ago."
    _spawn(
        send_now,
        _agent_id=agent_id,
        customer_id=int(customer_id),
        kind=kind,
        connection_id=int(connection_id) if connection_id else None,
        provider=provider,
        **extra,
    )
    return ""

def notify_after_renew(
    *,
    customer_id: int | None,
    provider: str | None = None,
    connection_id: int | None = None,
) -> None:
    """Send the renewed/recharged text after a successful live portal renew."""
    if not settings.whatsapp_web_auto_send or not customer_id:
        return
    _spawn(
        _send_renewed,
        customer_id=int(customer_id),
        provider=provider,
        connection_id=int(connection_id) if connection_id else None,
    )


def notify_after_payment(
    *,
    customer_id: int,
    provider: str | None = None,
    connection_id: int | None = None,
    mode: str | None = None,
) -> None:
    """Send the payment-received text after cash / UPI / cheque is recorded."""
    if not settings.whatsapp_web_auto_send:
        return
    if (mode or "").strip().lower() == "adjustment":
        return
    _spawn(
        _send_payment,
        customer_id=int(customer_id),
        provider=provider,
        connection_id=int(connection_id) if connection_id else None,
    )


def notify_complaint(
    event: str,
    *,
    complaint_id: int,
    actor: str | None = None,
    note: str | None = None,
    resolution: str | None = None,
    assigned_agent_id: int | None = None,
) -> None:
    """Alert technicians on WhatsApp when a complaint is logged or updated."""
    if not settings.whatsapp_web_auto_send:
        return
    _spawn(
        _send_complaint,
        event=(event or "").strip().lower(),
        complaint_id=int(complaint_id),
        actor=actor,
        note=note,
        resolution=resolution,
        assigned_agent_id=int(assigned_agent_id) if assigned_agent_id else None,
    )


def send_complaint_now(
    event: str,
    *,
    complaint_id: int,
    actor: str | None = None,
    note: str | None = None,
    resolution: str | None = None,
    assigned_agent_id: int | None = None,
) -> dict:
    """Send complaint WhatsApp alerts now and return the result (for UI feedback)."""
    if not settings.whatsapp_web_auto_send:
        return {"ok": False, "error": "WhatsApp auto-send is disabled (WHATSAPP_WEB_AUTO_SEND=0)."}
    return _send_complaint(
        event=(event or "").strip().lower(),
        complaint_id=int(complaint_id),
        actor=actor,
        note=note,
        resolution=resolution,
        assigned_agent_id=int(assigned_agent_id) if assigned_agent_id else None,
    )


def complaint_whatsapp_flash(result: dict) -> tuple[str, str]:
    """Return (flash_message, level) for complaint WhatsApp send."""
    if result.get("ok"):
        parts = result.get("parts") or []
        if parts:
            return "WhatsApp sent: " + "; ".join(parts), "ok"
        return "WhatsApp alert sent.", "ok"
    err = result.get("error") or ""
    if err:
        return f"WhatsApp not sent: {err}", "err"
    errors = result.get("errors") or []
    if errors:
        return f"WhatsApp not sent: {errors[0]}", "err"
    return "WhatsApp not sent. Check group name, agent phone and WhatsApp Web login.", "err"


def _spawn(fn, _agent_id: int | None = None, **kwargs) -> None:
    threading.Thread(
        target=_safe_run,
        args=(fn, kwargs, _agent_id),
        name="whatsapp-notify",
        daemon=True,
    ).start()


def _safe_run(fn, kwargs, agent_id: int | None = None) -> None:
    set_current_agent(agent_id)
    try:
        fn(**kwargs)
    except Exception:
        log.exception("WhatsApp notify failed")


def _customer_row(customer_id: int):
    with connection() as conn:
        return conn.execute(
            "SELECT name, phone FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()


def _connection_provider(connection_id: int | None) -> str | None:
    if not connection_id:
        return None
    with connection() as conn:
        row = conn.execute(
            "SELECT provider FROM connections WHERE id = ?",
            (connection_id,),
        ).fetchone()
    return (row["provider"] if row else None) or None


def _customer_providers_csv(customer_id: int) -> str | None:
    with connection() as conn:
        parts = [
            str(row["provider"] or "").strip()
            for row in conn.execute(
                "SELECT DISTINCT provider FROM connections WHERE customer_id = ? "
                "ORDER BY provider",
                (customer_id,),
            )
            if str(row["provider"] or "").strip()
        ]
    return ",".join(parts) if parts else None


def send_now(
    *,
    customer_id: int,
    kind: str,
    connection_id: int | None = None,
    provider: str | None = None,
    amount_paise: int | None = None,
    remaining_paise: int | None = None,
    renew_queued: bool = False,
) -> dict:
    """Send one customer message from the office WhatsApp. Blocks until WhatsApp Web finishes."""
    kind = (kind or "").strip().lower()
    if kind == "renewed":
        return _send_renewed(
            customer_id=customer_id, provider=provider, connection_id=connection_id
        )
    if kind == "payment":
        return _send_payment(
            customer_id=customer_id,
            provider=provider,
            connection_id=connection_id,
            amount_paise=amount_paise,
            remaining_paise=remaining_paise,
            renew_queued=renew_queued,
        )
    if kind == "expired":
        return _send_expired(
            customer_id=customer_id, provider=provider, connection_id=connection_id
        )
    if kind == "tonight":
        return _send_template(
            customer_id=customer_id, provider=provider, connection_id=connection_id,
            template_key="expiring_tonight", kind="tonight",
        )
    if kind == "reminder":
        return _send_template(
            customer_id=customer_id, provider=provider, connection_id=connection_id,
            template_key="renew_followup", kind="reminder",
        )
    return {"ok": False, "error": "Unknown WhatsApp message."}


def _send_template(
    *,
    customer_id: int,
    provider: str | None,
    connection_id: int | None,
    template_key: str,
    kind: str,
) -> dict:
    cust = _customer_row(customer_id)
    if cust is None:
        return {"ok": False, "error": "Customer not found."}
    provider = provider or _connection_provider(connection_id)
    providers_csv = None if provider else _customer_providers_csv(customer_id)
    text = simple_customer_text(template_key, cust["name"], provider, providers_csv=providers_csv)
    return _deliver(
        customer_id=customer_id,
        connection_id=connection_id,
        phone=cust["phone"],
        text=text,
        kind=kind,
    )


def _deliver(
    *,
    customer_id: int,
    connection_id: int | None,
    phone: str | None,
    text: str,
    kind: str,
) -> dict:
    from .whatsapp_send import send_whatsapp_text

    # Customer texts always go to the customer phone — never RAILTEL_INVOICE_WHATSAPP_TEST.
    result = send_whatsapp_text(phone, text)
    note = result.get("message") or result.get("error") or "WhatsApp send attempted"
    if result.get("ok"):
        last10 = str(result.get("phone") or "")[-10:]
        if kind == "renewed":
            note = f"Renewed WhatsApp sent to {last10}"
        elif kind == "payment":
            note = f"Payment WhatsApp sent to {last10}"
        elif kind == "expired":
            note = f"Expired reminder WhatsApp sent to {last10}"
        elif kind == "tonight":
            note = f"Expiring tonight WhatsApp sent to {last10}"
        elif kind == "reminder":
            note = f"Payment reminder WhatsApp sent to {last10}"
        elif kind == "complaint_assigned":
            note = f"Complaint technician WhatsApp sent to {last10}"
    with transaction() as conn:
        log_activity(
            conn,
            "whatsapp_sent" if result.get("ok") else "whatsapp_failed",
            note,
            customer_id=customer_id,
            connection_id=connection_id,
        )
    if result.get("ok") and note:
        result = {**result, "message": note}
    return result


def _send_renewed(
    *,
    customer_id: int,
    provider: str | None,
    connection_id: int | None,
) -> dict:
    cust = _customer_row(customer_id)
    if cust is None:
        return {"ok": False, "error": "Customer not found."}
    provider = provider or _connection_provider(connection_id)
    providers_csv = None if provider else _customer_providers_csv(customer_id)
    text = renewed_whatsapp_text(cust["name"], provider, providers_csv=providers_csv)
    return _deliver(
        customer_id=customer_id,
        connection_id=connection_id,
        phone=cust["phone"],
        text=text,
        kind="renewed",
    )


def _send_payment(
    *,
    customer_id: int,
    provider: str | None,
    connection_id: int | None,
    amount_paise: int | None = None,
    remaining_paise: int | None = None,
    renew_queued: bool = False,
) -> dict:
    cust = _customer_row(customer_id)
    if cust is None:
        return {"ok": False, "error": "Customer not found."}
    provider = provider or _connection_provider(connection_id)
    providers_csv = None if provider else _customer_providers_csv(customer_id)
    with connection() as conn:
        if amount_paise is None:
            last = conn.execute(
                "SELECT amount_paise FROM payments "
                "WHERE customer_id = ? AND lower(coalesce(mode, '')) != 'adjustment' "
                "ORDER BY id DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
            if last is not None:
                amount_paise = int(last["amount_paise"] or 0)
        if remaining_paise is None:
            from .billing import customer_ledger

            remaining_paise = int(customer_ledger(conn, customer_id)["net_due_paise"])
    text = payment_received_whatsapp_text(
        cust["name"],
        provider,
        providers_csv=providers_csv,
        amount_paise=amount_paise,
        remaining_paise=remaining_paise,
        renew_queued=renew_queued,
    )
    return _deliver(
        customer_id=customer_id,
        connection_id=connection_id,
        phone=cust["phone"],
        text=text,
        kind="payment",
    )


def _send_expired(
    *,
    customer_id: int,
    provider: str | None,
    connection_id: int | None,
) -> dict:
    cust = _customer_row(customer_id)
    if cust is None:
        return {"ok": False, "error": "Customer not found."}
    provider = provider or _connection_provider(connection_id)
    providers_csv = None if provider else _customer_providers_csv(customer_id)
    text = expired_whatsapp_text(cust["name"], provider, providers_csv=providers_csv)
    return _deliver(
        customer_id=customer_id,
        connection_id=connection_id,
        phone=cust["phone"],
        text=text,
        kind="expired",
    )


def _complaint_whatsapp_group() -> str:
    with connection() as conn:
        stored = (get_setting(conn, "complaint_whatsapp_group", "") or "").strip()
    return stored or settings.complaint_whatsapp_group


def _complaint_row(complaint_id: int):
    with connection() as conn:
        return conn.execute(
            "SELECT cp.*, c.name AS customer_name, c.code AS customer_code, "
            "c.phone AS customer_phone, c.address AS customer_address "
            "FROM complaints cp "
            "JOIN customers c ON c.id = cp.customer_id "
            "WHERE cp.id = ?",
            (complaint_id,),
        ).fetchone()


def _customer_service_id(customer_id: int) -> str:
    """Railtel login or Hathway STB — whatever we have on file for this customer."""
    with connection() as conn:
        row = conn.execute(
            "SELECT upstream_id FROM connections "
            "WHERE customer_id = ? AND trim(COALESCE(upstream_id, '')) != '' "
            "ORDER BY CASE provider "
            "  WHEN 'railtel' THEN 0 WHEN 'hathway' THEN 1 ELSE 2 END, id "
            "LIMIT 1",
            (int(customer_id),),
        ).fetchone()
    return (row["upstream_id"] if row else "") or ""


def _agent_phone(agent_id: int | None) -> str:
    if not agent_id:
        return ""
    with connection() as conn:
        row = conn.execute(
            "SELECT phone FROM agents WHERE id = ? AND active = 1",
            (int(agent_id),),
        ).fetchone()
    return (row["phone"] if row else "") or ""


def _complaint_context(
    complaint_id: int,
    *,
    event: str | None = None,
    actor: str | None = None,
    note: str | None = None,
    resolution: str | None = None,
    assigned_agent_id: int | None = None,
) -> dict | None:
    row = _complaint_row(complaint_id)
    if row is None:
        return None

    agent_id = assigned_agent_id or row["assigned_agent_id"]
    assigned_to = row["assigned_to"] or ""
    if agent_id and not assigned_to:
        with connection() as conn:
            name_row = conn.execute(
                "SELECT name FROM agents WHERE id = ?", (int(agent_id),)
            ).fetchone()
        assigned_to = (name_row["name"] if name_row else "") or ""

    event_key = (event or "").strip().lower()
    if not event_key:
        event_key = "assigned" if agent_id else "opened"

    platform_url = settings.public_base_url
    service_id = _customer_service_id(int(row["customer_id"]))
    group_text = complaint_whatsapp_text(
        event_key,
        complaint_id=complaint_id,
        title=row["title"],
        customer_name=row["customer_name"],
        customer_code=row["customer_code"],
        customer_service_id=service_id,
        customer_phone=row["customer_phone"],
        address=row["customer_address"],
        details=row["details"],
        assigned_to=assigned_to,
        created_by=row["created_by"],
        actor=actor,
        note=note or row["last_note"],
        resolution=resolution or row["resolution"],
        platform_url=platform_url,
    )
    agent_text = ""
    if agent_id and event_key in {"opened", "assigned"}:
        agent_text = complaint_whatsapp_text(
            "assigned",
            complaint_id=complaint_id,
            title=row["title"],
            customer_name=row["customer_name"],
            customer_code=row["customer_code"],
            customer_service_id=service_id,
            customer_phone=row["customer_phone"],
            address=row["customer_address"],
            details=row["details"],
            assigned_to=assigned_to,
            created_by=row["created_by"],
            actor=actor,
            note=note or row["last_note"],
            resolution=resolution or row["resolution"],
            platform_url=platform_url,
            for_agent=True,
        )
    customer_text = ""
    if agent_id and (assigned_to or "").strip() and event_key in {"opened", "assigned"}:
        customer_text = complaint_technician_assigned_whatsapp_text(
            row["customer_name"],
            title=row["title"],
            technician=assigned_to,
        )

    agent_phone = _agent_phone(int(agent_id) if agent_id else None)
    group = _complaint_whatsapp_group()
    return {
        "row": row,
        "event": event_key,
        "assigned_to": assigned_to,
        "agent_id": agent_id,
        "agent_phone": agent_phone,
        "group_name": group,
        "group_text": group_text,
        "agent_text": agent_text,
        "customer_text": customer_text,
    }


def complaint_whatsapp_actions(
    complaint_id: int,
    *,
    event: str | None = None,
    actor: str | None = None,
    note: str | None = None,
    resolution: str | None = None,
    assigned_agent_id: int | None = None,
) -> dict:
    """Build wa.me links for phone browser + metadata for optional office auto-send."""
    ctx = _complaint_context(
        complaint_id,
        event=event,
        actor=actor,
        note=note,
        resolution=resolution,
        assigned_agent_id=assigned_agent_id,
    )
    if ctx is None:
        return {"ok": False, "error": "Complaint not found."}

    row = ctx["row"]
    links: list[dict] = []
    if settings.complaint_whatsapp_notify_group and ctx["group_text"]:
        share = whatsapp_share_url(ctx["group_text"])
        hint = ctx["group_name"] or "technician group"
        if share:
            links.append(
                {
                    "key": "group",
                    "label": "Technician group",
                    "hint": f"Pick “{hint}” in WhatsApp",
                    "url": share,
                }
            )
    if (
        settings.complaint_whatsapp_notify_agent
        and ctx["agent_text"]
        and ctx["agent_phone"]
    ):
        url = whatsapp_text_url(ctx["agent_phone"], ctx["agent_text"])
        if url:
            links.append(
                {
                    "key": "agent",
                    "label": f"Technician ({ctx['assigned_to'] or ctx['agent_phone'][-10:]})",
                    "hint": ctx["agent_phone"],
                    "url": url,
                }
            )
    elif settings.complaint_whatsapp_notify_agent and ctx["agent_id"]:
        pass

    if (
        settings.complaint_whatsapp_notify_customer
        and ctx["customer_text"]
        and row["customer_phone"]
    ):
        url = whatsapp_text_url(row["customer_phone"], ctx["customer_text"])
        if url:
            links.append(
                {
                    "key": "customer",
                    "label": f"Customer ({row['customer_name']})",
                    "hint": row["customer_phone"],
                    "url": url,
                }
            )

    warnings: list[str] = []
    if settings.complaint_whatsapp_notify_group and not ctx["group_name"]:
        warnings.append("Technicians group name not set (Settings → Agents).")
    if settings.complaint_whatsapp_notify_agent and ctx["agent_id"] and not ctx["agent_phone"]:
        warnings.append(
            f"No WhatsApp phone for {ctx['assigned_to'] or 'assigned agent'} "
            "(Settings → Agents)."
        )
    if settings.complaint_whatsapp_notify_customer and ctx["agent_id"] and not row["customer_phone"]:
        warnings.append("Customer phone missing on the profile.")
    if not ctx["agent_id"]:
        warnings.append("Assign a technician first — customer alert needs an assigned agent.")

    return {
        "ok": True,
        "complaint_id": complaint_id,
        "title": row["title"],
        "customer_name": row["customer_name"],
        "event": ctx["event"],
        "group_name": ctx["group_name"],
        "links": links,
        "warnings": warnings,
        "auto_send_available": settings.whatsapp_web_auto_send,
    }


def complaint_whatsapp_links_for_ui(
    complaint_id: int,
    *,
    assigned_agent_id: int | None = None,
    event: str | None = None,
) -> list[dict]:
    """wa.me links for templates — same pattern as payment WhatsApp buttons."""
    data = complaint_whatsapp_actions(
        complaint_id,
        event=event,
        assigned_agent_id=assigned_agent_id,
    )
    if not data.get("ok"):
        return []
    return list(data.get("links") or [])


def complaint_whatsapp_primary_url(
    complaint_id: int,
    *,
    assigned_agent_id: int | None = None,
    event: str | None = None,
) -> str:
    links = complaint_whatsapp_links_for_ui(
        complaint_id,
        assigned_agent_id=assigned_agent_id,
        event=event,
    )
    return links[0]["url"] if links else ""


def _send_complaint(
    *,
    event: str,
    complaint_id: int,
    actor: str | None = None,
    note: str | None = None,
    resolution: str | None = None,
    assigned_agent_id: int | None = None,
) -> dict:
    from .whatsapp_send import send_whatsapp_text_to_target

    ctx = _complaint_context(
        complaint_id,
        event=event,
        actor=actor,
        note=note,
        resolution=resolution,
        assigned_agent_id=assigned_agent_id,
    )
    if ctx is None:
        return {"ok": False, "error": "Complaint not found."}

    row = ctx["row"]
    text = ctx["group_text"]
    group = ctx["group_name"]
    agent_id = ctx["agent_id"]
    assigned_to = ctx["assigned_to"]
    agent_phone = ctx["agent_phone"]
    agent_text = ctx["agent_text"]
    customer_text = ctx["customer_text"]

    sent_any = False
    errors: list[str] = []
    sent_parts: list[str] = []

    if settings.complaint_whatsapp_notify_group and group:
        result = send_whatsapp_text_to_target(group, text)
        sent_any = sent_any or bool(result.get("ok"))
        if result.get("ok"):
            sent_parts.append(f"group ({group[:24]})")
        if not result.get("ok"):
            errors.append(result.get("error") or "Group send failed")
    elif settings.complaint_whatsapp_notify_group and not group:
        errors.append("Technicians group name not set (Settings → Agents)")

    if (
        settings.complaint_whatsapp_notify_agent
        and agent_phone
        and event in {"opened", "assigned"}
    ):
        result = send_whatsapp_text_to_target(agent_phone, agent_text)
        sent_any = sent_any or bool(result.get("ok"))
        if result.get("ok"):
            sent_parts.append(f"agent ({assigned_to or agent_phone[-10:]})")
        if not result.get("ok"):
            errors.append(result.get("error") or "Agent send failed")
    elif (
        settings.complaint_whatsapp_notify_agent
        and agent_id
        and event in {"opened", "assigned"}
        and not agent_phone
    ):
        errors.append(f"No WhatsApp phone for {assigned_to or 'assigned agent'}")

    if (
        settings.complaint_whatsapp_notify_customer
        and agent_id
        and (assigned_to or "").strip()
        and event in {"opened", "assigned"}
    ):
        customer_result = _deliver(
            customer_id=int(row["customer_id"]),
            connection_id=None,
            phone=row["customer_phone"],
            text=customer_text,
            kind="complaint_assigned",
        )
        sent_any = sent_any or bool(customer_result.get("ok"))
        if customer_result.get("ok"):
            sent_parts.append("customer")
        if not customer_result.get("ok"):
            errors.append(customer_result.get("error") or "Customer send failed")

    log_note = (
        f"Complaint #{complaint_id} WhatsApp ({event})"
        if sent_any
        else f"Complaint #{complaint_id} WhatsApp skipped ({event})"
    )
    if errors:
        log_note += ": " + "; ".join(errors[:2])
    with transaction() as conn:
        log_activity(
            conn,
            "whatsapp_sent" if sent_any else "whatsapp_failed",
            log_note,
            customer_id=int(row["customer_id"]),
            actor=actor,
        )
    if sent_any:
        return {"ok": True, "parts": sent_parts, "errors": errors}
    primary = errors[0] if errors else "Nothing to send (assign an agent first)."
    return {"ok": False, "error": primary, "errors": errors}
