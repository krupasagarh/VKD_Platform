"""Adapters over railtel_debugger/vk_agent.

The Telegram bot already has working Playwright automation for both providers, and
those functions are plain `f(subscriber_id, account_id=None) -> dict`. This module
imports them lazily and flattens their different result shapes into one
`UpstreamResult`, so the rest of the platform never needs provider-specific code.

In `simulate` mode nothing is imported and no browser opens — the caller gets a
successful result with no expiry date, and billing falls back to plan validity.
That makes the whole payment -> renew -> bill flow testable without spending money
on provider wallets.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from ..config import VK_AGENT_DIR, settings
from ..money import fmt_date, fmt_datetime, parse_date

PROVIDERS = ("railtel", "hathway", "iptv", "ott")

PROVIDER_LABELS = {
    "railtel": "Railtel / Railwire",
    "hathway": "Hathway",
    "iptv": "ANT IPTV",
    "ott": "SmartPlay OTT",
}

# Actions that run against one connection.
PROVIDER_ACTIONS: dict[str, tuple[str, ...]] = {
    "railtel": ("renew", "status", "clear_session", "download_bill"),
    "hathway": ("renew", "status", "retrack", "deactivate", "activate", "remove_terminate"),
    "iptv": ("renew", "status", "subscribe"),
    "ott": ("renew", "status"),
}

# Actions that run against the dealer account rather than a customer.
ACCOUNT_ACTIONS: dict[str, tuple[str, ...]] = {
    "railtel": ("wallet", "online", "sync"),
    "hathway": ("wallet",),
    "iptv": ("wallet",),
    "ott": ("wallet", "sync"),
}

ACTION_LABELS = {
    "renew": "Renew",
    "status": "Check status",
    "clear_session": "Clear session",
    "download_bill": "Download portal bill",
    "retrack": "Retrack",
    "deactivate": "Temporary deactivate",
    "activate": "Reactivate",
    "remove_terminate": "Remove pack + terminate",
    "wallet": "Refresh wallet & counts",
    "online": "Refresh online list",
    "subscribe": "Subscribe on ANT",
    "sync": "Sync subscribers from portal",
}

# Actions that change the customer's service and should be confirmed carefully.
DESTRUCTIVE_ACTIONS = ("remove_terminate", "deactivate")

# Portal answers that will read the same way however often we ask. Retrying these buys
# nothing and costs a full login plus a CAPTCHA solve each time, so the job stops and
# waits for a human instead.
PERMANENT_ERROR_PATTERNS = (
    "stb is terminated",
    "stb not found",
    "no such stb",
    "invalid stb",
    "subscriber not found",
    "customer not found",
    "no record found",
    "not a valid",
    "already active",
    "already deactivated",
    "does not support",
    "is a railtel login",
    "is a hathway set-top box number",
    "not a hathway set-top box number",
    "does not look like a railtel login",
    "does not look like an iptv phone",
    "does not look like a smartplay",
    "is an ant iptv phone",
    "is a smartplay ott phone",
    "no provider id",
    "assign an lco",
    "direct activation is not allowed",
    "direct customer activation",
    "not expired yet",
    "still active until",
    "top-up was not started",
)


def is_permanent_error(error: str) -> bool:
    """True when the same request would fail identically on a retry."""
    text = (error or "").strip().lower()
    return any(pattern in text for pattern in PERMANENT_ERROR_PATTERNS)


# A status check can tell us the box is gone; reflect that locally instead of billing it.
TERMINATED_PATTERNS = ("stb is terminated", "stb not found", "no such stb")


def reports_terminated(error: str) -> bool:
    text = (error or "").strip().lower()
    return any(pattern in text for pattern in TERMINATED_PATTERNS)

# Hathway set-top boxes are N + 11 digits, viewing cards are T + 12 digits.
# Railtel/Railwire logins look like "ka.something".
# ANT IPTV subscribers are looked up by the 10-digit mobile used on the CRM.
HATHWAY_STB_RE = re.compile(r"^(N\d{11}|T\d{12})$", re.IGNORECASE)
RAILTEL_ID_RE = re.compile(r"^ka\.[a-z0-9._-]+$", re.IGNORECASE)
IPTV_PHONE_RE = re.compile(r"^\d{10}$")


class UpstreamUnsupported(RuntimeError):
    """Raised when a provider does not support the requested action."""


@dataclass
class UpstreamResult:
    ok: bool
    provider: str
    action: str
    upstream_id: str
    message: str = ""
    error: str = ""
    expiry: str = ""
    plan_name: str = ""
    simulated: bool = False
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "action": self.action,
            "upstream_id": self.upstream_id,
            "message": self.message,
            "error": self.error,
            "expiry": self.expiry,
            "plan_name": self.plan_name,
            "simulated": self.simulated,
            "raw": self.raw,
        }


# --------------------------------------------------------------------------- #
# Identifier validation
# --------------------------------------------------------------------------- #

def id_problem(provider: str, upstream_id: str) -> str | None:
    """Explain why this id cannot be used on this provider's portal, or None if fine.

    Worth checking before every run: the Bix export files a few broadband logins
    and placeholders like "NOBOX1" in the set-top box column, and driving the
    Hathway portal with a Railtel login wastes a real login attempt.
    """
    value = (upstream_id or "").strip()
    if not value:
        return "This connection has no provider id, so there is nothing to look up."

    if provider == "hathway":
        if HATHWAY_STB_RE.match(value):
            return None
        if RAILTEL_ID_RE.match(value):
            return (
                f"'{value}' is a Railtel login, not a Hathway set-top box number. "
                f"Change this connection's provider to Railtel."
            )
        return (
            f"'{value}' is not a Hathway set-top box number "
            f"(expected N + 11 digits, or T + 12 digits for a viewing card). "
            f"Fix the provider id before running portal actions."
        )

    if provider == "railtel":
        if RAILTEL_ID_RE.match(value):
            return None
        if HATHWAY_STB_RE.match(value):
            return (
                f"'{value}' is a Hathway set-top box number, not a Railtel login. "
                f"Change this connection's provider to Hathway."
            )
        # Railtel also accepts a 10-digit phone number as a search term.
        if IPTV_PHONE_RE.match(value):
            return None
        return (
            f"'{value}' does not look like a Railtel login (ka.username) or a "
            f"10-digit phone number. Fix the provider id before running portal actions."
        )

    if provider in ("iptv", "ott"):
        label = "ANT IPTV" if provider == "iptv" else "SmartPlay OTT"
        if IPTV_PHONE_RE.match(value):
            return None
        if RAILTEL_ID_RE.match(value):
            return (
                f"'{value}' is a Railtel login, not a {label} phone. "
                f"Change this connection's provider to Railtel."
            )
        if HATHWAY_STB_RE.match(value):
            return (
                f"'{value}' is a Hathway set-top box number, not a {label} phone. "
                f"Change this connection's provider to Hathway."
            )
        expected = (
            "the 10-digit mobile used on ANT CRM"
            if provider == "iptv"
            else "the 10-digit mobile used on SmartPlay"
        )
        return (
            f"'{value}' does not look like a {label} phone "
            f"(expected {expected}). "
            f"Fix the provider id before running portal actions."
        )

    return f"Unknown provider '{provider}'."


def normalise_upstream_id(provider: str, upstream_id: str) -> str:
    """Strip junk operators paste, and keep Hathway / IPTV ids in a portal-safe shape."""
    value = (upstream_id or "").strip().strip("'\"")
    provider = (provider or "").strip().lower()
    if provider == "hathway":
        return value.upper()
    if provider in ("iptv", "ott"):
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 10:
            return digits[-10:]
    return value


def can_run_actions(provider: str, upstream_id: str) -> bool:
    return id_problem(provider, upstream_id) is None


# --------------------------------------------------------------------------- #
# vk_agent loading
# --------------------------------------------------------------------------- #

def _ensure_agent_importable() -> None:
    path = str(VK_AGENT_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)


def _load(module_name: str, func_name: str) -> Callable:
    _ensure_agent_importable()
    try:
        module = __import__(module_name)
    except ImportError as exc:  # playwright/pytesseract missing, or path wrong
        raise UpstreamUnsupported(
            f"Cannot import {module_name} from {VK_AGENT_DIR}. "
            f"Install the vk_agent dependencies (playwright, pytesseract, Pillow) "
            f"or set VK_PLATFORM_UPSTREAM_MODE=simulate. Original error: {exc}"
        ) from exc
    func = getattr(module, func_name, None)
    if func is None:
        raise UpstreamUnsupported(f"{module_name}.{func_name} not found in vk_agent")
    return func


def _account_for(provider: str) -> str | None:
    if provider == "railtel":
        value = settings.railtel_account
    elif provider == "hathway":
        value = settings.hathway_account
    elif provider == "iptv":
        value = settings.iptv_account
    elif provider == "ott":
        value = getattr(settings, "ott_account", "") or ""
    else:
        value = ""
    return value or None


def _first_text(raw: dict, *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return ""


def _simulated(provider: str, action: str, upstream_id: str) -> UpstreamResult:
    raw = {"simulated": True}
    if action == "wallet":
        raw.update({"wallet_balance": "", "note": "no balance is fetched in simulate mode"})
    if action == "download_bill":
        raw.update({
            "file_path": "",
            "file_name": "SIM-NAMRATHAOIL Sep2026_RWKA.pdf",
            "invoice_no": "RWKA09/26/009609",
            "receipt_date": "11-Sep-2026",
            "note": "no portal is contacted in simulate mode",
        })
    if action == "online":
        raw.update({
            "online_count": 2,
            "upload_gb": "1.2GB",
            "download_gb": "10GB",
            "total_gb": "11.2GB",
            "nas": [{"address": "172.31.0.1", "count": "2"}],
            "subscribers": [
                {
                    "session_id": "1001",
                    "username": "ka.demo1",
                    "mac": "aa-bb-cc-dd-ee-01",
                    "framed_ip": "100.1.1.1",
                    "start_time": "13-09-2026 10:00:00 AM",
                    "total_time": "0 days, 1 hours, 0 minutes",
                    "upload_mb": "1",
                    "download_mb": "10",
                    "total_mb": "11",
                },
                {
                    "session_id": "1002",
                    "username": "ka.demo2",
                    "mac": "aa-bb-cc-dd-ee-02",
                    "framed_ip": "100.1.1.2",
                    "start_time": "13-09-2026 11:30:00 AM",
                    "total_time": "0 days, 0 hours, 30 minutes",
                    "upload_mb": "0.2",
                    "download_mb": "3",
                    "total_mb": "3.2",
                },
            ],
            "note": "no portal is contacted in simulate mode",
        })
    return UpstreamResult(
        ok=True,
        provider=provider,
        action=action,
        upstream_id=upstream_id,
        message=f"Simulated {ACTION_LABELS.get(action, action)} — provider portal was not contacted.",
        simulated=True,
        raw=raw,
    )


def _normalise(provider: str, action: str, upstream_id: str, raw: dict) -> UpstreamResult:
    raw = raw if isinstance(raw, dict) else {"result": raw}
    ok = bool(raw.get("success"))
    error = _first_text(raw, "error")

    if raw.get("railtel_not_expired"):
        ok = False
        expiry = _first_text(raw, "expiry")
        error = _first_text(raw, "error") or (
            f"Railtel is not expired yet — valid until {expiry or '?'}. Top-up was not started."
        )

    if raw.get("railtel_insufficient_bss_for_topup"):
        ok = False
        balance = _first_text(raw, "account_balance")
        needed = _first_text(raw, "renewal_amount")
        error = (
            f"Railtel partner wallet is too low to top up this account "
            f"(balance {balance or '?'}, renewal needs {needed or '?'}). "
            f"Recharge the partner wallet and retry."
        )

    expiry = fmt_date(parse_date(_first_text(raw, "expiry", "hathway_valid_upto", "valid_upto")))
    plan_name = _first_text(raw, "hathway_plan_name", "plan_name", "package_name")
    message = _first_text(raw, "message", "downtime", "matched_cid")
    if action == "online":
        count = raw.get("online_count")
        if count not in (None, ""):
            message = raw.get("message") or f"{count} subscriber(s) online on Railtel"
    if action == "sync" and provider == "railtel":
        message = raw.get("message") or f"{raw.get('row_count', 0)} Railtel subscriber(s) synced"

    return UpstreamResult(
        ok=ok,
        provider=provider,
        action=action,
        upstream_id=upstream_id,
        message=message,
        error="" if ok else (error or "Provider portal did not confirm the action."),
        expiry=expiry,
        plan_name=plan_name,
        raw=raw,
    )


def card_number_from(provider: str, raw: dict) -> str:
    """The viewing card / set-top box number a status check reported, or "".

    Both portals return a field called `mac`, but they mean different things: Hathway puts
    the viewing card there (T + 12 digits), while Railtel puts the customer router's
    network MAC (`e8-48-b8-3e-18-9e`). Only the former belongs in a card number field, so
    the value has to look like a card before we keep it.
    """
    if provider != "hathway":
        return ""
    value = str((raw or {}).get("mac") or "").strip()
    if HATHWAY_STB_RE.match(value):
        return value
    return ""


def link_state_from(provider: str, raw: dict) -> dict:
    """When the Railtel line last came up or went down.

    Railtel's status check reports the current AAA session as a sentence in `downtime`:
    "Active since 09/09/26 12:10:08 PM", or "Down since ..." when the line is not up.
    That answers the question a customer actually rings about — "since when?" — so it is
    worth keeping rather than leaving buried in the job result.

    Hathway reuses the same `downtime` key for something else entirely (an information
    line, "VC ID: /VM/JVM | STB NO: ..."), and never reports a session at all, so this
    returns nothing for Hathway rather than inventing a date out of that text.
    """
    blank = {"state": "", "since": "", "days": None}
    if provider != "railtel":
        return blank

    raw = raw or {}
    text = str(raw.get("downtime") or "").strip()
    lowered = text.lower()
    if lowered.startswith("active"):
        state = "online"
    elif lowered.startswith("down"):
        state = "offline"
    elif raw.get("is_online") is True:
        state = "online"
    elif raw.get("is_online") is False:
        state = "offline"
    else:
        return blank

    days = raw.get("session_days")
    return {
        "state": state,
        # "Active Now" and "Down status unknown" carry no timestamp; the state still does.
        "since": fmt_datetime(text),
        "days": int(days) if isinstance(days, (int, float)) else None,
    }


def looks_like_network_mac(value: str) -> bool:
    """Six hex pairs separated by colons or dashes — a router MAC, not a viewing card."""
    return bool(re.fullmatch(r"(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", (value or "").strip(), re.I))


def wallet_summary(provider: str, raw: dict) -> dict:
    """Pull the few dealer-account numbers we display, out of either portal's shape."""
    if provider == "hathway":
        return {
            "wallet_balance": _first_text(raw, "actual_balance"),
            "active": _first_text(raw, "active_stb"),
            "inactive": _first_text(raw, "inactive_stb"),
            "total": _first_text(raw, "total_stb"),
            "operator": _first_text(raw, "hathway_network_name", "hathway_operator_user"),
        }
    if provider == "iptv":
        return {
            "wallet_balance": _first_text(raw, "wallet_balance", "iptv_wallet"),
            "active": _first_text(raw, "active", "subscriptions"),
            "inactive": "",
            "total": _first_text(raw, "total"),
            "operator": _first_text(raw, "lco", "operator"),
        }
    if provider == "ott":
        return {
            "wallet_balance": _first_text(raw, "wallet_balance"),
            "active": _first_text(raw, "active"),
            "inactive": _first_text(raw, "expired"),
            "total": _first_text(raw, "total"),
            "operator": _first_text(raw, "operator"),
        }
    return {
        "wallet_balance": _first_text(raw, "bss_account_balance"),
        "active": _first_text(raw, "active_subscribers"),
        "inactive": _first_text(raw, "validity_end_7_days"),
        "total": "",
        "operator": "",
    }


