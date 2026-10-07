"""Login and access control.

The first user is an admin created from VK_PLATFORM_PASSWORD. Extra agents are
created on the Settings → Agents tab, each with their own username, password and
permissions. Portal actions and Bix sync stay off for collectors unless you tick
them — agents who only collect in the field should not be able to spend the
dealer wallet or overwrite the ledger from a Bix file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from .config import settings
from .money import now_iso

COOKIE_NAME = "vkp_session"
MAX_AGE_SECONDS = 60 * 60 * 24 * 14


def request_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    client = getattr(request, "client", None)
    return (client.host if client else "") or "unknown"

PUBLIC_PATH_PREFIXES = ("/login", "/static", "/healthz", "/favicon.ico")

# Customer self-pay (QR → phone → UPI). Staff confirm at /pay/admin/* (still logged-in).
PUBLIC_PAY_PREFIX = "/pay"

# Finest-grained flags. An admin ignores this list and can do everything.
PERMISSIONS = (
    ("customers_view", "View customers and statements"),
    ("customers_edit", "Add, edit or delete customers and connections"),
    ("payments", "Collect or delete payments and print receipts"),
    ("payment_date", "Set collection date (record a payment as yesterday)"),
    ("change_due", "Change a customer's due amount (balance correction / waiver)"),
    ("bills", "See bills and run the bill checker"),
    ("portal_actions", "Queue Railtel / Hathway portal actions and jobs"),
    ("providers", "Providers, wallet, online list, bulk status"),
    ("packages", "Add or edit plans"),
    ("activity", "See the activity log"),
    ("complaints", "Log complaints, assign agents and mark them fixed"),
    ("customer_whatsapp", "Send WhatsApp messages to customers"),
    ("bix_sync", "Upload a Bix file, update dues, and import Bix history"),
    ("inventory", "See stock and log items used in the field"),
    ("agents", "Create agents and change access"),
)

PERM_KEYS = tuple(key for key, _label in PERMISSIONS)

ROLE_DEFAULTS = {
    "admin": list(PERM_KEYS),
    "collector": ["customers_view", "payments", "complaints", "inventory"],
}

# Which ISP customers an agent may view (Railtel / Hathway connections only).
PROVIDER_SCOPES = (
    ("both", "Railtel + Hathway"),
    ("railtel", "Railtel only"),
    ("hathway", "Hathway only"),
)
PROVIDER_SCOPE_KEYS = tuple(key for key, _label in PROVIDER_SCOPES)

# First matching prefix wins. More specific paths must come first.
PATH_PERMISSIONS = (
    ("/settings/agents", "agents"),
    ("/settings/upi", "agents"),
    ("/settings/hathway-expiry", "agents"),
    ("/settings/bix-history", "bix_sync"),
    ("/settings/bix", "bix_sync"),
    ("/providers", "providers"),
    ("/sync", "providers"),
    ("/jobs", "portal_actions"),
    ("/packages", "packages"),
    ("/activity", "activity"),
    ("/complaints", "complaints"),
    ("/bills", "bills"),
    ("/inventory", "inventory"),
    ("/customers/new", "customers_edit"),
)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), salt.encode(), 180_000)
    return f"{salt}${digest.hex()}"


def password_hash_matches(stored: str, candidate: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, _, _rest = stored.partition("$")
    return hmac.compare_digest(hash_password(candidate, salt), stored)


def parse_permissions(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item in PERM_KEYS]
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        data = []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item in PERM_KEYS]


def parse_provider_scope(raw) -> str:
    val = (raw or "both").strip().lower()
    return val if val in PROVIDER_SCOPE_KEYS else "both"


def agent_provider_scope(agent: dict | None) -> str | None:
    """Return railtel/hathway when the agent is limited, else None (full access)."""
    if not agent or agent.get("role") == "admin":
        return None
    scope = parse_provider_scope(agent.get("provider_scope"))
    return None if scope == "both" else scope


def customer_in_agent_scope(agent: dict | None, customer, conn=None) -> bool:
    """True when the agent may open this customer (has a matching connection)."""
    scope = agent_provider_scope(agent)
    if not scope:
        return True
    raw = ""
    customer_id = None
    if customer is not None:
        if isinstance(customer, dict):
            raw = customer.get("providers") or ""
            customer_id = customer.get("id")
        elif hasattr(customer, "keys"):
            if "providers" in customer.keys():
                raw = customer["providers"] or ""
            if "id" in customer.keys():
                customer_id = customer["id"]
    providers = {p.strip().lower() for p in str(raw).split(",") if p.strip()}
    if not providers and conn is not None and customer_id:
        providers = {
            str(row["provider"] or "").strip().lower()
            for row in conn.execute(
                "SELECT DISTINCT provider FROM connections WHERE customer_id = ?",
                (int(customer_id),),
            )
            if (row["provider"] or "").strip()
        }
    return scope in providers


def can_use_settlements(agent: dict | None) -> bool:
    """Owner plus collectors who can open the settlements page."""
    if not agent or not agent.get("active"):
        return False
    if agent.get("role") == "admin":
        return True
    perms = agent.get("permissions") or []
    return any(
        key in perms for key in ("payments", "complaints", "agents", "customers_view")
    )


def sees_all_settlements(agent: dict | None) -> bool:
    """Only the owner sees every agent's handover. Collectors see their own."""
    return bool(agent and agent.get("role") == "admin")


