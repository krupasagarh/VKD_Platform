"""Customer messaging helpers (WhatsApp compose links)."""
from __future__ import annotations

import re
from urllib.parse import quote

# Wording customers see in WhatsApp — one label per provider connection type.
SERVICE_LABELS: dict[str, str] = {
    "railtel": "Railtel WiFi/Broadband",
    "hathway": "Hathway cable TV",
    "iptv": "ANT IPTV",
    "ott": "SmartPlay OTT",
}

EXPIRED_REMINDER_TEMPLATE = (
    "Hi {name},\n"
    "Your {service} has expired, please call or msg for renewing the services.\n"
    "Thanks,\n"
    "VK DIGITAL"
)

INVOICE_TEMPLATE = (
    "Hi {name},\n"
    "\n"
    "Your {service} bill ({invoice_no}) is ready:\n"
    "{download_url}\n"
    "\n"
    "Thanks,\n"
    "VK DIGITAL"
)

RENEW_FOLLOWUP_TEMPLATE = (
    "Hi {name},\n"
    "\n"
    "Your {service} service has been renewed. Please make the payment/"
    "If already payment is done you can ignore this msg.\n"
    "Thanks,\n"
    "VK DIGITAL"
)


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


def _whatsapp_compose_url(
    name: str | None,
    phone: str | None,
    template: str,
    *,
    provider: str | None = None,
    providers_csv: str | None = None,
) -> str | None:
    wa_phone = normalize_wa_phone(phone)
    if not wa_phone:
        return None
    customer = (name or "Customer").strip() or "Customer"
    service = service_label(provider, providers_csv=providers_csv)
    # Brace-safe: customer names must not break str.format.
    msg = template.replace("{name}", customer, 1).replace("{service}", service, 1)
    return f"https://wa.me/{wa_phone}?text={quote(msg)}"


def expired_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
) -> str | None:
    """WhatsApp compose URL for an expired-service reminder, or None if no phone."""
    return _whatsapp_compose_url(name, phone, EXPIRED_REMINDER_TEMPLATE, provider=provider)


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
    customer = (name or "Customer").strip() or "Customer"
    service = service_label(provider)
    inv = (invoice_no or "bill").strip()
    msg = (
        INVOICE_TEMPLATE.replace("{name}", customer, 1)
        .replace("{service}", service, 1)
        .replace("{invoice_no}", inv, 1)
        .replace("{download_url}", download_url.strip(), 1)
    )
    return f"https://wa.me/{wa_phone}?text={quote(msg)}"


def renew_followup_whatsapp_url(
    name: str | None,
    phone: str | None,
    provider: str | None = None,
    *,
    providers_csv: str | None = None,
) -> str | None:
    """WhatsApp compose URL after renew + collect later — chase promised payment."""
    return _whatsapp_compose_url(
        name,
        phone,
        RENEW_FOLLOWUP_TEMPLATE,
        provider=provider,
        providers_csv=providers_csv,
    )