# --------------------------------------------------------------------------- #
# Live calls
# --------------------------------------------------------------------------- #

def _railtel_live(
    action: str,
    upstream_id: str,
    account_id: str | None,
    extras: dict | None = None,
) -> dict:
    extras = extras or {}
    if action == "renew":
        return _load("portal", "check_railtel_renew_subscriber")(upstream_id, account_id=account_id)
    if action == "status":
        return _load("portal", "check_railtel_portal")(upstream_id, account_id=account_id)
    if action == "clear_session":
        return _load("portal", "check_clear_customer_session")(upstream_id, account_id=account_id)
    if action == "download_bill":
        return _load("portal", "check_railtel_download_invoice")(
            upstream_id,
            account_id=account_id,
            customer_name=extras.get("customer_name"),
            output_dir=extras.get("output_dir"),
        )
    if action == "wallet":
        return _load("portal", "check_railtel_billing_dashboard_kpis")(account_id=account_id)
    if action == "online":
        return _load("portal", "check_railtel_online_subscribers")(account_id=account_id)
    if action == "sync":
        return _load("portal", "check_railtel_sync_subscribers")(account_id=account_id)
    raise UpstreamUnsupported(f"Railtel does not support action '{action}'")


def _hathway_live(action: str, upstream_id: str, account_id: str | None) -> dict:
    if action == "renew":
        return _load("hathway_portal", "check_hathway_renew_stb")(upstream_id, account_id=account_id)
    if action == "status":
        return _load("hathway_portal", "check_hathway_portal")(upstream_id, account_id=account_id)
    if action == "retrack":
        return _load("hathway_portal", "check_hathway_retrack_stb")(upstream_id, account_id=account_id)
    if action == "deactivate":
        return _load("hathway_portal", "check_hathway_temp_deactivate")(upstream_id, account_id=account_id)
    if action == "activate":
        return _load("hathway_portal", "check_hathway_temp_activate")(upstream_id, account_id=account_id)
    if action == "remove_terminate":
        return _load("hathway_portal", "check_hathway_remove_pack_and_terminate")(
            upstream_id, account_id=account_id
        )
    if action == "wallet":
        return _load("hathway_portal", "check_hathway_dashboard_stats")(account_id=account_id)
    raise UpstreamUnsupported(f"Hathway does not support action '{action}'")