def scoped_connections(agent: dict | None, connections: list) -> list:
    """Drop connections outside this agent's Railtel / Hathway access."""
    scope = agent_provider_scope(agent)
    if not scope:
        return list(connections or [])
    out = []
    for row in connections or []:
        if isinstance(row, dict):
            provider = row.get("provider") or ""
        elif hasattr(row, "keys") and "provider" in row.keys():
            provider = row["provider"] or ""
        else:
            provider = ""
        if str(provider).strip().lower() == scope:
            out.append(row)
    return out


def collector_territory_limited(agent: dict | None) -> bool:
    """Non-admin collectors are limited to assigned areas and override customers."""
    return bool(agent and agent.get("active") and agent.get("role") != "admin")


def job_requested_by(agent: dict | None) -> str:
    """Name stored on provider jobs so the queue can show who created them."""
    if not agent:
        return settings.operator or "Owner"
    return (agent.get("name") or agent.get("username") or settings.operator or "Owner").strip()


def collector_id_for(agent: dict | None) -> int | None:
    if collector_territory_limited(agent):
        return int(agent["id"])
    return None


def customer_accessible(conn, agent: dict | None, customer) -> bool:
    """Whether this login may open the customer.

    ISP scope still applies (Railtel-only / Hathway-only). Area assignment does
    not hide customers — collectors with the same providers see the same
    customer records as admin, including Railtel imports with a blank locality.
    """
    if customer is None:
        return False
    if collector_id_for(agent) and conn is not None:
        if conn.execute(
            "SELECT 1 FROM connections WHERE customer_id = ? AND COALESCE(owner_reason, '') != '' LIMIT 1",
            (int(customer["id"]),),
        ).fetchone():
            return False
    return customer_in_agent_scope(agent, customer, conn)


def scoped_provider(agent: dict | None, provider: str = "") -> str:
    """List filter: agent Railtel/Hathway scope overrides a query param."""
    scope = agent_provider_scope(agent)
    if scope:
        return scope
    return (provider or "").strip().lower()


def permissions_for_role(role: str, extra: list[str] | None = None) -> list[str]:
    if role == "admin":
        return list(PERM_KEYS)
    base = list(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS["collector"]))
    for item in extra or []:
        if item in PERM_KEYS and item not in base:
            base.append(item)
    return base


