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
import time

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from .config import settings
from .money import now_iso

COOKIE_NAME = "vkp_session"
MAX_AGE_SECONDS = 60 * 60 * 24 * 14

PUBLIC_PATH_PREFIXES = ("/login", "/static", "/healthz", "/favicon.ico")

# Finest-grained flags. An admin ignores this list and can do everything.
PERMISSIONS = (
    ("customers_view", "View customers and statements"),
    ("customers_edit", "Add, edit or delete customers and connections"),
    ("payments", "Collect or delete payments and print receipts"),
    ("bills", "See bills and run the bill checker"),
    ("portal_actions", "Queue Railtel / Hathway portal actions and jobs"),
    ("providers", "Providers, wallet, online list, bulk status"),
    ("packages", "Add or edit plans"),
    ("activity", "See the activity log"),
    ("complaints", "Log complaints, assign agents and mark them fixed"),
    ("bix_sync", "Upload a Bix file, update dues, and import Bix history"),
    ("agents", "Create agents and change access"),
)

PERM_KEYS = tuple(key for key, _label in PERMISSIONS)

ROLE_DEFAULTS = {
    "admin": list(PERM_KEYS),
    "collector": ["customers_view", "payments", "complaints"],
}

# First matching prefix wins. More specific paths must come first.
PATH_PERMISSIONS = (
    ("/settings/agents", "agents"),
    ("/settings/bix-history", "bix_sync"),
    ("/settings/bix", "bix_sync"),
    ("/providers", "providers"),
    ("/sync", "providers"),
    ("/jobs", "portal_actions"),
    ("/packages", "packages"),
    ("/activity", "activity"),
    ("/complaints", "complaints"),
    ("/bills", "bills"),
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
        "active": bool(row["active"]),
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
    return any(path == prefix or path.startswith(prefix + "/") or path.startswith(prefix)
               for prefix in PUBLIC_PATH_PREFIXES)


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