def _iptv_live(action: str, upstream_id: str, account_id: str | None, extras: dict | None) -> dict:
    kwargs = {"account_id": account_id, "extras": extras or {}}
    if action == "renew":
        return _load("ant_iptv_portal", "check_ant_iptv_renew")(upstream_id, **kwargs)
    if action == "status":
        return _load("ant_iptv_portal", "check_ant_iptv_status")(upstream_id, **kwargs)
    if action == "subscribe":
        return _load("ant_iptv_portal", "check_ant_iptv_subscribe")(upstream_id, **kwargs)
    if action == "wallet":
        return _load("ant_iptv_portal", "check_ant_iptv_wallet")(account_id=account_id, extras=extras or {})
    raise UpstreamUnsupported(f"ANT IPTV does not support action '{action}'")


def _ott_live(action: str, upstream_id: str, account_id: str | None, extras: dict | None) -> dict:
    kwargs = {"account_id": account_id, "extras": extras or {}}
    if action == "renew":
        return _load("smartplay_portal", "check_smartplay_renew")(upstream_id, **kwargs)
    if action == "status":
        return _load("smartplay_portal", "check_smartplay_status")(upstream_id, **kwargs)
    if action == "wallet":
        return _load("smartplay_portal", "check_smartplay_wallet")(**kwargs)
    if action == "sync":
        return _load("smartplay_portal", "check_smartplay_list")(**kwargs)
    raise UpstreamUnsupported(f"SmartPlay OTT does not support action '{action}'")


