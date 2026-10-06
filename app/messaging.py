"""Customer messaging helpers (WhatsApp compose links).

Wording lives in `wa_templates` (editable under Settings → WhatsApp templates).
"""
from __future__ import annotations

import re
from urllib.parse import quote

from . import wa_templates

# Wording customers see in WhatsApp — one label per provider connection type.
SERVICE_LABELS: dict[str, str] = {
    "railtel": "Railtel",
    "hathway": "Hathway cable TV",
    "iptv": "ANT IPTV",
    "ott": "SmartPlay OTT",
}


def service_label(provider: str | None = None, *, providers_csv: str | None = None) -> str:
    """Human service name for the expiry reminder (respects list filter when set)."""
    key = (provider or "").strip().lower()
    if key in SERVICE_LABELS:
        return SERVICE_LABELS[key]
    # Page filter not set — infer from the customer's connection(s).
    parts = [p.strip().lower() for p in (providers_csv or provider or "").split(",") if p.strip()]
    if len(parts) == 1:
        return SERVICE_LABELS.get(parts[0], parts[0])
    if len(parts) > 1:
        labels = [SERVICE_LABELS.get(p, p) for p in parts]
        if len(labels) == 2:
            return f"{labels[0]} and {labels[1]}"
        return ", ".join(labels[:-1]) + f", and {labels[-1]}"
    return "service"


