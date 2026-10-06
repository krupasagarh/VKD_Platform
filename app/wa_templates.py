"""Editable WhatsApp message templates (Settings → WhatsApp templates).

Every WhatsApp text the platform builds — wa.me links and office auto-send — is
rendered from here. Admin edits are stored in the settings table; an untouched
template falls back to the default below.

Placeholders look like {name}. A line whose placeholder has no value for that
message (for example {renew_line} when no renewal was queued) is left out.
"""
from __future__ import annotations

import re
import threading
import time

SETTING_PREFIX = "wa_template."

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

_SIGN_OFF = "Thanks,\nVK DIGITAL"

TEMPLATES: list[dict] = [
    # ---------------------------------------------------------------- customers
    {
        "key": "payment_received",
        "group": "customer",
        "label": "Payment confirmation",
        "used_for": "After a payment is collected (auto-send and the “Payment confirmation” button).",
        "placeholders": {
            "name": "Customer name",
            "service": "Railtel / Hathway cable TV / ANT IPTV / SmartPlay OTT",
            "amount": "Amount received, e.g. ₹500 (“your payment” when not known)",
            "balance": "Balance due now, e.g. ₹200 or Nil",
            "balance_line": "“Balance due now: ₹200.” or “Nothing outstanding. Thank you.”",
            "renew_line": "“Renewal will be processed shortly.” when a renew was queued",
        },
        "default": (
            "Hi {name},\n"
            "\n"
            "Received {amount} for your {service}.\n"
            "{balance_line}\n"
            "{renew_line}\n"
            "Please call or msg if there is any issue.\n"
            "\n" + _SIGN_OFF
        ),
    },
    {
        "key": "renew_followup",
        "group": "customer",
        "label": "Payment reminder",
        "used_for": "“Payment reminder” in the WhatsApp menu — service renewed, payment pending.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
        },
        "default": (
            "Hi {name},\n"
            "\n"
            "Your {service} service has been renewed. Please make the payment/"
            "If already payment is done you can ignore this msg.\n" + _SIGN_OFF
        ),
    },
    {
        "key": "balance_reminder",
        "group": "customer",
        "label": "Balance due reminder",
        "used_for": "“Balance reminder” on the customer page — total outstanding amount.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
            "amount": "Total balance, e.g. ₹1,200",
        },
        "default": (
            "Hi {name},\n"
            "Your {service}'s service balance amount with us is pending. "
            "Please make the payment and help us in keeping the service uninterrupted.\n"
            "Total Balance: {amount}\n" + _SIGN_OFF
        ),
    },
    {
        "key": "expired",
        "group": "customer",
        "label": "Renewal reminder (expired)",
        "used_for": "Expired lists and “Renewal reminder” in the WhatsApp menu.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
        },
        "default": (
            "Hi {name},\n"
            "Your {service} has expired, please call or msg for renewing the services.\n"
            + _SIGN_OFF
        ),
    },
    {
        "key": "expiring_tonight",
        "group": "customer",
        "label": "Expiring tonight",
        "used_for": "Expiring tonight list.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
        },
        "default": (
            "Hi {name},\n"
            "\n"
            "Your {service} is expiring tonight. Please let me know for renewal on msg "
            "or you can call me morning.\n"
            "\n" + _SIGN_OFF
        ),
    },
    {
        "key": "renewed",
        "group": "customer",
        "label": "Renewed / recharged",
        "used_for": "Sent automatically after a portal renew succeeds.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
            "up_line": "“Internet would be up in few min” (Railtel) or “it should be up in few min”",
        },
        "default": (
            "Hi {name},\n"
            "Your {service} has renewed/recharged, {up_line}. If not please call or msg\n"
            + _SIGN_OFF
        ),
    },
    {
        "key": "invoice_link",
        "group": "customer",
        "label": "Railtel bill link",
        "used_for": "WhatsApp link with the bill download URL.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
            "invoice_no": "Bill number",
            "download_url": "Link to the bill PDF",
        },
        "default": (
            "Hi {name},\n"
            "\n"
            "Your {service} bill ({invoice_no}) is ready:\n"
            "{download_url}\n"
            "\n" + _SIGN_OFF
        ),
    },
    {
        "key": "invoice_caption",
        "group": "customer",
        "label": "Railtel bill PDF caption",
        "used_for": "Caption when the office WhatsApp sends the bill PDF itself.",
        "placeholders": {
            "name": "Customer name",
            "service": "Service name",
            "invoice_no": "Bill number",
        },
        "default": (
            "Hi {name},\n"
            "\n"
            "Please find your {service} bill ({invoice_no}).\n"
            "\n" + _SIGN_OFF
        ),
    },
    {
        "key": "complaint_customer",
        "group": "customer",
        "label": "Complaint — technician assigned",
        "used_for": "Tells the customer who will attend their complaint.",
        "placeholders": {
            "name": "Customer name",
            "title": "Complaint title",
            "technician": "Assigned technician",
        },
        "default": (
            "Hi {name},\n"
            "\n"
            "Your complaint ({title}) has been registered. "
            "Our technician {technician} has been assigned and will contact you shortly.\n"
            "\n" + _SIGN_OFF
        ),
    },
    # ------------------------------------------------------------- team alerts
    {
        "key": "complaint_opened",
        "group": "team",
        "label": "Complaint opened (technician group)",
        "used_for": "Technician group alert when a complaint is logged.",
        "placeholders": {
            "complaint_id": "Complaint number",
            "customer_block": "Customer name, ID, phone, address and complaint (several lines)",
            "details": "Complaint details",
            "assigned_to": "Assigned technician",
            "created_by": "Who logged it",
        },
        "default": (
            "New complaint #{complaint_id}\n"
            "{customer_block}\n"
            "Details: {details}\n"
            "Assigned to: {assigned_to}\n"
            "Logged by: {created_by}"
        ),
    },
    {
        "key": "complaint_assigned",
        "group": "team",
        "label": "Complaint assigned (technician group)",
        "used_for": "Technician group alert when a complaint is assigned.",
        "placeholders": {
            "complaint_id": "Complaint number",
            "customer_block": "Customer details (several lines)",
            "assigned_to": "Assigned technician",
            "actor": "Who assigned it",
        },
        "default": (
            "Complaint #{complaint_id} assigned\n"
            "{customer_block}\n"
            "Assigned to: {assigned_to}\n"
            "By: {actor}"
        ),
    },
    {
        "key": "complaint_assigned_agent",
        "group": "team",
        "label": "Complaint assigned (to the technician)",
        "used_for": "Direct message to the assigned technician's phone.",
        "placeholders": {
            "complaint_id": "Complaint number",
            "customer_block": "Customer details (several lines)",
            "actor": "Who assigned it",
        },
        "default": (
            "Complaint #{complaint_id} assigned to you\n"
            "{customer_block}\n"
            "By: {actor}"
        ),
    },
    {
        "key": "complaint_note",
        "group": "team",
        "label": "Complaint follow-up note",
        "used_for": "Technician group alert when a follow-up note is added.",
        "placeholders": {
            "complaint_id": "Complaint number",
            "customer_block": "Customer details (several lines)",
            "note": "The note",
            "actor": "Who added it",
        },
        "default": (
            "Follow-up on complaint #{complaint_id}\n"
            "{customer_block}\n"
            "Note: {note}\n"
            "By: {actor}"
        ),
    },
    {
        "key": "complaint_fixed",
        "group": "team",
        "label": "Complaint fixed",
        "used_for": "Technician group alert when a complaint is closed.",
        "placeholders": {
            "complaint_id": "Complaint number",
            "customer_block": "Customer details (several lines)",
            "resolution": "What was done",
            "actor": "Who fixed it",
        },
        "default": (
            "Complaint #{complaint_id} fixed\n"
            "{customer_block}\n"
            "Resolution: {resolution}\n"
            "Fixed by: {actor}"
        ),
    },
]