def run_action(
    provider: str, action: str, upstream_id: str, extras: dict | None = None
) -> UpstreamResult:
    """Execute one provider action. Blocking — call from the worker thread only."""
    provider = (provider or "").strip().lower()
    action = (action or "").strip().lower()
    upstream_id = (upstream_id or "").strip()

    if provider not in PROVIDERS:
        raise UpstreamUnsupported(f"Unknown provider '{provider}'")

    is_account_action = action in ACCOUNT_ACTIONS.get(provider, ())
    if not is_account_action and action not in PROVIDER_ACTIONS[provider]:
        raise UpstreamUnsupported(
            f"{PROVIDER_LABELS[provider]} does not support '{ACTION_LABELS.get(action, action)}'"
        )

    if not is_account_action:
        problem = id_problem(provider, upstream_id)
        if problem:
            return UpstreamResult(
                ok=False, provider=provider, action=action, upstream_id=upstream_id, error=problem
            )

    if not settings.is_live and not (provider == "ott" and action == "sync"):
        return _simulated(provider, action, upstream_id)

    account_id = _account_for(provider)
    try:
        if provider == "railtel":
            raw = _railtel_live(action, upstream_id, account_id, extras)
        elif provider == "hathway":
            raw = _hathway_live(action, upstream_id, account_id)
        elif provider == "iptv":
            raw = _iptv_live(action, upstream_id, account_id, extras)
        else:
            raw = _ott_live(action, upstream_id, account_id, extras)
    except UpstreamUnsupported:
        raise
    except Exception as exc:  # portal automation blew up mid-run
        return UpstreamResult(
            ok=False,
            provider=provider,
            action=action,
            upstream_id=upstream_id,
            error=f"{type(exc).__name__}: {exc}",
        )

    return _normalise(provider, action, upstream_id, raw)