def normalize_wa_phone(phone: str | None) -> str | None:
    """Return digits for wa.me (91 + 10-digit Indian mobile when possible)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone.strip())
    if not digits:
        return None
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits if len(digits) >= 10 else None


def simple_customer_text(
    key: str,
    name: str | None,
    provider: str | None = None,
    *,
    providers_csv: str | None = None,
) -> str:
    """Templates that only need {name} and {service}."""
    return wa_templates.render(
        key,
        name=_safe_name(name),
        service=service_label(provider, providers_csv=providers_csv),
    )


def _whatsapp_compose_url(
    name: str | None,
    phone: str | None,
    key: str,
    *,
    provider: str | None = None,
    providers_csv: str | None = None,
) -> str | None:
    wa_phone = normalize_wa_phone(phone)
    if not wa_phone:
        return None
    msg = simple_customer_text(key, name, provider, providers_csv=providers_csv)
    return f"https://wa.me/{wa_phone}?text={quote(msg)}"


def expired_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
    providers_csv: str | None = None,
) -> str | None:
    """WhatsApp compose URL for an expired-service reminder, or None if no phone."""
    return _whatsapp_compose_url(
        name, phone, "expired", provider=provider, providers_csv=providers_csv
    )


def expiring_tonight_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
    providers_csv: str | None = None,
) -> str | None:
    """WhatsApp compose URL — service expires tonight."""
    return _whatsapp_compose_url(
        name, phone, "expiring_tonight", provider=provider, providers_csv=providers_csv
    )


def expired_whatsapp_text(
    name: str | None,
    provider: str | None = None,
    *,
    providers_csv: str | None = None,
) -> str:
    return simple_customer_text("expired", name, provider, providers_csv=providers_csv)


def invoice_whatsapp_target_phone(customer_phone: str | None) -> str | None:
    """While testing, send invoice links to RAILTEL_INVOICE_WHATSAPP_TEST instead of the customer."""
    from .config import settings

    test = (settings.railtel_invoice_whatsapp_test or "").strip()
    if test:
        return test
    return customer_phone


def invoice_whatsapp_url(
    name: str | None,
    phone: str | None,
    *,
    invoice_no: str,
    download_url: str,
    provider: str = "railtel",
) -> str | None:
    """WhatsApp compose URL with a link to the hosted portal bill PDF."""
    wa_phone = normalize_wa_phone(invoice_whatsapp_target_phone(phone))
    if not wa_phone or not (download_url or "").strip():
        return None
    msg = wa_templates.render(
        "invoice_link",
        name=_safe_name(name),
        service=service_label(provider),
        invoice_no=(invoice_no or "bill").strip(),
        download_url=download_url.strip(),
    )
    return f"https://wa.me/{wa_phone}?text={quote(msg)}"


def invoice_caption_text(name: str | None, invoice_no: str, provider: str = "railtel") -> str:
    """Caption when the office WhatsApp sends the bill PDF."""
    return wa_templates.render(
        "invoice_caption",
        name=_safe_name(name),
        service=service_label(provider),
        invoice_no=(invoice_no or "bill").strip(),
    )


def renew_followup_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
    *,
    providers_csv: str | None = None,
) -> str | None:
    """WhatsApp compose URL after renew + collect later — chase promised payment."""
    return _whatsapp_compose_url(
        name, phone, "renew_followup", provider=provider, providers_csv=providers_csv
    )


def _safe_name(name: str | None) -> str:
    return (name or "Customer").strip() or "Customer"


def renewed_whatsapp_text(
    name: str | None, provider: str | None = None, *, providers_csv: str | None = None
) -> str:
    """Auto-sent after a portal renew succeeds."""
    key = (provider or "").strip().lower()
    if not key and (providers_csv or "").strip().lower() == "railtel":
        key = "railtel"
    if key == "railtel":
        up_line = "Internet would be up in few min"
    else:
        up_line = "it should be up in few min"
    return wa_templates.render(
        "renewed",
        name=_safe_name(name),
        service=service_label(provider, providers_csv=providers_csv),
        up_line=up_line,
    )


def payment_received_whatsapp_text(
    name: str | None,
    provider: str | None = None,
    *,
    providers_csv: str | None = None,
    amount_paise: int | None = None,
    remaining_paise: int | None = None,
    renew_queued: bool = False,
) -> str:
    """Text for the payment-received confirmation shown to the customer."""
    from .money import fmt_rupees

    if amount_paise is not None and int(amount_paise) > 0:
        amount = f"₹{fmt_rupees(amount_paise)}"
    else:
        amount = "your payment"
    balance = balance_line = ""
    if remaining_paise is not None:
        if int(remaining_paise) <= 0:
            balance = "Nil"
            balance_line = "Nothing outstanding. Thank you."
        else:
            balance = f"₹{fmt_rupees(remaining_paise)}"
            balance_line = f"Balance due now: {balance}."
    return wa_templates.render(
        "payment_received",
        name=_safe_name(name),
        service=service_label(provider, providers_csv=providers_csv),
        amount=amount,
        balance=balance,
        balance_line=balance_line,
        renew_line="Renewal will be processed shortly." if renew_queued else "",
    )


def complaint_technician_assigned_whatsapp_text(
    name: str | None,
    *,
    title: str,
    technician: str,
) -> str:
    """Tell the customer a technician is assigned for their complaint."""
    return wa_templates.render(
        "complaint_customer",
        name=_safe_name(name),
        title=(title or "your complaint").strip() or "your complaint",
        technician=(technician or "our technician").strip() or "our technician",
    )


def whatsapp_text_url(phone: str | None, text: str) -> str | None:
    """wa.me link to the customer's own number. Never uses the invoice test intercept."""
    wa_phone = normalize_wa_phone(phone)
    body = (text or "").strip()
    if not wa_phone or not body:
        return None
    return f"https://wa.me/{wa_phone}?text={quote(body)}"


def whatsapp_share_url(text: str) -> str | None:
    """wa.me link with no phone — opens WhatsApp so the user picks a group or contact."""
    body = (text or "").strip()
    if not body:
        return None
    return f"https://wa.me/?text={quote(body)}"


def renewed_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
    providers_csv: str | None = None,
) -> str | None:
    return whatsapp_text_url(
        phone, renewed_whatsapp_text(name, provider, providers_csv=providers_csv)
    )


def balance_reminder_whatsapp_text(
    name: str | None,
    due_paise: int,
    provider: str | None = None,
    *,
    providers_csv: str | None = None,
) -> str:
    from .money import fmt_rupees

    return wa_templates.render(
        "balance_reminder",
        name=_safe_name(name),
        service=service_label(provider, providers_csv=providers_csv),
        amount=f"₹{fmt_rupees(due_paise)}",
    )