def agent_from_row(row) -> dict:
    role = (row["role"] if row else "") or "collector"
    perms = list(PERM_KEYS) if role == "admin" else parse_permissions(row["permissions"])
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "username": row["username"],
        "role": role,
        "permissions": perms,
        "phone": (row["phone"] if "phone" in row.keys() else "") or "",
        "provider_scope": parse_provider_scope(
            row["provider_scope"] if "provider_scope" in row.keys() else "both"
        ),
        "active": bool(row["active"]),
        "last_login_at": (row["last_login_at"] if "last_login_at" in row.keys() else "") or "",
        "last_seen_at": (row["last_seen_at"] if "last_seen_at" in row.keys() else "") or "",
        "field_duty_on": bool(row["field_duty_on"]) if "field_duty_on" in row.keys() else False,
        "field_duty_at": (row["field_duty_at"] if "field_duty_at" in row.keys() else "") or "",
    }


def can(agent: dict | None, permission: str) -> bool:
    if not agent or not agent.get("active"):
        return False
    if agent.get("role") == "admin":
        return True
    return permission in (agent.get("permissions") or [])


def _sign(payload: str) -> str:
    return hmac.new(settings.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(agent_id: int) -> str:
    payload = f"{int(time.time())}:{int(agent_id)}"
    return f"{payload}.{_sign(payload)}"


def read_token(token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    issued_s, sep, agent_s = payload.partition(":")
    if not sep:
        return None
    try:
        issued = int(issued_s)
        agent_id = int(agent_s)
    except ValueError:
        return None
    if (time.time() - issued) > MAX_AGE_SECONDS:
        return None
    return agent_id


def load_agent(conn, agent_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None or not row["active"]:
        return None
    return agent_from_row(row)


def find_agent_by_username(conn, username: str):
    return conn.execute(
        "SELECT * FROM agents WHERE lower(username) = lower(?)",
        ((username or "").strip(),),
    ).fetchone()


_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_MAX_TRIES = 5
_LOGIN_WINDOW_SEC = 15 * 60


def _login_fail_key(client_ip: str, username: str) -> str:
    name = (username or "").strip().lower() or "admin"
    return f"{(client_ip or 'unknown').strip()}|{name}"


def login_lock_message(client_ip: str, username: str) -> str | None:
    """Return a lockout message after 5 failed tries, else None."""
    key = _login_fail_key(client_ip, username)
    now = time.time()
    stamps = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW_SEC]
    _LOGIN_FAILS[key] = stamps
    if len(stamps) < _LOGIN_MAX_TRIES:
        return None
    wait = int(_LOGIN_WINDOW_SEC - (now - stamps[0]))
    minutes = max(1, (wait + 59) // 60)
    return (
        f"Too many failed sign-in attempts. Try again in about {minutes} minute"
        f"{'s' if minutes != 1 else ''}."
    )


def record_login_failure(client_ip: str, username: str) -> None:
    key = _login_fail_key(client_ip, username)
    now = time.time()
    stamps = [t for t in _LOGIN_FAILS.get(key, []) if now - t < _LOGIN_WINDOW_SEC]
    stamps.append(now)
    _LOGIN_FAILS[key] = stamps


def clear_login_failures(client_ip: str, username: str) -> None:
    _LOGIN_FAILS.pop(_login_fail_key(client_ip, username), None)


def password_policy_error(password: str) -> str | None:
    """Rules for new/changed agent passwords. Existing logins are not affected."""
    text = password or ""
    if len(text) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", text):
        return "Password must include a letter."
    if not re.search(r"[0-9]", text):
        return "Password must include a number."
    return None


_SEEN_TOUCH: dict[int, float] = {}
_SEEN_TOUCH_SECONDS = 90


def mark_agent_login(conn, agent_id: int) -> None:
    if not agent_id:
        return
    stamp = now_iso()
    try:
        conn.execute(
            "UPDATE agents SET last_login_at = ?, last_seen_at = ? WHERE id = ?",
            (stamp, stamp, int(agent_id)),
        )
    except Exception:
        return
    _SEEN_TOUCH[int(agent_id)] = time.time()


def touch_agent_seen(conn, agent_id: int) -> None:
    """Record that this login is using the app. Throttled so every click is not a write."""
    if not agent_id:
        return
    now = time.time()
    aid = int(agent_id)
    last = _SEEN_TOUCH.get(aid, 0)
    if now - last < _SEEN_TOUCH_SECONDS:
        return
    _SEEN_TOUCH[aid] = now
    try:
        conn.execute(
            "UPDATE agents SET last_seen_at = ? WHERE id = ?",
            (now_iso(), aid),
        )
    except Exception:
        _SEEN_TOUCH.pop(aid, None)


def authenticate(conn, username: str, password: str) -> dict | None:
    """Return the agent when username+password match, else None.

    An empty username is treated as `admin`, so the original single-password
    login still works for the owner.
    """
    username = (username or "").strip() or "admin"
    row = find_agent_by_username(conn, username)
    if row is None or not row["active"]:
        return None
    if not password_hash_matches(row["password_hash"], password):
        return None
    return agent_from_row(row)


def grant_collector_complaints(conn) -> None:
    """Give existing collectors the complaints tab added after they were created."""
    for row in conn.execute("SELECT id, permissions FROM agents WHERE role = 'collector' AND active = 1"):
        perms = parse_permissions(row["permissions"])
        if "complaints" in perms:
            continue
        perms.append("complaints")
        conn.execute("UPDATE agents SET permissions = ? WHERE id = ?", (json.dumps(perms), row["id"]))


def grant_collector_inventory(conn) -> None:
    """Give existing collectors inventory so they can log STBs / fiber used."""
    for row in conn.execute("SELECT id, permissions FROM agents WHERE role = 'collector' AND active = 1"):
        perms = parse_permissions(row["permissions"])
        if "inventory" in perms:
            continue
        perms.append("inventory")
        conn.execute("UPDATE agents SET permissions = ? WHERE id = ?", (json.dumps(perms), row["id"]))


def ensure_admin_agent(conn) -> None:
    """Create the owner login from the platform password if no admin exists yet."""
    existing = conn.execute(
        "SELECT id FROM agents WHERE role = 'admin' LIMIT 1"
    ).fetchone()
    if existing:
        return
    stamp = now_iso()
    conn.execute(
        "INSERT INTO agents(name, username, password_hash, role, permissions, active, "
        "created_at, updated_at) VALUES(?, 'admin', ?, 'admin', ?, 1, ?, ?)",
        (
            settings.operator or "Admin",
            hash_password(settings.password),
            json.dumps(list(PERM_KEYS)),
            stamp,
            stamp,
        ),
    )


def is_authenticated(request: Request) -> bool:
    return getattr(request.state, "agent", None) is not None


def is_public_path(path: str) -> bool:
    if path.startswith("/pay/admin"):
        return False
    if path == PUBLIC_PAY_PREFIX or path.startswith(PUBLIC_PAY_PREFIX + "/"):
        return True
    return any(
        path == prefix or path.startswith(prefix + "/") or path.startswith(prefix)
        for prefix in PUBLIC_PATH_PREFIXES
    )


def path_permission(path: str) -> str | None:
    # v2 routes mirror classic paths for permission checks.
    if path.startswith("/v2/"):
        path = path[3:] or "/"
    for prefix, perm in PATH_PERMISSIONS:
        if path == prefix or path.startswith(prefix + "/"):
            return perm
    return None


def set_login_cookie(response: Response, agent_id: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_token(agent_id),
        max_age=MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def redirect_to_login(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?next={target}", status_code=303)


def redirect_forbidden(message: str = "You do not have access to that.") -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse(f"/?{urlencode({'flash': message, 'level': 'err'})}", status_code=303)