TEMPLATES_BY_KEY: dict[str, dict] = {t["key"]: t for t in TEMPLATES}

GROUP_LABELS = {
    "customer": "Messages to customers",
    "team": "Alerts to technicians",
}

SAMPLE_VALUES: dict[str, str] = {
    "name": "Ramesh K",
    "service": "Railtel",
    "amount": "₹589",
    "balance": "₹200",
    "balance_line": "Balance due now: ₹200.",
    "renew_line": "Renewal will be processed shortly.",
    "up_line": "Internet would be up in few min",
    "invoice_no": "RWKA09-26-014710",
    "download_url": "https://vkdigital.tipturbroadband.in/railtel-invoices/12/pdf",
    "title": "No internet",
    "technician": "Dilip",
    "complaint_id": "142",
    "customer_block": (
        "Customer Name: Ramesh K\nCustomer ID: ka.rameshk\nPhone: 9876543210\n"
        "Address: CN Road, Tiptur\nComplaint: No internet"
    ),
    "details": "Red light on router since morning",
    "assigned_to": "Dilip",
    "created_by": "Krupasagar",
    "actor": "Krupasagar",
    "note": "Fibre cut near the pole, will fix by evening",
    "resolution": "Fibre spliced, line up",
}

_CACHE_TTL = 30.0
_cache: dict[str, str] = {}
_cache_at = 0.0
_cache_lock = threading.Lock()


