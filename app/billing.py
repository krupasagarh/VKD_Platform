"""Billing engine: bill generation, payment recording and ledger reconciliation.

Ledger model, kept intentionally small:

* A bill is money we have charged the customer for one service period.
* A payment is money we received, independent of any bill.
* `bill_payments` allocates payments to bills oldest-first.
* Outstanding = unpaid bill balances. Credit = payment money not yet allocated.

Because allocation is recomputed from those three tables, recording a payment
before its renewal bill exists is fine: the payment sits as credit and is applied
automatically the moment the renewal bill is created.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from .config import settings
from .money import (
    add_days,
    fmt_date,
    fmt_rupees,
    gst_on_exclusive,
    gst_split,
    now_iso,
    parse_date,
    round_up_rupee,
    today,
)

BILL_PREFIX = "VK"
RECEIPT_PREFIX = "RC"

OPEN_BILL_STATUSES = ("pending", "partial")


# --------------------------------------------------------------------------- #
# Numbering
# --------------------------------------------------------------------------- #

def _next_sequence(conn: sqlite3.Connection, table: str, column: str, prefix: str) -> str:
    stamp = today().strftime("%Y%m")
    like = f"{prefix}-{stamp}-%"
    row = conn.execute(
        f"SELECT {column} AS val FROM {table} WHERE {column} LIKE ? ORDER BY id DESC LIMIT 1",
        (like,),
    ).fetchone()
    seq = 1
    if row and row["val"]:
        tail = str(row["val"]).rsplit("-", 1)[-1]
        if tail.isdigit():
            seq = int(tail) + 1
    return f"{prefix}-{stamp}-{seq:04d}"


def next_bill_no(conn: sqlite3.Connection) -> str:
    return _next_sequence(conn, "bills", "bill_no", BILL_PREFIX)


def next_receipt_no(conn: sqlite3.Connection) -> str:
    return _next_sequence(conn, "payments", "receipt_no", RECEIPT_PREFIX)


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #

def renewal_period(
    current_expiry: date | str | None,
    validity_days: int,
    as_of: date | None = None,
) -> tuple[date, date]:
    """Return (period_start, new_expiry) for a renewal.

    An unexpired connection is extended from its existing expiry so the customer
    never loses paid days. An expired connection starts fresh today.
    """
    as_of = as_of or today()
    validity = max(1, int(validity_days or 30))
    expiry = parse_date(current_expiry)
    if expiry and expiry >= as_of:
        return expiry + timedelta(days=1), add_days(expiry, validity)
    return as_of, add_days(as_of, validity - 1)


PREPAID_PROVIDERS = frozenset({"railtel", "iptv", "ott"})
RAILTEL_GST_PERCENTAGE = 18.0


def billing_type_for(provider: str) -> str:
    """Railtel, ANT IPTV and SmartPlay OTT are prepaid. Hathway is postpaid (use then bill)."""
    return "prepaid" if (provider or "").strip().lower() in PREPAID_PROVIDERS else "postpaid"


def apply_provider_billing_rules(conn: sqlite3.Connection) -> dict:
    """Force connection and plan billing types to match the provider."""
    stamp = now_iso()
    railtel_cn = conn.execute(
        "UPDATE connections SET billing_type = 'prepaid', updated_at = ? "
        "WHERE provider = 'railtel' AND billing_type != 'prepaid'",
        (stamp,),
    ).rowcount
    hathway_cn = conn.execute(
        "UPDATE connections SET billing_type = 'postpaid', updated_at = ? "
        "WHERE provider = 'hathway' AND billing_type != 'postpaid'",
        (stamp,),
    ).rowcount
    iptv_cn = conn.execute(
        "UPDATE connections SET billing_type = 'prepaid', updated_at = ? "
        "WHERE provider = 'iptv' AND billing_type != 'prepaid'",
        (stamp,),
    ).rowcount
    ott_cn = conn.execute(
        "UPDATE connections SET billing_type = 'prepaid', updated_at = ? "
        "WHERE provider = 'ott' AND billing_type != 'prepaid'",
        (stamp,),
    ).rowcount
    railtel_pkg = conn.execute(
        "UPDATE packages SET billing_type = 'prepaid' "
        "WHERE provider = 'railtel' AND billing_type != 'prepaid'"
    ).rowcount
    hathway_pkg = conn.execute(
        "UPDATE packages SET billing_type = 'postpaid' "
        "WHERE provider = 'hathway' AND billing_type != 'postpaid'"
    ).rowcount
    iptv_pkg = conn.execute(
        "UPDATE packages SET billing_type = 'prepaid' "
        "WHERE provider = 'iptv' AND billing_type != 'prepaid'"
    ).rowcount
    ott_pkg = conn.execute(
        "UPDATE packages SET billing_type = 'prepaid' "
        "WHERE provider = 'ott' AND billing_type != 'prepaid'"
    ).rowcount
    gst = ensure_railtel_exclusive_gst(conn)
    return {
        "connections_railtel": int(railtel_cn or 0),
        "connections_hathway": int(hathway_cn or 0),
        "connections_iptv": int(iptv_cn or 0),
        "connections_ott": int(ott_cn or 0),
        "packages_railtel": int(railtel_pkg or 0),
        "packages_hathway": int(hathway_pkg or 0),
        "packages_iptv": int(iptv_pkg or 0),
        "packages_ott": int(ott_pkg or 0),
        "railtel_gst": gst,
    }


def railtel_only_customer_ids(conn: sqlite3.Connection) -> list[int]:
    """Prepaid-only households (Railtel and/or IPTV, no Hathway)."""
    rows = conn.execute(
        "SELECT c.id FROM customers c "
        "WHERE EXISTS (SELECT 1 FROM connections x WHERE x.customer_id = c.id "
        "              AND x.provider IN ('railtel', 'iptv', 'ott')) "
        "AND NOT EXISTS (SELECT 1 FROM connections x WHERE x.customer_id = c.id AND x.provider = 'hathway')"
    ).fetchall()
    return [int(row["id"]) for row in rows]


def zero_railtel_only_dues(conn: sqlite3.Connection, *, actor: str | None = None) -> dict:
    """Prepaid Railtel households should not carry an outstanding balance.

    Mixed Hathway + Railtel customers keep their Hathway / Bix dues.
    """
    cleared = 0
    total_paise = 0
    for customer_id in railtel_only_customer_ids(conn):
        due = int(customer_ledger(conn, customer_id)["net_due_paise"])
        if due <= 0:
            continue
        record_payment(
            conn,
            customer_id=customer_id,
            amount_paise=due,
            mode="adjustment",
            collected_by=actor,
            notes="Railtel prepaid — outstanding cleared",
        )
        reconcile_customer(conn, customer_id)
        cleared += 1
        total_paise += due
    return {"cleared": cleared, "amount_paise": total_paise}


def set_customer_due(
    conn: sqlite3.Connection,
    customer_id: int,
    target_paise: int,
    *,
    actor: str | None = None,
    reason: str = "",
) -> dict:
    """Move net due to an exact amount. The delta is a bill or adjustment in history."""
    current = int(customer_ledger(conn, customer_id)["net_due_paise"])
    target = int(target_paise)
    delta = target - current
    note = (reason or "").strip() or f"Balance set to {fmt_rupees(target)}"
    if delta == 0:
        return {"changed": False, "from_paise": current, "to_paise": target}
    if delta > 0:
        create_bill(
            conn,
            customer_id=customer_id,
            connection_id=None,
            package_id=None,
            package_name="Balance adjustment",
            amount_paise=delta,
            period_start=None,
            period_end=None,
            source="balance_adjust",
            gst_percentage=0,
            notes=note,
        )
    else:
        record_payment(
            conn,
            customer_id=customer_id,
            amount_paise=-delta,
            mode="adjustment",
            collected_by=actor,
            notes=note,
        )
    reconcile_customer(conn, customer_id)
    return {"changed": True, "from_paise": current, "to_paise": target, "delta_paise": delta}


def connection_discount_paise(conn_row, package_row=None) -> int:
    """Permanent monthly discount, multiplied by the paid months of a term plan (x3, x6...)."""
    monthly = int(_row_get(conn_row, "discount_paise") or 0)
    if monthly <= 0:
        return 0
    from .plans import parse_term_months

    plan_name = (
        _row_get(conn_row, "package_name")
        or _row_get(package_row, "name")
        or _row_get(conn_row, "upstream_plan_name")
        or ""
    )
    return monthly * (parse_term_months(str(plan_name)) or 1)


def connection_charge_paise(conn_row: sqlite3.Row | dict, package_row: sqlite3.Row | dict | None) -> int:
    """Catalog / override amount before GST, less the connection's permanent discount."""
    override = int((conn_row["amount_paise"] if conn_row is not None else 0) or 0)
    if override > 0:
        base = override
    elif package_row is not None:
        base = int(package_row["price_paise"] or 0)
    else:
        return 0
    if base <= 0:
        return base
    return max(0, base - connection_discount_paise(conn_row, package_row))


