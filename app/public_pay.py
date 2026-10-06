"""Customer self-pay portal — phone lookup, UPI intent, staff confirmation."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from urllib.parse import quote

from . import billing
from .config import settings
from .db import get_setting, set_setting
from .money import from_paise, now_iso
from .upstream.providers import normalise_upstream_id, provider_label

_DIGITS = re.compile(r"\D+")

_LOOKUP_HITS: dict[str, list[float]] = {}
_LOOKUP_MAX = 10
_LOOKUP_WINDOW_SEC = 300

INTENT_TTL_HOURS = 24


def normalize_phone(phone: str) -> str:
    """Last 10 Indian mobile digits. Ignores +91, 0, spaces, and punctuation."""
    digits = _DIGITS.sub("", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[-10:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) == 10 else ""


def _phone_last10(stored: str | None) -> str:
    digits = _DIGITS.sub("", stored or "")
    return digits[-10:] if len(digits) >= 10 else ""


def check_lookup_rate_limit(client_ip: str) -> bool:
    """Return True if lookup is allowed."""
    ip = (client_ip or "unknown").strip()[:64]
    now = time.time()
    hits = [t for t in _LOOKUP_HITS.get(ip, []) if now - t < _LOOKUP_WINDOW_SEC]
    if len(hits) >= _LOOKUP_MAX:
        _LOOKUP_HITS[ip] = hits
        return False
    hits.append(now)
    _LOOKUP_HITS[ip] = hits
    return True


def effective_upi_vpa(conn: sqlite3.Connection) -> str:
    db_val = (get_setting(conn, "public_pay_upi_vpa", "") or "").strip()
    if db_val:
        return db_val
    return (settings.upi_vpa or "").strip()


def effective_payee_name(conn: sqlite3.Connection) -> str:
    db_val = (get_setting(conn, "public_pay_upi_payee", "") or "").strip()
    if db_val:
        return db_val
    return (settings.upi_payee_name or settings.operator or "VK Digital").strip()


def public_pay_enabled(conn: sqlite3.Connection) -> bool:
    if not settings.public_pay_enabled:
        return False
    flag = (get_setting(conn, "public_pay_enabled", "") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    return True


def save_public_pay_settings(
    conn: sqlite3.Connection,
    *,
    upi_vpa: str,
    upi_payee: str,
    enabled: bool,
) -> None:
    set_setting(conn, "public_pay_upi_vpa", (upi_vpa or "").strip())
    set_setting(conn, "public_pay_upi_payee", (upi_payee or "").strip())
    set_setting(conn, "public_pay_enabled", "1" if enabled else "0")


def find_customers_by_phone(conn: sqlite3.Connection, phone: str) -> list[sqlite3.Row]:
    last10 = normalize_phone(phone)
    if not last10:
        return []
    pattern = f"%{last10}"
    rows = conn.execute(
        "SELECT id, name, phone, alt_phone, status FROM customers "
        "WHERE phone LIKE ? OR alt_phone LIKE ? ORDER BY name, id",
        (pattern, pattern),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in rows:
        if _phone_last10(row["phone"]) == last10 or _phone_last10(row["alt_phone"]) == last10:
            out.append(row)
    return out


def mask_name(name: str | None) -> str:
    parts = (name or "Customer").split()
    if not parts:
        return "Customer"
    if len(parts) == 1:
        word = parts[0]
        if len(word) <= 2:
            return word
        return f"{word[0]}{'•' * (len(word) - 2)}{word[-1]}"
    return f"{parts[0]} {parts[-1][0]}."


def mask_phone_last4(phone: str | None) -> str:
    d = normalize_phone(phone or "")
    return f"••••{d[-4:]}" if d else ""


def connection_display_id(provider: str | None, upstream_id: str | None, card_number: str | None) -> str:
    uid = (upstream_id or "").strip()
    card = (card_number or "").strip()
    prov = (provider or "").lower()
    if prov == "hathway":
        return card or uid or "—"
    return uid or card or "—"


def normalize_service_lookup(raw: str) -> str:
    """Customer-entered STB (N…), Railtel login, or IPTV mobile.

    Spaces (including the extra space phones insert after '.') are ignored.
    Do not force caps — Railtel logins stay as typed aside from whitespace.
    """
    text = (raw or "").strip().strip("'\"")
    text = re.sub(r"\s+", "", text)
    if not text:
        return ""
    digits = _DIGITS.sub("", text)
    if len(digits) == 10 and not text.upper().startswith("N"):
        return digits
    if text.upper().startswith("N"):
        return text.upper()
    return text


def find_connections_by_service_id(
    conn: sqlite3.Connection, raw: str
) -> list[sqlite3.Row]:
    """Match one Hathway STB, Railtel username, or similar upstream id."""
    key = normalize_service_lookup(raw)
    if len(key) < 3:
        return []
    key_upper = key.upper()
    key_lower = key.lower()
    rows = conn.execute(
        "SELECT cn.id AS connection_id, cn.customer_id, cn.provider, cn.upstream_id, "
        "cn.card_number, cn.status, c.name AS customer_name, c.phone AS customer_phone "
        "FROM connections cn "
        "JOIN customers c ON c.id = cn.customer_id "
        "WHERE cn.status != 'terminated' "
        "AND ("
        "  upper(trim(cn.upstream_id)) = ? "
        "  OR lower(trim(cn.upstream_id)) = ? "
        "  OR upper(trim(cn.card_number)) = ? "
        "  OR trim(cn.upstream_id) = ?"
        ") "
        "ORDER BY cn.provider, cn.id",
        (key_upper, key_lower, key_upper, key),
    ).fetchall()
    if rows:
        return list(rows)
    # Hathway: try normalised portal shape (N701…)
    norm = normalise_upstream_id("hathway", key)
    if norm and norm != key_upper:
        return conn.execute(
            "SELECT cn.id AS connection_id, cn.customer_id, cn.provider, cn.upstream_id, "
            "cn.card_number, cn.status, c.name AS customer_name, c.phone AS customer_phone "
            "FROM connections cn "
            "JOIN customers c ON c.id = cn.customer_id "
            "WHERE cn.status != 'terminated' AND upper(trim(cn.upstream_id)) = ?",
            (norm.upper(),),
        ).fetchall()
    norm_r = normalise_upstream_id("railtel", key)
    if norm_r and norm_r != key:
        return conn.execute(
            "SELECT cn.id AS connection_id, cn.customer_id, cn.provider, cn.upstream_id, "
            "cn.card_number, cn.status, c.name AS customer_name, c.phone AS customer_phone "
            "FROM connections cn "
            "JOIN customers c ON c.id = cn.customer_id "
            "WHERE cn.status != 'terminated' AND lower(trim(cn.upstream_id)) = lower(?)",
            (norm_r,),
        ).fetchall()
    return []


def _connection_quote_paise(cn: sqlite3.Row) -> int:
    gst = 0
    if "package_gst" in cn.keys() and cn["package_gst"] is not None:
        gst = cn["package_gst"]
    pkg = {
        "price_paise": int(cn["package_price_paise"] or 0) if "package_price_paise" in cn.keys() else 0,
        "gst_percentage": gst,
    }
    quote = billing.quoted_charge(cn, pkg)
    return int(quote["total_paise"] or 0)


def build_account_view(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    focus_connection_id: int | None = None,
) -> dict | None:
    from . import repo

    customer = repo.get_customer(conn, customer_id)
    if customer is None:
        return None
    connections = repo.customer_connections(conn, customer_id)
    ledger = billing.customer_ledger(conn, customer_id)
    collect = billing.customer_collect_paise(conn, customer_id, connections=connections)
    net_due = int(ledger["net_due_paise"])
    lines = billing.connection_collect_lines(conn, customer_id, connections=connections)
    custom = billing.customer_custom_plan(conn, customer_id)
    custom_plan_name = ""
    if custom:
        name = (custom.get("name") or "").strip()
        if name and name.lower() != "custom plan":
            custom_plan_name = name

    focus_cn = None
    if focus_connection_id:
        for cn in connections:
            if int(cn["id"]) == int(focus_connection_id):
                focus_cn = cn
                break
        if focus_cn is None:
            return None

    if focus_cn is not None:
        if net_due > 0:
            pay_paise = net_due
        else:
            conn_pay = _connection_quote_paise(focus_cn)
            pay_paise = conn_pay if conn_pay > 0 else (collect if len(connections) == 1 else conn_pay)
        renew_label = connection_display_id(
            focus_cn["provider"], focus_cn["upstream_id"], focus_cn["card_number"]
        )
    else:
        pay_paise = net_due if net_due > 0 else collect
        renew_label = ""

    conn_rows = []
    show_connections = connections
    if focus_cn is not None:
        show_connections = [focus_cn]
    for cn in show_connections:
        if (cn["status"] or "").lower() == "terminated":
            continue
        prov = (cn["provider"] or "").lower()
        conn_rows.append(
            {
                "id": int(cn["id"]),
                "provider": prov,
                "provider_label": provider_label(prov),
                "display_id": connection_display_id(prov, cn["upstream_id"], cn["card_number"]),
                "status": cn["status"] or "",
                "expiry_date": cn["expiry_date"] or "",
                "package_name": custom_plan_name
                or (cn["package_name"] or cn["upstream_plan_name"] or "").strip(),
            }
        )

    return {
        "customer_id": int(customer["id"]),
        "name": customer["name"] or "",
        "name_masked": mask_name(customer["name"]),
        "custom_plan_name": custom_plan_name,
        "phone_masked": mask_phone_last4(customer["phone"]),
        "net_due_paise": net_due,
        "collect_paise": collect,
        "pay_paise": pay_paise,
        "collect_lines": lines,
        "connections": conn_rows,
        "focus_connection_id": int(focus_connection_id) if focus_connection_id else None,
        "renew_single": focus_connection_id is not None,
        "renew_label": renew_label,
    }


def suggest_connection_id(account: dict, raw: str | None) -> int | None:
    focus = account.get("focus_connection_id")
    if focus:
        return int(focus)
    if raw and str(raw).strip().isdigit():
        cid = int(raw)
        for row in account.get("connections") or []:
            if row["id"] == cid:
                return cid
    conns = account.get("connections") or []
    if len(conns) == 1:
        return int(conns[0]["id"])
    return None


def make_reference(customer_id: int, intent_id: int) -> str:
    return f"VK{customer_id}-{intent_id:05d}"[:40]


def build_upi_uri(*, vpa: str, payee_name: str, amount_paise: int, reference: str) -> str:
    amount = f"{from_paise(amount_paise):.2f}"
    params = [
        ("pa", vpa),
        ("pn", payee_name[:50]),
        ("am", amount),
        ("cu", "INR"),
        ("tn", reference[:50]),
    ]
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"upi://pay?{query}"


def create_pay_intent(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    connection_id: int | None,
    amount_paise: int,
    ip_hint: str | None = None,
) -> sqlite3.Row:
    if amount_paise <= 0:
        raise ValueError("Nothing to pay for this account.")
    vpa = effective_upi_vpa(conn)
    if not vpa:
        raise ValueError("UPI ID is not configured yet. Ask your operator to set it in Settings.")

    token = secrets.token_urlsafe(18)
    stamp = now_iso()
    cursor = conn.execute(
        "INSERT INTO pay_intents(token, customer_id, connection_id, amount_paise, reference, "
        "status, created_at, updated_at, ip_hint) "
        "VALUES(?, ?, ?, ?, '', 'pending', ?, ?, ?)",
        (token, customer_id, connection_id, int(amount_paise), stamp, stamp, ip_hint),
    )
    intent_id = int(cursor.lastrowid)
    reference = make_reference(customer_id, intent_id)
    conn.execute(
        "UPDATE pay_intents SET reference = ?, updated_at = ? WHERE id = ?",
        (reference, now_iso(), intent_id),
    )
    return conn.execute("SELECT * FROM pay_intents WHERE id = ?", (intent_id,)).fetchone()


def get_intent_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM pay_intents WHERE token = ?", ((token or "").strip(),)).fetchone()
    if row is None:
        return None
    if intent_expired(row):
        if (row["status"] or "") in {"pending", "customer_marked"}:
            conn.execute(
                "UPDATE pay_intents SET status = 'expired', updated_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )
        return conn.execute("SELECT * FROM pay_intents WHERE id = ?", (row["id"],)).fetchone()
    return row


def intent_expired(row: sqlite3.Row) -> bool:
    if (row["status"] or "") in {"confirmed", "cancelled", "expired"}:
        return (row["status"] or "") == "expired"
    created = row["created_at"] or ""
    try:
        from .money import parse_datetime

        dt = parse_datetime(created)
        if dt is None:
            return False
        age = time.time() - dt.timestamp()
        return age > INTENT_TTL_HOURS * 3600
    except Exception:
        return False


def intent_upi_payload(conn: sqlite3.Connection, intent: sqlite3.Row) -> dict:
    vpa = effective_upi_vpa(conn)
    payee = effective_payee_name(conn)
    uri = build_upi_uri(
        vpa=vpa,
        payee_name=payee,
        amount_paise=int(intent["amount_paise"]),
        reference=intent["reference"] or "",
    )
    return {"vpa": vpa, "payee_name": payee, "upi_uri": uri}


def mark_customer_paid(conn: sqlite3.Connection, intent_id: int) -> None:
    stamp = now_iso()
    conn.execute(
        "UPDATE pay_intents SET status = 'customer_marked', customer_marked_at = ?, updated_at = ? "
        "WHERE id = ? AND status IN ('pending', 'customer_marked')",
        (stamp, stamp, intent_id),
    )


def count_open_pay_intents(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pay_intents WHERE status IN ('pending', 'customer_marked')"
    ).fetchone()
    return int(row["n"] or 0)


def customer_pay_intents(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    limit: int = 15,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT pi.*, cn.provider AS connection_provider, cn.upstream_id AS connection_upstream_id, "
        "p.receipt_no AS payment_receipt "
        "FROM pay_intents pi "
        "LEFT JOIN connections cn ON cn.id = pi.connection_id "
        "LEFT JOIN payments p ON p.id = pi.payment_id "
        "WHERE pi.customer_id = ? "
        "ORDER BY pi.id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def resolve_renew_connection_id(
    conn: sqlite3.Connection,
    customer_id: int,
    connection_id: int | None,
) -> int | None:
    if connection_id:
        row = conn.execute(
            "SELECT id FROM connections WHERE id = ? AND customer_id = ?",
            (connection_id, customer_id),
        ).fetchone()
        return int(row["id"]) if row else None
    rows = conn.execute(
        "SELECT id FROM connections WHERE customer_id = ? AND status = 'active' "
        "AND provider IN ('railtel', 'hathway', 'iptv', 'ott') "
        "ORDER BY CASE provider WHEN 'railtel' THEN 0 WHEN 'hathway' THEN 1 ELSE 2 END, id",
        (customer_id,),
    ).fetchall()
    if len(rows) == 1:
        return int(rows[0]["id"])
    return None


def confirm_pay_intent(
    conn: sqlite3.Connection,
    intent_id: int,
    *,
    agent_name: str,
    agent_id: int | None,
    queue_renew: bool = True,
    can_portal: bool = False,
) -> dict:
    """Record ledger payment for a QR/UPI intent and optionally queue renew."""
    from .upstream import jobs as job_queue

    intent = conn.execute("SELECT * FROM pay_intents WHERE id = ?", (intent_id,)).fetchone()
    if intent is None:
        raise ValueError("Payment request not found.")
    if (intent["status"] or "") not in {"pending", "customer_marked"}:
        raise ValueError("That QR payment is already closed.")

    customer_id = int(intent["customer_id"])
    conn_id = resolve_renew_connection_id(
        conn,
        customer_id,
        int(intent["connection_id"]) if intent["connection_id"] else None,
    )
    amount_paise = int(intent["amount_paise"])
    reference = (intent["reference"] or "").strip()

    billing.ensure_custom_plan_bill(conn, customer_id, connection_id=conn_id)
    payment_id = billing.record_payment(
        conn,
        customer_id=customer_id,
        connection_id=conn_id,
        amount_paise=amount_paise,
        mode="scanner",
        reference=reference,
        collected_by=agent_name,
        collected_agent_id=agent_id,
        notes="QR pay portal",
    )
    billing.reconcile_customer(conn, customer_id)
    stamp = now_iso()
    conn.execute(
        "UPDATE pay_intents SET status = 'confirmed', payment_id = ?, confirmed_at = ?, "
        "confirmed_by = ?, updated_at = ?, connection_id = COALESCE(connection_id, ?) WHERE id = ?",
        (payment_id, stamp, agent_name, stamp, conn_id, intent_id),
    )
    from .db import log_activity

    log_activity(
        conn,
        "public_pay_confirmed",
        f"QR UPI {reference} ₹{from_paise(amount_paise):.2f}",
        customer_id=customer_id,
        connection_id=conn_id,
        actor=agent_name,
        meta_json=json.dumps({"intent_id": intent_id, "payment_id": payment_id}),
    )

    receipt = conn.execute(
        "SELECT receipt_no FROM payments WHERE id = ?", (payment_id,)
    ).fetchone()["receipt_no"]

    job_id = None
    renew_note = ""
    if queue_renew and conn_id and can_portal:
        try:
            job_id = job_queue.enqueue_job(
                conn,
                connection_id=conn_id,
                action="renew",
                payment_id=payment_id,
                needs_confirmation=False,
                requested_by=agent_name,
            )
            renew_note = f" Renew job #{job_id} started."
        except job_queue.RenewNotAllowed as exc:
            renew_note = f" Payment saved; renew blocked — {exc}"
        except ValueError as exc:
            renew_note = f" Payment saved; {exc}"
    elif queue_renew and conn_id and not can_portal:
        renew_note = " Payment saved. An admin must queue renew (no portal access)."
    elif queue_renew and not conn_id:
        renew_note = " Payment saved. Pick a connection to renew."

    return {
        "payment_id": payment_id,
        "receipt_no": receipt,
        "job_id": job_id,
        "renew_note": renew_note,
        "customer_id": customer_id,
    }


def list_open_intents(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT pi.*, c.name AS customer_name, c.phone AS customer_phone, "
        "cn.provider AS connection_provider, cn.upstream_id AS connection_upstream_id "
        "FROM pay_intents pi "
        "JOIN customers c ON c.id = pi.customer_id "
        "LEFT JOIN connections cn ON cn.id = pi.connection_id "
        "WHERE pi.status IN ('pending', 'customer_marked') "
        "ORDER BY pi.customer_marked_at DESC, pi.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