def _load_overrides() -> dict[str, str]:
    global _cache, _cache_at
    now = time.monotonic()
    with _cache_lock:
        if _cache_at and now - _cache_at < _CACHE_TTL:
            return _cache
    from .db import connection

    try:
        with connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE ?",
                (SETTING_PREFIX + "%",),
            ).fetchall()
        data = {
            row["key"][len(SETTING_PREFIX):]: row["value"]
            for row in rows
            if (row["value"] or "").strip()
        }
    except Exception:
        data = {}
    with _cache_lock:
        _cache = data
        _cache_at = now
    return data


def clear_cache() -> None:
    global _cache_at
    with _cache_lock:
        _cache_at = 0.0


def get_template(key: str) -> str:
    spec = TEMPLATES_BY_KEY[key]
    return _load_overrides().get(key) or spec["default"]


def is_customised(key: str) -> bool:
    return key in _load_overrides()


def _normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


def save_template(conn, key: str, text: str) -> None:
    """Store an edited template; blank or unchanged-from-default removes the override."""
    spec = TEMPLATES_BY_KEY[key]
    body = _normalise(text)
    if not body or body == _normalise(spec["default"]):
        conn.execute("DELETE FROM settings WHERE key = ?", (SETTING_PREFIX + key,))
    else:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTING_PREFIX + key, body),
        )
    clear_cache()


def reset_template(conn, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (SETTING_PREFIX + key,))
    clear_cache()


def unknown_placeholders(key: str, text: str) -> list[str]:
    allowed = set(TEMPLATES_BY_KEY[key]["placeholders"])
    return sorted({p for p in _PLACEHOLDER_RE.findall(text or "") if p not in allowed})


def render_text(text: str, values: dict) -> str:
    """Fill placeholders; drop lines whose placeholder is empty for this message."""
    out: list[str] = []
    for line in (text or "").split("\n"):
        used = [p for p in _PLACEHOLDER_RE.findall(line) if p in values]
        if used and any(not str(values.get(p) or "").strip() for p in used):
            continue
        out.append(
            _PLACEHOLDER_RE.sub(
                lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                line,
            )
        )
    # Collapse blank runs left behind by dropped lines.
    cleaned: list[str] = []
    for line in out:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def render(key: str, **values) -> str:
    spec = TEMPLATES_BY_KEY[key]
    full = {p: values.get(p, "") for p in spec["placeholders"]}
    return render_text(get_template(key), full)


def preview(key: str, text: str | None = None) -> str:
    spec = TEMPLATES_BY_KEY[key]
    sample = {p: SAMPLE_VALUES.get(p, p) for p in spec["placeholders"]}
    return render_text(text if text is not None else get_template(key), sample)