def _row_get(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def gst_rate_for(provider: str, package_row: sqlite3.Row | dict | None = None) -> float:
    """Railtel catalog prices are exclusive; GST is 18% unless the plan says otherwise."""
    pkg_rate = _row_get(package_row, "gst_percentage")
    if pkg_rate is not None and str(pkg_rate) != "":
        try:
            pkg_rate = float(pkg_rate)
        except (TypeError, ValueError):
            pkg_rate = None
    if (provider or "").strip().lower() == "railtel":
        return pkg_rate if pkg_rate and pkg_rate > 0 else RAILTEL_GST_PERCENTAGE
    if pkg_rate is not None:
        return float(pkg_rate)
    return float(settings.gst_percentage or 0)


def quoted_charge(
    conn_row: sqlite3.Row | dict, package_row: sqlite3.Row | dict | None
) -> dict:
    """What to collect: Railtel = plan + GST; others stay GST-inclusive as stored."""
    base = connection_charge_paise(conn_row, package_row)
    provider = (_row_get(conn_row, "provider") or "").strip().lower()
    rate = gst_rate_for(provider, package_row)
    exclusive = provider == "railtel"
    if exclusive:
        gst, total = gst_on_exclusive(base, rate)
        return {
            "base_paise": base,
            "gst_paise": gst,
            "total_paise": total,
            "gst_percentage": rate,
            "exclusive": True,
        }
    if rate > 0:
        split_base, gst = gst_split(base, rate)
        return {
            "base_paise": split_base,
            "gst_paise": gst,
            "total_paise": base,
            "gst_percentage": rate,
            "exclusive": False,
        }
    return {
        "base_paise": base,
        "gst_paise": 0,
        "total_paise": base,
        "gst_percentage": 0,
        "exclusive": False,
    }


PLAN_BUNDLES = (
    ("internet", "Railtel / broadband"),
    ("hathway", "Hathway cable TV"),
    ("iptv", "ANT IPTV"),
    ("ott", "SmartPlay OTT"),
    ("internet_hathway", "Railtel + Hathway"),
    ("internet_iptv", "Railtel + ANT IPTV"),
    ("internet_ott", "Railtel + OTT"),
    ("hathway_iptv", "Hathway + ANT IPTV"),
    ("hathway_ott", "Hathway + OTT"),
    ("iptv_ott", "ANT IPTV + OTT"),
    ("internet_iptv_ott", "Railtel + IPTV + OTT"),
    ("internet_hathway_iptv", "Railtel + Hathway + IPTV"),
    ("internet_hathway_ott", "Railtel + Hathway + OTT"),
    ("all_four", "Railtel + Hathway + IPTV + OTT"),
)
BUNDLE_LABELS = dict(PLAN_BUNDLES)


def customer_custom_plan(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    """Household plan used for collection and printed bills. None if not set."""
    try:
        row = conn.execute(
            "SELECT custom_plan_name, custom_plan_amount_paise, custom_plan_validity_days, "
            "custom_plan_details, custom_plan_bundle, collect_paise FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    amount = int(row["custom_plan_amount_paise"] or 0) or int(row["collect_paise"] or 0)
    name = (row["custom_plan_name"] or "").strip()
    details = (row["custom_plan_details"] or "").strip()
    days = int(row["custom_plan_validity_days"] or 0)
    bundle = (_row_get(row, "custom_plan_bundle") or "").strip()
    if amount <= 0 and not name and not bundle:
        return None
    discount = 0
    if amount > 0:
        try:
            monthly_off = int(conn.execute(
                "SELECT COALESCE(SUM(discount_paise), 0) FROM connections "
                "WHERE customer_id = ? AND status = 'active' AND COALESCE(free_reason, '') = ''",
                (customer_id,),
            ).fetchone()[0] or 0)
        except sqlite3.OperationalError:
            monthly_off = 0
        months = max(1, round(days / 30)) if days > 0 else 1
        discount = min(amount, monthly_off * months)
    return {
        "name": name or "Custom plan",
        "amount_paise": amount - discount,
        "list_paise": amount,
        "discount_paise": discount,
        "validity_days": days,
        "details": details,
        "bundle": bundle if bundle in BUNDLE_LABELS else "",
    }


def covering_custom_plan_bill(conn: sqlite3.Connection, customer_id: int):
    """Open or still-valid custom-plan bill so a bundle is not billed twice."""
    try:
        return conn.execute(
            "SELECT * FROM bills WHERE customer_id = ? AND source = 'custom_plan' "
            "AND status IN ('pending', 'partial', 'paid') "
            "AND (period_end IS NULL OR period_end >= ?) "
            "ORDER BY id DESC LIMIT 1",
            (customer_id, fmt_date(today())),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def ensure_custom_plan_bill(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    connection_id: int | None = None,
    collect_later: bool = False,
) -> int | None:
    """Create (or reuse) the household custom-plan bill."""
    plan = customer_custom_plan(conn, customer_id)
    if plan is None or plan["amount_paise"] <= 0:
        return None
    existing = covering_custom_plan_bill(conn, customer_id)
    if existing is not None:
        return int(existing["id"])
    # Paying down money already owed must not mint another plan bill —
    # that left net due unchanged after a full collection.
    due = int(customer_ledger(conn, customer_id)["net_due_paise"])
    if due > 0:
        return None
    start = today()
    end = add_days(start, plan["validity_days"] - 1) if plan["validity_days"] > 0 else None
    return create_bill(
        conn,
        customer_id=customer_id,
        connection_id=connection_id,
        package_id=None,
        package_name=plan["name"],
        amount_paise=plan["amount_paise"],
        period_start=start,
        period_end=end,
        source="custom_plan",
        gst_percentage=0,
        notes=plan["details"] or None,
        collect_later=collect_later,
        followup_kind="renew" if collect_later else "",
        amount_exclusive=False,
    )


def customer_collect_paise(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    connections=None,
) -> int:
    """Usual amount this household pays — per-connection prices summed when no bundle plan."""
    plan = customer_custom_plan(conn, customer_id)
    if plan and plan["amount_paise"] > 0:
        return plan["amount_paise"]
    try:
        row = conn.execute(
            "SELECT collect_paise FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        try:
            saved = int(row["collect_paise"] or 0)
        except (KeyError, IndexError, TypeError):
            saved = 0
        if saved > 0:
            return saved
    return connections_collect_total(conn, customer_id, connections=connections)


def default_collect_amount_paise(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    connections=None,
) -> int:
    """Amount to prefill on Collect payment: full outstanding balance when due, else usual monthly."""
    due = int(customer_ledger(conn, customer_id)["net_due_paise"])
    if due > 0:
        return due
    return customer_collect_paise(conn, customer_id, connections=connections)


def connections_collect_total(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    connections=None,
    only_active: bool = True,
) -> int:
    """Sum each connection's collect quote (per-STB overrides included)."""
    if connections is None:
        sql = (
            "SELECT c.*, p.price_paise AS package_price_paise, "
            "p.gst_percentage AS package_gst "
            "FROM connections c LEFT JOIN packages p ON p.id = c.package_id "
            "WHERE c.customer_id = ?"
        )
        params: list = [customer_id]
        if only_active:
            sql += " AND c.status = 'active'"
        connections = conn.execute(sql, params).fetchall()
    total = 0
    for cn in connections or []:
        if only_active and (cn["status"] or "").lower() != "active":
            continue
        pkg = {
            "price_paise": _row_get(cn, "package_price_paise"),
            "gst_percentage": _row_get(cn, "package_gst") or 0,
        }
        quote = quoted_charge(cn, pkg)
        total += int(quote["total_paise"] or 0)
    return total


def connection_collect_lines(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    connections=None,
) -> list[dict]:
    """Per-connection collect amounts for display (STB 320 + STB 300 = 620)."""
    if connections is None:
        connections = conn.execute(
            "SELECT c.*, p.name AS package_name, p.price_paise AS package_price_paise, "
            "p.gst_percentage AS package_gst "
            "FROM connections c LEFT JOIN packages p ON p.id = c.package_id "
            "WHERE c.customer_id = ? AND c.status = 'active' "
            "ORDER BY c.provider, c.upstream_id",
            (customer_id,),
        ).fetchall()
    lines: list[dict] = []
    for cn in connections or []:
        pkg = {
            "price_paise": _row_get(cn, "package_price_paise"),
            "gst_percentage": _row_get(cn, "package_gst") or 0,
        }
        quote = quoted_charge(cn, pkg)
        amount = int(quote["total_paise"] or 0)
        if amount <= 0:
            continue
        provider = (_row_get(cn, "provider") or "").strip().lower()
        plan = (_row_get(cn, "package_name") or _row_get(cn, "upstream_plan_name") or "").strip()
        lines.append({
            "connection_id": int(cn["id"]),
            "provider": provider,
            "upstream_id": _row_get(cn, "upstream_id") or "",
            "plan": plan,
            "amount_paise": amount,
            "custom": int(_row_get(cn, "amount_paise") or 0) > 0,
        })
    return lines


def set_customer_collect_paise(conn: sqlite3.Connection, customer_id: int, amount_paise: int) -> None:
    amount = max(0, int(amount_paise or 0))
    conn.execute(
        "UPDATE customers SET collect_paise = ?, custom_plan_amount_paise = ?, "
        "updated_at = ? WHERE id = ?",
        (amount, amount, now_iso(), customer_id),
    )


def set_customer_custom_plan(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    name: str,
    amount_paise: int,
    validity_days: int,
    details: str,
    bundle: str = "",
) -> None:
    amount = max(0, int(amount_paise or 0))
    days = max(0, int(validity_days or 0))
    kind = (bundle or "").strip()
    if kind not in BUNDLE_LABELS:
        kind = ""
    conn.execute(
        "UPDATE customers SET custom_plan_name = ?, custom_plan_amount_paise = ?, "
        "custom_plan_validity_days = ?, custom_plan_details = ?, custom_plan_bundle = ?, "
        "collect_paise = ?, updated_at = ? WHERE id = ?",
        (
            (name or "").strip(),
            amount,
            days,
            (details or "").strip(),
            kind,
            amount,
            now_iso(),
            customer_id,
        ),
    )


def _bundle_from_provider_flags(
    has_railtel: bool,
    has_hathway: bool,
    has_iptv: bool,
    has_ott: bool,
) -> str:
    key = (has_railtel, has_hathway, has_iptv, has_ott)
    return {
        (True, False, False, False): "internet",
        (False, True, False, False): "hathway",
        (False, False, True, False): "iptv",
        (False, False, False, True): "ott",
        (True, True, False, False): "internet_hathway",
        (True, False, True, False): "internet_iptv",
        (True, False, False, True): "internet_ott",
        (False, True, True, False): "hathway_iptv",
        (False, True, False, True): "hathway_ott",
        (False, False, True, True): "iptv_ott",
        (True, False, True, True): "internet_iptv_ott",
        (True, True, True, False): "internet_hathway_iptv",
        (True, True, False, True): "internet_hathway_ott",
        (True, True, True, True): "all_four",
    }.get(key, "internet")


def infer_plan_bundle(connections, plan: dict | None) -> str:
    """Pick the household service bundle from a saved choice or live connections."""
    stored = ((plan or {}).get("bundle") or "").strip()
    if stored in BUNDLE_LABELS:
        return stored
    text = f"{(plan or {}).get('name') or ''} {(plan or {}).get('details') or ''}".lower()
    providers = set()
    for row in connections or []:
        providers.add((_row_get(row, "provider") or "").strip().lower())
    has_railtel = "railtel" in providers or "railtel" in text or "broadband" in text
    has_hathway = "hathway" in providers or "hathway" in text or "cable" in text or "stb" in text
    has_iptv = "iptv" in providers or "iptv" in text or "ant" in text
    has_ott = "ott" in providers or "smartplay" in text or " ott" in f" {text}"
    if not providers and not text.strip():
        return "internet"
    return _bundle_from_provider_flags(has_railtel, has_hathway, has_iptv, has_ott)


_TERM_PLAN_MARKERS = (" x3", " x6", " x10", " x12")


def is_railtel_term_plan_name(name: str) -> bool:
    text = f" {(name or '').strip().lower()}"
    return any(
        text.endswith(tag) or f"{tag} " in text or f"{tag}-" in text
        for tag in _TERM_PLAN_MARKERS
    )


def soonest_paid_through(connections) -> dict:
    """Earliest connection paid-through date — same rule as the connection card.

    Railtel x3/x6/x10 use Subscriber Details xpath (effective_expiry /
    subscription_expiry). Monthly packs use portal expiry_date. Household
    custom-plan payment math is not used here. Term plans whose xpath date
    has not been read yet fall back to the portal renewal date.
    """
    best = None
    best_id = None
    for row in connections or []:
        sub = parse_date(_row_get(row, "subscription_expiry") or "")
        if sub is not None and sub.year < 2000:
            sub = None
        parsed = (
            parse_date(_row_get(row, "effective_expiry") or "")
            or sub
            or parse_date(_row_get(row, "expiry_date") or "")
        )
        if parsed is None:
            continue
        if best is None or parsed < best:
            best = parsed
            best_id = int(_row_get(row, "id") or 0) or None
    return {
        "expiry": fmt_date(best) if best else "",
        "connection_id": best_id,
        "days_left": (best - today()).days if best else None,
    }


def customer_cover(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    connections=None,
    custom_plan: dict | None = None,
) -> dict:
    """Household paid-through date and which services that payment covers.

    Portal connection expiry can lag behind a bundle collection. This uses last
    cash payment + the customer-plan term when one is set.
    """
    plan = custom_plan if custom_plan is not None else customer_custom_plan(conn, customer_id)
    if connections is None:
        connections = conn.execute(
            "SELECT provider, expiry_date, subscription_expiry FROM connections WHERE customer_id = ?",
            (customer_id,),
        ).fetchall()
    key = infer_plan_bundle(connections, plan)
    days = int((plan or {}).get("validity_days") or 0)
    pay = conn.execute(
        "SELECT paid_at FROM payments WHERE customer_id = ? "
        "AND lower(COALESCE(mode, '')) != 'adjustment' "
        "ORDER BY paid_at DESC, id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    expiry = None
    from_payment = False
    if days > 0 and pay is not None:
        paid = parse_date(pay["paid_at"])
        if paid:
            expiry = add_days(paid, days - 1)
            from_payment = True
    if expiry is None:
        dates = []
        for row in connections or []:
            parsed = parse_date(
                _row_get(row, "effective_expiry")
                or _row_get(row, "subscription_expiry")
                or _row_get(row, "expiry_date")
            )
            if parsed:
                dates.append(parsed)
        expiry = min(dates) if dates else None
    return {
        "expiry": fmt_date(expiry) if expiry else "",
        "bundle": key,
        "label": BUNDLE_LABELS[key],
        "validity_days": days,
        "from_payment": from_payment,
    }


def renewal_bill_amount(
    conn: sqlite3.Connection,
    conn_row: sqlite3.Row | dict,
    package_row: sqlite3.Row | dict | None,
) -> dict:
    """Plan + GST, or this customer's usual collect amount if one is saved."""
    quote = quoted_charge(conn_row, package_row)
    plan = customer_custom_plan(conn, int(conn_row["customer_id"]))
    if plan and plan["amount_paise"] > 0:
        return {
            "amount_paise": plan["amount_paise"],
            "gst_percentage": 0,
            "amount_exclusive": False,
            "quote": quote,
            "usual_paise": plan["amount_paise"],
            "custom_plan": plan,
        }
    return {
        "amount_paise": quote["base_paise"] if quote["exclusive"] else quote["total_paise"],
        "gst_percentage": quote["gst_percentage"],
        "amount_exclusive": quote["exclusive"],
        "quote": quote,
        "usual_paise": 0,
        "custom_plan": None,
    }


def ensure_railtel_exclusive_gst(conn: sqlite3.Connection) -> dict:
    """Catalog Railtel prices are exclusive. Mark 18% GST; unwrap any inclusive leftovers."""
    from .db import get_setting, set_setting

    converted = 0
    if get_setting(conn, "railtel_prices_exclusive", "") != "1":
        rows = conn.execute(
            "SELECT id, price_paise, gst_percentage FROM packages "
            "WHERE provider = 'railtel' AND gst_percentage > 0"
        ).fetchall()
        for row in rows:
            base, _gst = gst_split(int(row["price_paise"] or 0), float(row["gst_percentage"]))
            if base != int(row["price_paise"] or 0):
                conn.execute(
                    "UPDATE packages SET price_paise = ?, gst_percentage = ? WHERE id = ?",
                    (base, RAILTEL_GST_PERCENTAGE, row["id"]),
                )
                converted += 1
        set_setting(conn, "railtel_prices_exclusive", "1")
    marked = conn.execute(
        "UPDATE packages SET gst_percentage = ? "
        "WHERE provider = 'railtel' AND (gst_percentage IS NULL OR gst_percentage = 0)",
        (RAILTEL_GST_PERCENTAGE,),
    ).rowcount
    return {"converted_inclusive": converted, "marked_18": int(marked or 0)}


def connection_validity_days(conn_row: sqlite3.Row | dict, package_row: sqlite3.Row | dict | None) -> int:
    override = _row_get(conn_row, "validity_days")
    try:
        override = int(override or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return override
    if package_row is not None and _row_get(package_row, "validity_days"):
        return int(package_row["validity_days"])
    return 30


def bind_portal_plan(conn: sqlite3.Connection, connection_id: int, plan_name: str) -> int | None:
    """Save the plan the provider portal just reported.

    If that name exists in the catalog, the connection is attached so amount and
    term follow the live plan instead of the imported CSV row.
    """
    name = (plan_name or "").strip()
    if not name:
        return None
    if re.search(r"renewal\s+fee|per\s+day", name, re.I):
        conn.execute(
            "UPDATE connections SET upstream_plan_name = ?, updated_at = ? WHERE id = ?",
            (name, now_iso(), connection_id),
        )
        return None
    row = conn.execute(
        "SELECT provider FROM connections WHERE id = ?", (connection_id,)
    ).fetchone()
    if row is None:
        return None
    pkg = conn.execute(
        "SELECT id FROM packages WHERE provider = ? AND lower(trim(name)) = lower(trim(?)) LIMIT 1",
        (row["provider"], name),
    ).fetchone()
    pkg_id = int(pkg["id"]) if pkg else None
    conn.execute(
        "UPDATE connections SET upstream_plan_name = ?, package_id = COALESCE(?, package_id), "
        "updated_at = ? WHERE id = ?",
        (name, pkg_id, now_iso(), connection_id),
    )
    return pkg_id


# --------------------------------------------------------------------------- #
# Bills
# --------------------------------------------------------------------------- #

def create_bill(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    connection_id: int | None,
    package_id: int | None,
    package_name: str | None,
    amount_paise: int,
    period_start: date | str | None,
    period_end: date | str | None,
    source: str = "manual",
    gst_percentage: float | None = None,
    job_id: int | None = None,
    notes: str | None = None,
    due_date: date | str | None = None,
    collect_later: bool = False,
    followup_kind: str = "",
    amount_exclusive: bool = False,
) -> int:
    """Insert a bill and return its id.

    Amount is GST-inclusive unless amount_exclusive is set (Railtel catalog).
    The total is rounded up to a whole rupee (₹588.82 bills as ₹589).
    """
    rate = settings.gst_percentage if gst_percentage is None else gst_percentage
    if amount_exclusive:
        _, total = gst_on_exclusive(int(amount_paise), rate)
        total = round_up_rupee(total)
        base = int(amount_paise)
        gst = total - base
    else:
        total = round_up_rupee(int(amount_paise))
        base, gst = gst_split(total, rate)
    stamp = now_iso()
    due = due_date or add_days(today(), settings.due_days)
    kind = (followup_kind or "").strip()
    if collect_later and kind not in ("manual", "renew"):
        kind = "renew"
    cursor = conn.execute(
        "INSERT INTO bills(bill_no, customer_id, connection_id, package_id, package_name, "
        "period_start, period_end, amount_paise, gst_paise, total_paise, paid_paise, "
        "due_date, status, source, job_id, notes, collect_later, followup_kind, "
        "created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
        (
            next_bill_no(conn),
            customer_id,
            connection_id,
            package_id,
            package_name,
            fmt_date(period_start),
            fmt_date(period_end),
            base,
            gst,
            total,
            fmt_date(due),
            source,
            job_id,
            notes,
            1 if collect_later else 0,
            kind,
            stamp,
            stamp,
        ),
    )
    return int(cursor.lastrowid)


def add_manual_followup(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    connection_id: int | None = None,
    amount_paise: int = 0,
    notes: str = "",
) -> dict:
    """Put a customer on the payment follow-up list as a manual chase.

    Existing unpaid bills are flagged (no extra charge). A new bill is created
    only when nothing is outstanding and an amount was given.
    """
    customer = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer is None:
        raise ValueError("Customer not found.")
    if connection_id:
        link = conn.execute(
            "SELECT id FROM connections WHERE id = ? AND customer_id = ?",
            (connection_id, customer_id),
        ).fetchone()
        if link is None:
            raise ValueError("That connection is not on this customer.")

    already = conn.execute(
        "SELECT COUNT(*) AS n FROM bills WHERE customer_id = ? AND collect_later = 1 "
        "AND followup_kind = 'manual' AND status IN ('pending', 'partial')",
        (customer_id,),
    ).fetchone()["n"]

    unmarked = conn.execute(
        "SELECT id, notes FROM bills WHERE customer_id = ? "
        "AND status IN ('pending', 'partial') AND collect_later = 0 "
        "AND (? IS NULL OR connection_id = ?) ORDER BY id",
        (customer_id, connection_id, connection_id),
    ).fetchall()

    stamp = now_iso()
    extra = (notes or "").strip()
    if unmarked:
        for row in unmarked:
            prior = (row["notes"] or "").strip()
            merged = extra if not prior else (prior if not extra else f"{prior} · {extra}")
            conn.execute(
                "UPDATE bills SET collect_later = 1, followup_kind = 'manual', "
                "notes = ?, updated_at = ? WHERE id = ?",
                (merged, stamp, row["id"]),
            )
        return {"created": False, "flagged": len(unmarked), "already": int(already)}

    if already:
        raise ValueError("This customer is already on manual follow-up.")
    renew_open = conn.execute(
        "SELECT COUNT(*) AS n FROM bills WHERE customer_id = ? AND collect_later = 1 "
        "AND followup_kind != 'manual' AND status IN ('pending', 'partial')",
        (customer_id,),
    ).fetchone()["n"]
    if renew_open:
        raise ValueError(
            "They are already on follow-up as Renewed, not paid. "
            "Collect from that list, or take them off it first."
        )
    if int(amount_paise or 0) <= 0:
        raise ValueError("Enter an amount — they have no outstanding bill to flag.")

    create_bill(
        conn,
        customer_id=customer_id,
        connection_id=connection_id,
        package_id=None,
        package_name="Manual follow-up",
        amount_paise=int(amount_paise),
        period_start=today(),
        period_end=None,
        source="manual_followup",
        notes=extra or "Manual payment follow-up",
        collect_later=True,
        followup_kind="manual",
    )
    return {"created": True, "flagged": 0, "already": int(already)}


def drop_followup(conn: sqlite3.Connection, bill_id: int) -> bool:
    """Take a manual follow-up off the list without cancelling the bill."""
    row = conn.execute(
        "SELECT id, followup_kind FROM bills WHERE id = ? AND collect_later = 1",
        (bill_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE bills SET collect_later = 0, followup_kind = '', updated_at = ? WHERE id = ?",
        (now_iso(), bill_id),
    )
    return True


def cancel_bill(conn: sqlite3.Connection, bill_id: int) -> None:
    conn.execute("DELETE FROM bill_payments WHERE bill_id = ?", (bill_id,))
    conn.execute(
        "UPDATE bills SET status = 'cancelled', paid_paise = 0, updated_at = ? WHERE id = ?",
        (now_iso(), bill_id),
    )


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #

def record_payment(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    amount_paise: int,
    connection_id: int | None = None,
    mode: str = "cash",
    reference: str | None = None,
    collected_by: str | None = None,
    collected_agent_id: int | None = None,
    paid_at: str | None = None,
    notes: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO payments(receipt_no, customer_id, connection_id, amount_paise, mode, "
        "reference, collected_by, collected_agent_id, paid_at, notes, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            next_receipt_no(conn),
            customer_id,
            connection_id,
            int(amount_paise),
            mode,
            reference,
            collected_by or settings.operator,
            collected_agent_id,
            paid_at or now_iso(),
            notes,
            now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def delete_payment(conn: sqlite3.Connection, payment_id: int) -> None:
    conn.execute("DELETE FROM bill_payments WHERE payment_id = ?", (payment_id,))
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def reconcile_customer(conn: sqlite3.Connection, customer_id: int) -> None:
    """Re-apply every payment to that customer's open bills, oldest bill first."""
    conn.execute(
        "DELETE FROM bill_payments WHERE payment_id IN "
        "(SELECT id FROM payments WHERE customer_id = ?)",
        (customer_id,),
    )
    conn.execute(
        "UPDATE bills SET paid_paise = 0, status = CASE WHEN status = 'cancelled' "
        "THEN 'cancelled' ELSE 'pending' END WHERE customer_id = ?",
        (customer_id,),
    )

    payments = conn.execute(
        "SELECT id, amount_paise, connection_id, mode, paid_at FROM payments "
        "WHERE customer_id = ? ORDER BY paid_at, id",
        (customer_id,),
    ).fetchall()
    bills = conn.execute(
        "SELECT id, total_paise, connection_id, collect_later, created_at FROM bills "
        "WHERE customer_id = ? AND status != 'cancelled' "
        "ORDER BY COALESCE(period_start, created_at), id",
        (customer_id,),
    ).fetchall()

    remaining = [[p["id"], int(p["amount_paise"])] for p in payments]
    applied_by_bill = {int(b["id"]): 0 for b in bills}
    stamp = now_iso()

    def _apply(bill, entry) -> None:
        need = int(bill["total_paise"]) - applied_by_bill[int(bill["id"])]
        take = min(need, entry[1])
        if take <= 0:
            return
        conn.execute(
            "INSERT INTO bill_payments(bill_id, payment_id, amount_paise, created_at) "
            "VALUES(?, ?, ?, ?)",
            (bill["id"], entry[0], take, stamp),
        )
        entry[1] -= take
        applied_by_bill[int(bill["id"])] += take

    # Cash taken against a connection after a collect-later renew settles that
    # renewal first; otherwise it stays on Unpaid renewals and gets collected twice.
    for pay, entry in zip(payments, remaining):
        if not pay["connection_id"] or (pay["mode"] or "").lower() == "adjustment":
            continue
        for bill in bills:
            if entry[1] <= 0:
                break
            if (
                bill["collect_later"]
                and bill["connection_id"] == pay["connection_id"]
                and (bill["created_at"] or "")[:10] <= (pay["paid_at"] or "")[:10]
            ):
                _apply(bill, entry)

    for bill in bills:
        for entry in remaining:
            if applied_by_bill[int(bill["id"])] >= int(bill["total_paise"]):
                break
            if entry[1] > 0:
                _apply(bill, entry)
        applied = applied_by_bill[int(bill["id"])]

        total = int(bill["total_paise"])
        if total <= 0:
            status = "paid"
        else:
            status = "paid" if applied >= total else ("partial" if applied > 0 else "pending")
        conn.execute(
            "UPDATE bills SET paid_paise = ?, status = ?, updated_at = ? WHERE id = ?",
            (applied, status, stamp, bill["id"]),
        )


def customer_ledger(conn: sqlite3.Connection, customer_id: int) -> dict:
    """Outstanding / credit / net due for one customer, all in paise."""
    billed = conn.execute(
        "SELECT COALESCE(SUM(total_paise), 0) AS total, COALESCE(SUM(paid_paise), 0) AS paid "
        "FROM bills WHERE customer_id = ? AND status != 'cancelled'",
        (customer_id,),
    ).fetchone()
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM payments WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    collected = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM payments "
        "WHERE customer_id = ? AND lower(COALESCE(mode, '')) != 'adjustment'",
        (customer_id,),
    ).fetchone()

    total_billed = int(billed["total"])
    allocated = int(billed["paid"])
    total_paid = int(paid["total"])
    outstanding = max(0, total_billed - allocated)
    credit = max(0, total_paid - allocated)
    return {
        "billed_paise": total_billed,
        "collected_paise": int(collected["total"]),
        "outstanding_paise": outstanding,
        "credit_paise": credit,
        "net_due_paise": outstanding - credit,
    }


# --------------------------------------------------------------------------- #
# Renewal billing (called by the upstream worker after a successful renew)
# --------------------------------------------------------------------------- #

FREE_REASONS: dict[str, str] = {
    "social": "Social cause",
    "node": "Node point",
    "other": "Other",
}


def free_reason_of(conn_row) -> str:
    reason = str(_row_get(conn_row, "free_reason") or "").strip().lower()
    return reason if reason in FREE_REASONS else ""


def _free_renewal_bill(
    conn: sqlite3.Connection,
    conn_row,
    package_name: str,
    *,
    period_start: date | None,
    period_end: date | None,
    source: str,
    job_id: int | None,
) -> int:
    """Zero bill so the period shows in the statement but nothing is ever due."""
    label = FREE_REASONS[free_reason_of(conn_row)]
    note = str(_row_get(conn_row, "free_note") or "").strip()
    bill_id = create_bill(
        conn,
        customer_id=int(conn_row["customer_id"]),
        connection_id=int(conn_row["id"]),
        package_id=None,
        package_name=package_name,
        amount_paise=0,
        period_start=period_start,
        period_end=period_end,
        source=source,
        gst_percentage=0,
        job_id=job_id,
        notes=f"Free STB — {label}" + (f" · {note}" if note else ""),
    )
    conn.execute("UPDATE bills SET status = 'paid' WHERE id = ?", (bill_id,))
    return bill_id


def set_connection_free(
    conn: sqlite3.Connection, connection_id: int, reason: str, note: str = ""
) -> int:
    """Mark or clear a free STB. Marking cancels its open bills; returns how many."""
    reason = (reason or "").strip().lower()
    if reason not in FREE_REASONS:
        reason = ""
    conn.execute(
        "UPDATE connections SET free_reason = ?, free_note = ?, updated_at = ? WHERE id = ?",
        (reason, (note or "").strip() if reason else "", now_iso(), connection_id),
    )
    if not reason:
        return 0
    conn.execute(
        "UPDATE connections SET owner_reason = '', owner_note = '' WHERE id = ?", (connection_id,)
    )
    open_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM bills WHERE connection_id = ? AND status IN ('pending', 'partial') "
            "AND paid_paise = 0",
            (connection_id,),
        )
    ]
    for bill_id in open_ids:
        cancel_bill(conn, bill_id)
    return len(open_ids)


# Owner-collected boxes are billed normally, but the owner collects — agents never see them.
OWNER_REASONS: dict[str, str] = {
    "friend": "Owner's friend",
    "combo": "Combo with internet",
}


def owner_reason_of(conn_row) -> str:
    reason = str(_row_get(conn_row, "owner_reason") or "").strip().lower()
    return reason if reason in OWNER_REASONS else ""


def set_connection_owner(
    conn: sqlite3.Connection, connection_id: int, reason: str, note: str = ""
) -> None:
    reason = (reason or "").strip().lower()
    if reason not in OWNER_REASONS:
        reason = ""
    conn.execute(
        "UPDATE connections SET owner_reason = ?, owner_note = ?, updated_at = ? WHERE id = ?",
        (reason, (note or "").strip() if reason else "", now_iso(), connection_id),
    )
    if reason:
        conn.execute(
            "UPDATE connections SET free_reason = '', free_note = '' WHERE id = ?", (connection_id,)
        )


def bill_for_renewal(
    conn: sqlite3.Connection,
    conn_row: sqlite3.Row,
    package_row: sqlite3.Row | None,
    *,
    provider_expiry: date | None,
    job_id: int | None = None,
    collect_later: bool = False,
) -> tuple[int, date, date]:
    """Create the renewal bill and return (bill_id, period_start, new_expiry).

    The provider's own expiry date wins when we could read it; otherwise we fall
    back to the plan's validity so the ledger still advances.

    Unpaid portal renews (collect_later or no cash on the job) go on Payment
    follow-up so they can be chased.
    """
    billed = renewal_bill_amount(conn, conn_row, package_row)
    custom = billed.get("custom_plan")
    validity = connection_validity_days(conn_row, package_row)
    if custom and custom.get("validity_days"):
        validity = int(custom["validity_days"])
    current_expiry = conn_row["expiry_date"]
    if validity > 30:
        current_expiry = conn_row["subscription_expiry"] or current_expiry
    period_start, computed_expiry = renewal_period(current_expiry, validity)
    new_expiry = provider_expiry or computed_expiry
    if new_expiry < period_start:
        period_start = today()

    package_name = (
        (package_row["name"] if package_row is not None else None)
        or conn_row["upstream_plan_name"]
        or ""
    )
    if free_reason_of(conn_row):
        bill_id = _free_renewal_bill(
            conn, conn_row, package_name,
            period_start=period_start, period_end=new_expiry, source="renewal", job_id=job_id,
        )
        return bill_id, period_start, new_expiry

    follow = bool(collect_later) and int(billed["amount_paise"] or 0) > 0
    notes = "Renewed on portal — payment not collected" if follow else None
    source = "renewal"
    if custom:
        package_name = custom["name"]
        source = "custom_plan"
        extra = custom["details"] or f"Household plan (provider catalog ₹{fmt_rupees(billed['quote']['total_paise'])})"
        notes = f"{notes} · {extra}" if notes else extra
        existing = covering_custom_plan_bill(conn, int(conn_row["customer_id"]))
        if existing is not None:
            if follow and existing["status"] in ("pending", "partial"):
                conn.execute(
                    "UPDATE bills SET collect_later = 1, "
                    "followup_kind = CASE WHEN followup_kind = 'manual' "
                    "THEN followup_kind ELSE 'renew' END, "
                    "updated_at = ? WHERE id = ?",
                    (now_iso(), int(existing["id"])),
                )
            return int(existing["id"]), period_start, new_expiry
    elif billed["usual_paise"]:
        plan_total = billed["quote"]["total_paise"]
        extra = f"Custom collect (plan + GST ₹{fmt_rupees(plan_total)})"
        notes = f"{notes} · {extra}" if notes else extra
    bill_id = create_bill(
        conn,
        customer_id=int(conn_row["customer_id"]),
        connection_id=int(conn_row["id"]),
        package_id=None if custom else (int(package_row["id"]) if package_row is not None else None),
        package_name=package_name,
        amount_paise=billed["amount_paise"],
        period_start=period_start,
        period_end=new_expiry,
        source=source,
        gst_percentage=billed["gst_percentage"],
        job_id=job_id,
        notes=notes,
        collect_later=follow,
        followup_kind="renew" if follow else "",
        amount_exclusive=billed["amount_exclusive"],
    )
    return bill_id, period_start, new_expiry


def bill_for_collect_later_enable(
    conn: sqlite3.Connection,
    conn_row: sqlite3.Row,
    package_row: sqlite3.Row | None,
    *,
    job_id: int | None = None,
) -> int:
    """Bill the plan amount after enabling a box the customer will pay for later."""
    amount = connection_charge_paise(conn_row, package_row)
    package_name = (
        (package_row["name"] if package_row is not None else None)
        or conn_row["upstream_plan_name"]
        or ""
    )
    if free_reason_of(conn_row):
        return _free_renewal_bill(
            conn, conn_row, package_name,
            period_start=today(), period_end=None, source="collect_later", job_id=job_id,
        )
    provider = (conn_row["provider"] or "").strip().lower()
    return create_bill(
        conn,
        customer_id=int(conn_row["customer_id"]),
        connection_id=int(conn_row["id"]),
        package_id=int(package_row["id"]) if package_row is not None else None,
        package_name=package_name,
        amount_paise=amount or 0,
        period_start=today(),
        period_end=None,
        source="collect_later",
        gst_percentage=gst_rate_for(provider, package_row),
        job_id=job_id,
        notes="Enabled, collect later — promised cash or online",
        collect_later=True,
        followup_kind="renew",
        amount_exclusive=provider == "railtel",
    )


# --------------------------------------------------------------------------- #
# Bill checker (postpaid / lapsed connections)
# --------------------------------------------------------------------------- #

def run_bill_checker(conn: sqlite3.Connection, *, as_of: date | None = None) -> dict:
    """Generate bills for active connections whose paid period has ended.

    Skips connections that are inactive, already have an open bill, have no plan
    price, or have no known expiry — and reports the count per reason, so nothing
    is silently left unbilled.
    """
    as_of = as_of or today()
    rows = conn.execute(
        "SELECT c.*, p.id AS pkg_id, p.name AS pkg_name, p.price_paise AS pkg_price, "
        "       p.validity_days AS pkg_validity, p.gst_percentage AS pkg_gst "
        "FROM connections c LEFT JOIN packages p ON p.id = c.package_id "
        "ORDER BY c.id"
    ).fetchall()

    generated: list[dict] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for row in rows:
        if (row["status"] or "").lower() != "active":
            skip("connection_not_active")
            continue
        if (row["billing_type"] or "").lower() == "prepaid" or (row["provider"] or "").lower() == "railtel":
            skip("prepaid_no_cycle_bill")
            continue
        if free_reason_of(row):
            skip("free_stb")
            continue

        expiry = parse_date(row["expiry_date"])
        if expiry is None:
            # Guessing a period for a connection we have never read from the provider
            # would invent a due date. Run a status check on it first.
            skip("expiry_unknown_needs_status_sync")
            continue
        if expiry >= as_of:
            skip("still_within_paid_period")
            continue

        open_bill = conn.execute(
            "SELECT id FROM bills WHERE connection_id = ? AND status IN (?, ?) LIMIT 1",
            (row["id"], *OPEN_BILL_STATUSES),
        ).fetchone()
        if open_bill:
            skip("open_bill_already_exists")
            continue

        amount = int(row["amount_paise"] or 0) or int(row["pkg_price"] or 0)
        if amount <= 0:
            skip("no_plan_price")
            continue
        amount = max(0, amount - connection_discount_paise(row, {"name": row["pkg_name"]}))
        if amount <= 0:
            skip("fully_discounted")
            continue

        validity = int(row["pkg_validity"] or 30)
        period_start, period_end = renewal_period(row["expiry_date"], validity, as_of)
        bill_id = create_bill(
            conn,
            customer_id=int(row["customer_id"]),
            connection_id=int(row["id"]),
            package_id=int(row["pkg_id"]) if row["pkg_id"] else None,
            package_name=row["pkg_name"] or row["upstream_plan_name"] or "",
            amount_paise=amount,
            period_start=period_start,
            period_end=period_end,
            source="cycle",
            gst_percentage=float(row["pkg_gst"]) if row["pkg_gst"] is not None else None,
        )
        generated.append(
            {
                "bill_id": bill_id,
                "connection_id": int(row["id"]),
                "customer_id": int(row["customer_id"]),
                "amount_paise": amount,
            }
        )

    for customer_id in {g["customer_id"] for g in generated}:
        reconcile_customer(conn, customer_id)

    return {
        "as_of": fmt_date(as_of),
        "generated": len(generated),
        "generated_amount_paise": sum(g["amount_paise"] for g in generated),
        "skipped": skipped,
        "bills": generated,
    }