def balance_reminder_whatsapp_url(
    name: str | None,
    phone: str | None,
    due_paise: int | None,
    provider: str | None = None,
    providers_csv: str | None = None,
) -> str | None:
    """Chase an outstanding balance. None when nothing is due or no phone."""
    if not due_paise or int(due_paise) <= 0:
        return None
    return whatsapp_text_url(
        phone,
        balance_reminder_whatsapp_text(name, int(due_paise), provider, providers_csv=providers_csv),
    )


def payment_received_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
    providers_csv: str | None = None,
    *,
    amount_paise: int | None = None,
    remaining_paise: int | None = None,
    renew_queued: bool = False,
) -> str | None:
    return whatsapp_text_url(
        phone,
        payment_received_whatsapp_text(
            name,
            provider,
            providers_csv=providers_csv,
            amount_paise=amount_paise,
            remaining_paise=remaining_paise,
            renew_queued=renew_queued,
        ),
    )


def _complaint_customer_block(
    *,
    customer_name: str | None,
    customer_service_id: str | None = None,
    customer_phone: str | None = None,
    address: str | None = None,
    complaint_title: str | None = None,
) -> str:
    """Customer lines for complaint WhatsApp — Railtel login or STB as Customer ID."""
    lines = [
        f"Customer Name: {(customer_name or 'Customer').strip() or 'Customer'}",
    ]
    service_id = (customer_service_id or "").strip()
    if service_id:
        lines.append(f"Customer ID: {service_id}")
    phone = (customer_phone or "").strip()
    if phone:
        lines.append(f"Phone: {phone}")
    addr = (address or "").strip()
    if addr:
        lines.append(f"Address: {addr}")
    title = (complaint_title or "").strip()
    if title:
        lines.append(f"Complaint: {title}")
    return "\n".join(lines)


def _complaint_customer_line(
    *,
    customer_name: str | None,
    customer_code: str | None,
    customer_phone: str | None,
    address: str | None = None,
    customer_service_id: str | None = None,
    complaint_title: str | None = None,
) -> str:
    service_id = (customer_service_id or "").strip() or (customer_code or "").strip()
    return _complaint_customer_block(
        customer_name=customer_name,
        customer_service_id=service_id or None,
        customer_phone=customer_phone,
        address=address,
        complaint_title=complaint_title,
    )


def complaint_whatsapp_text(
    event: str,
    *,
    complaint_id: int,
    title: str,
    customer_name: str | None = None,
    customer_code: str | None = None,
    customer_phone: str | None = None,
    customer_service_id: str | None = None,
    address: str | None = None,
    details: str | None = None,
    assigned_to: str | None = None,
    created_by: str | None = None,
    actor: str | None = None,
    note: str | None = None,
    resolution: str | None = None,
    platform_url: str | None = None,
    for_agent: bool = False,
) -> str:
    """Plain-text alert for technicians / assigned agents."""
    event = (event or "").strip().lower()
    title = (title or "Complaint").strip() or "Complaint"
    customer_block = _complaint_customer_line(
        customer_name=customer_name,
        customer_code=customer_code,
        customer_phone=customer_phone,
        customer_service_id=customer_service_id,
        address=address,
        complaint_title=title,
    )
    values = {
        "complaint_id": str(complaint_id),
        "customer_block": customer_block,
        "details": (details or "").strip(),
        "assigned_to": (assigned_to or "").strip(),
        "created_by": (created_by or "").strip(),
        "actor": (actor or "").strip(),
        "note": (note or "").strip() or "—",
        "resolution": (resolution or "").strip() or "Fixed",
    }
    if event == "opened":
        key = "complaint_opened"
    elif event == "assigned":
        key = "complaint_assigned_agent" if for_agent else "complaint_assigned"
    elif event == "note":
        key = "complaint_note"
    elif event == "fixed":
        key = "complaint_fixed"
    else:
        return f"Complaint #{complaint_id}\n{customer_block}"
    return wa_templates.render(key, **values)
