"""Read queries for the UI. Writes live in billing.py and upstream/jobs.py."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from .config import settings
from .money import add_days, fmt_date_display, today

PAGE_SIZE = 50
EXPORT_PAGE_SIZE = 100_000

# Bix due-align writes mode=adjustment to move the ledger. That is not cash collected.
NOT_ADJUSTMENT = "lower(COALESCE(mode, '')) != 'adjustment'"

# Household paid-through date: last cash payment + custom-plan term, else soonest portal expiry.
NEXT_EXPIRY_SQL = """
COALESCE(
  CASE WHEN c.custom_plan_validity_days > 0 THEN (
    SELECT date(p.paid_at, '+' || (c.custom_plan_validity_days - 1) || ' days')
    FROM payments p
    WHERE p.customer_id = c.id AND lower(COALESCE(p.mode, '')) != 'adjustment'
    ORDER BY p.paid_at DESC, p.id DESC
    LIMIT 1
  ) END,
  (SELECT MIN(cn.expiry_date) FROM connections cn
     WHERE cn.customer_id = c.id AND cn.status = 'active' AND cn.expiry_date IS NOT NULL
           AND cn.expiry_date != '')
)
"""


def _customer_expiry_sql(provider: str) -> str:
    if provider:
        return (
            f"(SELECT MIN(cn.expiry_date) FROM connections cn "
            f"WHERE cn.customer_id = c.id AND cn.provider = '{provider}' "
            f"AND cn.expiry_date IS NOT NULL AND cn.expiry_date != '')"
        )
    return f"({NEXT_EXPIRY_SQL})"


def _customer_aggregates_sql(*, provider: str = "") -> str:
    expiry = _customer_expiry_sql(provider)
    return f"""
    (SELECT COUNT(*) FROM connections cn WHERE cn.customer_id = c.id) AS connection_count,
    (SELECT COUNT(*) FROM connections cn WHERE cn.customer_id = c.id AND cn.status = 'active') AS active_count,
    (SELECT GROUP_CONCAT(DISTINCT cn.provider) FROM connections cn WHERE cn.customer_id = c.id) AS providers,
    {expiry} AS next_expiry,
    COALESCE((SELECT SUM(b.total_paise - b.paid_paise) FROM bills b
       WHERE b.customer_id = c.id AND b.status IN ('pending', 'partial')), 0) AS outstanding_paise,
    COALESCE((SELECT SUM(p.amount_paise) FROM payments p WHERE p.customer_id = c.id
              AND lower(COALESCE(p.mode, '')) != 'adjustment'), 0) AS collected_paise
"""


CUSTOMER_AGGREGATES = _customer_aggregates_sql()


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

NONE_AREA = "__none__"


def _lateness_order(
    *,
    sort: str,
    late_days: int | None,
    expiry_sql: str,
    today_str: str,
    name_col: str = "c.name COLLATE NOCASE",
) -> tuple[str, list]:
    """Order expired rows by days late; optional bucket (e.g. all 1d late first)."""
    if sort != "late":
        return name_col, []
    parts: list[str] = []
    params: list = []
    if late_days is not None and late_days > 0:
        parts.append(
            f"CASE WHEN {expiry_sql} IS NOT NULL AND {expiry_sql} != '' "
            f"AND julianday(?) - julianday({expiry_sql}) = ? THEN 0 ELSE 1 END"
        )
        params.extend([today_str, late_days])
    parts.append(
        f"CASE WHEN {expiry_sql} IS NOT NULL AND {expiry_sql} != '' "
        f"AND julianday({expiry_sql}) < julianday(?) "
        f"THEN julianday(?) - julianday({expiry_sql}) ELSE 999999 END ASC"
    )
    params.extend([today_str, today_str])
    parts.append(name_col)
    return ", ".join(parts), params


def list_customer_areas(conn: sqlite3.Connection) -> list[str]:
    """Localities shown in the customers Area column, one option per spelling group."""
    rows = conn.execute(
        "SELECT MIN(sub_area) AS sub_area FROM customers "
        "WHERE TRIM(COALESCE(sub_area, '')) != '' "
        "GROUP BY lower(trim(sub_area)) "
        "ORDER BY MIN(sub_area) COLLATE NOCASE"
    ).fetchall()
    return [row["sub_area"] for row in rows]


def search_customers(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    provider: str = "",
    status: str = "",
    area: str = "",
    view: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sort: str = "",
    late_days: int | None = None,
    export_all: bool = False,
) -> dict:
    from .upstream import PROVIDERS

    where: list[str] = []
    params: list = []
    provider = (provider or "").strip()
    if provider and provider not in PROVIDERS:
        provider = ""
    today_str = today().strftime("%Y-%m-%d")

    text = (query or "").strip()
    if text:
        like = f"%{text}%"
        where.append(
            "(c.name LIKE ? OR c.phone LIKE ? OR c.code LIKE ? OR c.sub_area LIKE ? "
            "OR EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND (cn.upstream_id LIKE ? OR cn.card_number LIKE ?)))"
        )
        params.extend([like, like, like, like, like, like])

    # Expiry tabs with a provider filter use that provider's connection expiry only —
    # not household next_expiry, so bundle customers are not listed when OTT/IPTV lapses first.
    provider_expiry_view = provider and view in ("expired", "expiring")
    if provider and not provider_expiry_view:
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id AND cn.provider = ?)"
        )
        params.append(provider)

    if status:
        where.append("c.status = ?")
        params.append(status)

    area = (area or "").strip()
    if area == NONE_AREA:
        where.append("(c.sub_area IS NULL OR TRIM(c.sub_area) = '')")
    elif area:
        where.append("lower(trim(c.sub_area)) = lower(trim(?))")
        params.append(area)

    if view == "due":
        where.append(
            "COALESCE((SELECT SUM(b.total_paise - b.paid_paise) FROM bills b "
            "WHERE b.customer_id = c.id AND b.status IN ('pending','partial')), 0) > 0"
        )
    elif view == "expiring":
        limit_date = add_days(today(), settings.expiring_soon_days).strftime("%Y-%m-%d")
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider = ? AND cn.status = 'active' "
                "AND cn.expiry_date IS NOT NULL AND cn.expiry_date != '' "
                "AND cn.expiry_date BETWEEN ? AND ?)"
            )
            params.extend([provider, today_str, limit_date])
        else:
            where.append(f"({NEXT_EXPIRY_SQL}) IS NOT NULL AND ({NEXT_EXPIRY_SQL}) <= ?")
            params.append(limit_date)
    elif view == "expired":
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider = ? AND cn.expiry_date IS NOT NULL AND cn.expiry_date != '' "
                "AND cn.expiry_date < ?)"
            )
            params.extend([provider, today_str])
        else:
            where.append(f"({NEXT_EXPIRY_SQL}) IS NOT NULL AND ({NEXT_EXPIRY_SQL}) < ?")
            params.append(today_str)
    elif view == "hathway_live":
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = 'hathway' AND cn.status = 'active' "
            "AND upper(cn.upstream_id) GLOB 'N[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]')"
        )
    elif view == "hathway":
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = 'hathway' "
            "AND upper(cn.upstream_id) GLOB 'N[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]')"
        )
    elif view == "iptv_live":
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = 'iptv' AND cn.status = 'active')"
        )
    elif view == "iptv":
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = 'iptv')"
        )
    elif view == "ott_live":
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = 'ott' AND cn.status = 'active' "
            "AND (cn.expiry_date IS NULL OR cn.expiry_date = '' OR cn.expiry_date >= ?))"
        )
        params.append(today().strftime("%Y-%m-%d"))
    elif view == "ott":
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = 'ott')"
        )
    elif view == "followup":
        where.append(
            "EXISTS (SELECT 1 FROM bills b WHERE b.customer_id = c.id "
            "AND b.collect_later = 1 AND b.status IN ('pending', 'partial'))"
        )

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM customers c {clause}", params).fetchone()["n"]

    order_sql, order_params = _lateness_order(
        sort=sort,
        late_days=late_days,
        expiry_sql=_customer_expiry_sql(provider),
        today_str=today_str,
    )
    if export_all:
        page = 1
        page_size = EXPORT_PAGE_SIZE
        limit_sql = " LIMIT ?"
        limit_params = [page_size]
    else:
        page = max(1, int(page or 1))
        offset = (page - 1) * page_size
        limit_sql = " LIMIT ? OFFSET ?"
        limit_params = [page_size, offset]
    aggregates = _customer_aggregates_sql(provider=provider) if provider else CUSTOMER_AGGREGATES
    rows = conn.execute(
        f"SELECT c.*, {aggregates} FROM customers c {clause} "
        f"ORDER BY {order_sql}{limit_sql}",
        [*params, *order_params, *limit_params],
    ).fetchall()

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


def get_customer(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT c.*, {CUSTOMER_AGGREGATES} FROM customers c WHERE c.id = ?", (customer_id,)
    ).fetchone()


def customer_connections(conn: sqlite3.Connection, customer_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT cn.*, p.name AS package_name, p.price_paise AS package_price_paise, "
        "       p.validity_days AS package_validity_days, "
        "       p.gst_percentage AS package_gst, "
        "       (SELECT j.status FROM upstream_jobs j WHERE j.connection_id = cn.id "
        "        ORDER BY j.id DESC LIMIT 1) AS last_job_status, "
        "       (SELECT j.id FROM upstream_jobs j WHERE j.connection_id = cn.id "
        "        ORDER BY j.id DESC LIMIT 1) AS last_job_id, "
        "       (SELECT j.action FROM upstream_jobs j WHERE j.connection_id = cn.id "
        "        ORDER BY j.id DESC LIMIT 1) AS last_job_action "
        "FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.customer_id = ? ORDER BY cn.provider, cn.upstream_id",
        (customer_id,),
    ).fetchall()


def customer_bills(conn: sqlite3.Connection, customer_id: int, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bills WHERE customer_id = ? ORDER BY id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def customer_payments(conn: sqlite3.Connection, customer_id: int, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM payments WHERE customer_id = ? ORDER BY paid_at DESC, id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def customer_jobs(conn: sqlite3.Connection, customer_id: int, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT j.*, cn.upstream_id FROM upstream_jobs j "
        "LEFT JOIN connections cn ON cn.id = j.connection_id "
        "WHERE j.customer_id = ? ORDER BY j.id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def customer_railtel_invoices(
    conn: sqlite3.Connection, customer_id: int, *, limit: int = 12
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ri.*, cn.upstream_id AS connection_upstream_id "
        "FROM railtel_invoices ri "
        "LEFT JOIN connections cn ON cn.id = ri.connection_id "
        "WHERE ri.customer_id = ? ORDER BY ri.id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def get_railtel_invoice(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM railtel_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()


def customer_statement(conn: sqlite3.Connection, customer_id: int) -> list[dict]:
    """One running-balance ledger, oldest first, in the style Bix shows.

    A bill pushes the balance up, a payment pulls it down, and `balance` after each
    row is what the customer owed at that moment. This is a view over the same bills
    and payments used elsewhere — nothing is stored twice.
    """
    events: list[dict] = []

    for row in conn.execute(
        "SELECT id, bill_no, package_name, period_start, period_end, total_paise, "
        "created_at, source, notes "
        "FROM bills WHERE customer_id = ? AND status != 'cancelled'",
        (customer_id,),
    ):
        period = ""
        if row["period_start"] and row["period_end"]:
            period = f"{fmt_date_display(row['period_start'])} to {fmt_date_display(row['period_end'])}"
        if (row["source"] or "") == "balance_adjust" or (row["package_name"] or "") == "Balance adjustment":
            description = "Balance increased"
            if row["notes"]:
                description += f" — {row['notes']}"
        else:
            description = f"Bill for {row['package_name'] or 'service'}"
            if period:
                description += f" ({period})"
            if row["notes"] and (row["source"] or "") in {"bix_sync", "collect_later"}:
                description += f" — {row['notes']}"
        events.append({
            "kind": "bill",
            "at": row["created_at"] or "",
            "sort_at": row["period_start"] or row["created_at"] or "",
            "ref": row["bill_no"],
            "ref_id": int(row["id"]),
            "description": description,
            "debit_paise": int(row["total_paise"] or 0),
            "credit_paise": 0,
        })

    for row in conn.execute(
        "SELECT id, receipt_no, amount_paise, mode, reference, paid_at, notes "
        "FROM payments WHERE customer_id = ?",
        (customer_id,),
    ):
        mode = (row["mode"] or "cash").replace("_", " ")
        ref = f" ref {row['reference']}" if row["reference"] else ""
        if (row["mode"] or "").lower() == "adjustment":
            description = "Balance reduced"
            if row["notes"]:
                description += f" — {row['notes']}"
        else:
            description = f"Payment by {mode}{ref}"
            if row["notes"]:
                description += f" — {row['notes']}"
        events.append({
            "kind": "payment",
            "at": row["paid_at"] or "",
            "sort_at": row["paid_at"] or "",
            "ref": row["receipt_no"],
            "ref_id": int(row["id"]),
            "description": description,
            "debit_paise": 0,
            "credit_paise": int(row["amount_paise"] or 0),
        })

    events.sort(key=lambda e: (e["sort_at"], 0 if e["kind"] == "bill" else 1, e["ref_id"]))

    balance = 0
    for event in events:
        balance += event["debit_paise"] - event["credit_paise"]
        event["balance_paise"] = balance
    return events


def get_connection(conn: sqlite3.Connection, connection_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT cn.*, c.name AS customer_name, c.code AS customer_code, p.name AS package_name "
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        "LEFT JOIN packages p ON p.id = cn.package_id WHERE cn.id = ?",
        (connection_id,),
    ).fetchone()


# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #

PACKAGE_IN_USE = (
    "EXISTS (SELECT 1 FROM connections cn WHERE cn.package_id = p.id)"
)


def list_packages(conn: sqlite3.Connection, *, provider: str = "", query: str = "",
                  only_active: bool = False, in_use: bool | None = None,
                  limit: int | None = None) -> list[sqlite3.Row]:
    """List plans. `in_use=True` keeps only plans some connection is on.

    The full catalog is ~800 rows, most of them à-la-carte entries nobody is on,
    so callers that render a form per row should pass `in_use` or `limit`.
    """
    where: list[str] = []
    params: list = []
    if provider:
        where.append("p.provider = ?")
        params.append(provider)
    if query:
        where.append("p.name LIKE ?")
        params.append(f"%{query.strip()}%")
    if only_active:
        where.append("p.active = 1")
    if in_use is True:
        where.append(PACKAGE_IN_USE)
    elif in_use is False:
        where.append(f"NOT {PACKAGE_IN_USE}")

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    tail = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"SELECT p.*, (SELECT COUNT(*) FROM connections cn WHERE cn.package_id = p.id) AS subscriber_count "
        f"FROM packages p {clause} ORDER BY p.provider, p.name COLLATE NOCASE{tail}",
        params,
    ).fetchall()


def count_packages(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        f"SELECT COUNT(*) AS total, "
        f"SUM(CASE WHEN {PACKAGE_IN_USE} THEN 1 ELSE 0 END) AS in_use FROM packages p"
    ).fetchone()
    total = int(row["total"] or 0)
    in_use = int(row["in_use"] or 0)
    return {"total": total, "in_use": in_use, "unused": total - in_use}


# --------------------------------------------------------------------------- #
# Jobs / bills / payments lists
# --------------------------------------------------------------------------- #

def list_jobs(conn: sqlite3.Connection, *, status: str = "", limit: int = 200) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []
    if status == "open":
        where.append("j.status IN ('awaiting_confirm', 'awaiting_otp', 'queued', 'running')")
    elif status:
        where.append("j.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"SELECT j.*, c.name AS customer_name, c.code AS customer_code, cn.upstream_id, "
        f"       cn.card_number, b.bill_no, b.total_paise AS bill_total_paise "
        f"FROM upstream_jobs j "
        f"LEFT JOIN customers c ON c.id = j.customer_id "
        f"LEFT JOIN connections cn ON cn.id = j.connection_id "
        f"LEFT JOIN bills b ON b.id = j.bill_id {clause} "
        f"ORDER BY CASE j.status WHEN 'awaiting_otp' THEN 0 WHEN 'awaiting_confirm' THEN 1 "
        f"         WHEN 'running' THEN 2 WHEN 'queued' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END, "
        f"j.id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT j.*, c.name AS customer_name, c.code AS customer_code, cn.upstream_id "
        "FROM upstream_jobs j "
        "LEFT JOIN customers c ON c.id = j.customer_id "
        "LEFT JOIN connections cn ON cn.id = j.connection_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()


def list_bills(conn: sqlite3.Connection, *, status: str = "", limit: int = 200) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []
    if status == "open":
        where.append("b.status IN ('pending', 'partial')")
    elif status:
        where.append("b.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"SELECT b.*, c.name AS customer_name, c.code AS customer_code, c.phone "
        f"FROM bills b JOIN customers c ON c.id = b.customer_id {clause} "
        f"ORDER BY b.id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()


def list_payments(
    conn: sqlite3.Connection,
    *,
    date_from: str = "",
    date_to: str = "",
    include_adjustments: bool = False,
    limit: int = 500,
) -> dict:
    """Payments in an optional paid-on date range (YYYY-MM-DD), newest first."""
    where: list[str] = []
    params: list = []
    if date_from:
        where.append("substr(p.paid_at, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(p.paid_at, 1, 10) <= ?")
        params.append(date_to)
    if not include_adjustments:
        where.append(NOT_ADJUSTMENT)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(
        f"SELECT COALESCE(SUM(p.amount_paise), 0) AS n, COUNT(*) AS c "
        f"FROM payments p {clause}",
        params,
    ).fetchone()
    rows = conn.execute(
        f"SELECT p.*, c.name AS customer_name, c.code AS customer_code "
        f"FROM payments p JOIN customers c ON c.id = p.customer_id {clause} "
        f"ORDER BY p.paid_at DESC, p.id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return {
        "rows": rows,
        "total_paise": int(total["n"] or 0),
        "count": int(total["c"] or 0),
    }


def get_bill(conn: sqlite3.Connection, bill_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT b.*, c.name AS customer_name, c.code AS customer_code, c.phone, c.address "
        "FROM bills b JOIN customers c ON c.id = b.customer_id WHERE b.id = ?",
        (bill_id,),
    ).fetchone()


def get_payment(conn: sqlite3.Connection, payment_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, c.name AS customer_name, c.code AS customer_code, c.phone, c.address "
        "FROM payments p JOIN customers c ON c.id = p.customer_id WHERE p.id = ?",
        (payment_id,),
    ).fetchone()


def payment_allocations(conn: sqlite3.Connection, payment_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT bp.amount_paise, b.id AS bill_id, b.bill_no, b.period_start, b.period_end, "
        "       b.package_name, b.notes, b.source "
        "FROM bill_payments bp JOIN bills b ON b.id = bp.bill_id "
        "WHERE bp.payment_id = ? ORDER BY b.id",
        (payment_id,),
    ).fetchall()


def provider_overview(conn: sqlite3.Connection) -> list[dict]:
    """Per-provider dealer snapshot plus what we hold locally for that provider."""
    from .upstream import PROVIDER_LABELS, PROVIDERS, id_problem

    snapshots = {
        row["provider"]: row
        for row in conn.execute("SELECT * FROM provider_status")
    }

    out: list[dict] = []
    for provider in PROVIDERS:
        counts = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active "
            "FROM connections WHERE provider = ?",
            (provider,),
        ).fetchone()

        unusable = [
            row for row in conn.execute(
                "SELECT cn.id, cn.upstream_id, cn.customer_id, c.name AS customer_name, "
                "c.code AS customer_code FROM connections cn "
                "JOIN customers c ON c.id = cn.customer_id WHERE cn.provider = ?",
                (provider,),
            )
            if id_problem(provider, row["upstream_id"] or "")
        ]

        snapshot = snapshots.get(provider)
        sub_snap = latest_railtel_subscribers(conn) if provider == "railtel" else None
        portal_active = (snapshot["active_count"] if snapshot else "") or ""
        portal_inactive = (snapshot["inactive_count"] if snapshot else "") or ""
        portal_total = (snapshot["total_count"] if snapshot else "") or ""
        if sub_snap:
            portal_active = int(sub_snap["active_count"] or 0)
            portal_inactive = int(sub_snap["expiring_7d"] or 0)
            portal_total = int(sub_snap["total_count"] or 0)
        out.append({
            "provider": provider,
            "label": PROVIDER_LABELS[provider],
            "local_total": int(counts["total"] or 0),
            "local_active": int(counts["active"] or 0),
            "wallet_balance": (snapshot["wallet_balance"] if snapshot else "") or "",
            "portal_active": portal_active,
            "portal_inactive": portal_inactive,
            "portal_total": portal_total,
            "operator_name": (snapshot["operator_name"] if snapshot else "") or "",
            "checked_at": (snapshot["checked_at"] if snapshot else "") or "",
            "subscribers_at": (sub_snap["fetched_at"] if sub_snap else "") or "",
            "subscribers_expired": int(sub_snap["expired_count"] or 0) if sub_snap else "",
            "error": (snapshot["error"] if snapshot else "") or "",
            "unusable": unusable,
        })
    return out


def recent_sweeps(conn: sqlite3.Connection, limit: int = 6) -> list[dict]:
    """Past bulk status checks with their outcome counts."""
    out: list[dict] = []
    for sweep in conn.execute(
        "SELECT * FROM sync_sweeps ORDER BY id DESC LIMIT ?", (limit,)
    ):
        counts = {
            row["status"]: int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? "
                "GROUP BY status", (sweep["id"],)
            )
        }
        terminated = int(conn.execute(
            "SELECT COUNT(*) AS n FROM upstream_jobs WHERE sweep_id = ? AND status = 'failed' "
            "AND LOWER(COALESCE(error, '')) LIKE '%terminated%'", (sweep["id"],)
        ).fetchone()["n"])
        out.append({
            "row": sweep,
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0),
            "pending": counts.get("queued", 0) + counts.get("running", 0),
            "terminated_found": terminated,
        })
    return out


def unusable_connections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Connections whose provider id does not match their provider's format."""
    from .upstream import id_problem

    rows = conn.execute(
        "SELECT cn.id, cn.provider, cn.upstream_id, cn.customer_id, cn.status, "
        "c.name AS customer_name, c.code AS customer_code "
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id ORDER BY c.name"
    ).fetchall()
    return [r for r in rows if id_problem(r["provider"], r["upstream_id"] or "")]


def recent_activity(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, c.name AS customer_name FROM activity_log a "
        "LEFT JOIN customers c ON c.id = a.customer_id ORDER BY a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Renewed / enabled, not paid
# --------------------------------------------------------------------------- #

def collect_later_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_paise - paid_paise), 0) AS due, "
        "COUNT(DISTINCT customer_id) AS customers, "
        "SUM(CASE WHEN followup_kind = 'manual' THEN 1 ELSE 0 END) AS manual_n, "
        "SUM(CASE WHEN followup_kind != 'manual' THEN 1 ELSE 0 END) AS renew_n "
        "FROM bills WHERE collect_later = 1 AND status IN ('pending', 'partial')"
    ).fetchone()
    return {
        "count": int(row["n"] or 0),
        "customers": int(row["customers"] or 0),
        "due_paise": int(row["due"] or 0),
        "manual": int(row["manual_n"] or 0),
        "renew": int(row["renew_n"] or 0),
    }


def list_collect_later(
    conn: sqlite3.Connection, *, kind: str = "", limit: int = 200
) -> list[sqlite3.Row]:
    """Open follow-up bills — newest first. kind is '', 'manual', or 'renew'."""
    where = "b.collect_later = 1 AND b.status IN ('pending', 'partial')"
    if kind == "manual":
        where += " AND b.followup_kind = 'manual'"
    elif kind == "renew":
        where += " AND b.followup_kind != 'manual'"
    return conn.execute(
        "SELECT b.*, c.name AS customer_name, c.phone, c.code AS customer_code, "
        "       cn.upstream_id, cn.provider "
        "FROM bills b "
        "JOIN customers c ON c.id = b.customer_id "
        "LEFT JOIN connections cn ON cn.id = b.connection_id "
        f"WHERE {where} "
        "ORDER BY CASE b.followup_kind WHEN 'manual' THEN 0 ELSE 1 END, b.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def customer_collect_later(conn: sqlite3.Connection, customer_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.*, cn.upstream_id, cn.provider "
        "FROM bills b LEFT JOIN connections cn ON cn.id = b.connection_id "
        "WHERE b.customer_id = ? AND b.collect_later = 1 AND b.status IN ('pending', 'partial') "
        "ORDER BY b.id",
        (customer_id,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Hathway STB mapping
# --------------------------------------------------------------------------- #

# Real Hathway set-top boxes are N + 11 digits. Viewing cards (T…) do not count.
HATHWAY_STB_GLOB = "N[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"


def _as_int_count(value) -> int:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def hathway_mapping_stats(conn: sqlite3.Connection) -> dict:
    """Portal 571+22 snapshot versus STBs already assigned to a customer here."""
    snap = conn.execute(
        "SELECT * FROM provider_status WHERE provider = 'hathway'"
    ).fetchone()
    portal_active = _as_int_count(snap["active_count"] if snap else 0)
    portal_inactive = _as_int_count(snap["inactive_count"] if snap else 0)
    portal_total = _as_int_count(snap["total_count"] if snap else 0)
    if portal_total <= 0:
        portal_total = portal_active + portal_inactive

    mapped_total = conn.execute(
        "SELECT COUNT(*) AS n FROM connections "
        "WHERE provider = 'hathway' AND upper(upstream_id) GLOB ?",
        (HATHWAY_STB_GLOB,),
    ).fetchone()["n"]
    mapped_active = conn.execute(
        "SELECT COUNT(*) AS n FROM connections "
        "WHERE provider = 'hathway' AND status = 'active' "
        "AND upper(upstream_id) GLOB ?",
        (HATHWAY_STB_GLOB,),
    ).fetchone()["n"]
    live_customers = conn.execute(
        "SELECT COUNT(DISTINCT customer_id) AS n FROM connections "
        "WHERE provider = 'hathway' AND status = 'active' "
        "AND upper(upstream_id) GLOB ?",
        (HATHWAY_STB_GLOB,),
    ).fetchone()["n"]
    hathway_customers = conn.execute(
        "SELECT COUNT(DISTINCT customer_id) AS n FROM connections "
        "WHERE provider = 'hathway' AND upper(upstream_id) GLOB ?",
        (HATHWAY_STB_GLOB,),
    ).fetchone()["n"]
    unmapped_local = conn.execute(
        "SELECT COUNT(*) AS n FROM connections "
        "WHERE provider = 'hathway' "
        "AND upper(COALESCE(upstream_id, '')) NOT GLOB ?",
        (HATHWAY_STB_GLOB,),
    ).fetchone()["n"]
    portal_gap = max(0, portal_total - mapped_total)
    extra_local = max(0, mapped_total - portal_total) if portal_total else 0

    return {
        "portal_active": portal_active,
        "portal_inactive": portal_inactive,
        "portal_total": portal_total,
        "checked_at": (snap["checked_at"] if snap else "") or "",
        "mapped_total": int(mapped_total),
        "mapped_active": int(mapped_active),
        "mapped_inactive": int(mapped_total) - int(mapped_active),
        "live_customers": int(live_customers),
        "hathway_customers": int(hathway_customers),
        "unmapped_local": int(unmapped_local),
        "portal_gap": int(portal_gap),
        "extra_local": int(extra_local),
        "unmapped": int(unmapped_local) + int(portal_gap),
    }


def list_hathway_stbs(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    view: str = "running",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    export_all: bool = False,
) -> dict:
    """Hathway set-top boxes only — Railtel ids never appear here."""
    from .upstream.providers import id_problem

    where: list[str] = ["cn.provider = 'hathway'"]
    params: list = []
    view = (view or "running").strip()
    if view not in ("running", "inactive", "all", "unmapped"):
        view = "running"

    if view == "unmapped":
        where.append("upper(COALESCE(cn.upstream_id, '')) NOT GLOB ?")
        params.append(HATHWAY_STB_GLOB)
    else:
        where.append("upper(cn.upstream_id) GLOB ?")
        params.append(HATHWAY_STB_GLOB)
        if view == "running":
            where.append("cn.status = 'active'")
        elif view == "inactive":
            where.append("cn.status != 'active'")

    text = (query or "").strip()
    if text:
        like = f"%{text}%"
        where.append(
            "(c.name LIKE ? OR c.phone LIKE ? OR c.code LIKE ? OR c.sub_area LIKE ? "
            "OR cn.upstream_id LIKE ? OR cn.card_number LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])

    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM connections cn "
        f"JOIN customers c ON c.id = cn.customer_id WHERE {clause}",
        params,
    ).fetchone()["n"]

    if export_all:
        page = 1
        page_size = EXPORT_PAGE_SIZE
        limit_sql = " LIMIT ?"
        limit_params = [page_size]
    else:
        page = max(1, int(page or 1))
        offset = (page - 1) * page_size
        limit_sql = " LIMIT ? OFFSET ?"
        limit_params = [page_size, offset]
    rows = conn.execute(
        f"SELECT cn.id, cn.upstream_id, cn.card_number, cn.status, cn.expiry_date, "
        f"       cn.upstream_plan_name, cn.last_synced_at, "
        f"       c.id AS customer_id, c.name AS customer_name, c.phone, "
        f"       c.code AS customer_code, c.sub_area, "
        f"       p.name AS package_name "
        f"FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        f"LEFT JOIN packages p ON p.id = cn.package_id "
        f"WHERE {clause} ORDER BY c.name COLLATE NOCASE, cn.upstream_id "
        f"{limit_sql}",
        [*params, *limit_params],
    ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        item["problem"] = id_problem("hathway", row["upstream_id"] or "") or ""
        items.append(item)

    return {
        "rows": items,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "pages": max(1, (int(total) + page_size - 1) // page_size),
        "view": view,
    }


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

def field_office_summary(conn: sqlite3.Connection) -> dict:
    from .field import office_summary

    return office_summary(conn)


def dashboard_stats(conn: sqlite3.Connection) -> dict:
    now = today()
    today_str = now.strftime("%Y-%m-%d")
    soon_str = add_days(now, settings.expiring_soon_days).strftime("%Y-%m-%d")
    month_start = now.replace(day=1).strftime("%Y-%m-%d")

    def one(sql: str, params: tuple = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0

    customers_total = one("SELECT COUNT(*) FROM customers")
    connections_total = one("SELECT COUNT(*) FROM connections")
    connections_active = one("SELECT COUNT(*) FROM connections WHERE status = 'active'")

    by_provider = conn.execute(
        "SELECT provider, COUNT(*) AS n, "
        "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_n "
        "FROM connections GROUP BY provider ORDER BY provider"
    ).fetchall()

    expiring = one(
        "SELECT COUNT(*) FROM connections WHERE status = 'active' AND expiry_date IS NOT NULL "
        "AND expiry_date != '' AND expiry_date BETWEEN ? AND ?",
        (today_str, soon_str),
    )
    expired = one(
        "SELECT COUNT(*) FROM connections WHERE status = 'active' AND expiry_date IS NOT NULL "
        "AND expiry_date != '' AND expiry_date < ?",
        (today_str,),
    )
    outstanding = one(
        "SELECT COALESCE(SUM(total_paise - paid_paise), 0) FROM bills "
        "WHERE status IN ('pending', 'partial')"
    )
    open_bills = one("SELECT COUNT(*) FROM bills WHERE status IN ('pending', 'partial')")
    collected_today = one(
        f"SELECT COALESCE(SUM(amount_paise), 0) FROM payments "
        f"WHERE substr(paid_at, 1, 10) = ? AND {NOT_ADJUSTMENT}",
        (today_str,),
    )
    collected_month = one(
        f"SELECT COALESCE(SUM(amount_paise), 0) FROM payments "
        f"WHERE substr(paid_at, 1, 10) >= ? AND {NOT_ADJUSTMENT}",
        (month_start,),
    )

    jobs = conn.execute(
        "SELECT status, COUNT(*) AS n FROM upstream_jobs GROUP BY status"
    ).fetchall()
    job_counts = {row["status"]: int(row["n"]) for row in jobs}

    return {
        "customers_total": customers_total,
        "connections_total": connections_total,
        "connections_active": connections_active,
        "by_provider": by_provider,
        "expiring": expiring,
        "expired": expired,
        "outstanding_paise": outstanding,
        "open_bills": open_bills,
        "collected_today_paise": collected_today,
        "collected_month_paise": collected_month,
        "packages_total": one("SELECT COUNT(*) FROM packages"),
        "jobs": job_counts,
        "jobs_awaiting": job_counts.get("awaiting_confirm", 0),
        "jobs_otp": job_counts.get("awaiting_otp", 0),
        "jobs_queued": job_counts.get("queued", 0) + job_counts.get("running", 0)
        + job_counts.get("awaiting_otp", 0),
        "jobs_failed": job_counts.get("failed", 0),
        "expiring_soon_days": settings.expiring_soon_days,
        "today": today_str,
        "month_start": month_start,
        "complaints_open": one("SELECT COUNT(*) FROM complaints WHERE status != 'fixed'"),
        "complaints_fixed_today": one(
            "SELECT COUNT(*) FROM complaints WHERE status = 'fixed' AND substr(COALESCE(resolved_at, ''), 1, 10) = ?",
            (today_str,),
        ),
        "hathway": hathway_mapping_stats(conn),
        "iptv": iptv_stats(conn),
        "ott": ott_stats(conn),
        "railtel": railtel_stats(conn),
        "followups": collect_later_stats(conn),
        "field": field_office_summary(conn),
    }


def prepaid_connection_stats(conn: sqlite3.Connection, provider: str) -> dict:
    """Local prepaid connections (ANT IPTV or SmartPlay OTT) grouped by expiry."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active, "
        "COUNT(DISTINCT customer_id) AS customers "
        "FROM connections WHERE provider = ?",
        (provider,),
    ).fetchone()
    today_str = today().strftime("%Y-%m-%d")
    soon_str = add_days(today(), settings.expiring_soon_days).strftime("%Y-%m-%d")
    expiring = conn.execute(
        "SELECT COUNT(*) AS n FROM connections "
        "WHERE provider = ? AND status = 'active' "
        "AND expiry_date IS NOT NULL AND expiry_date != '' "
        "AND expiry_date BETWEEN ? AND ?",
        (provider, today_str, soon_str),
    ).fetchone()["n"]
    expired = conn.execute(
        "SELECT COUNT(*) AS n FROM connections "
        "WHERE provider = ? AND status = 'active' "
        "AND expiry_date IS NOT NULL AND expiry_date != '' AND expiry_date < ?",
        (provider, today_str),
    ).fetchone()["n"]
    packs = conn.execute(
        "SELECT COUNT(*) AS n FROM packages WHERE provider = ? AND active = 1",
        (provider,),
    ).fetchone()["n"]
    live = conn.execute(
        "SELECT COUNT(*) AS n FROM connections "
        "WHERE provider = ? AND status = 'active' "
        "AND (expiry_date IS NULL OR expiry_date = '' OR expiry_date >= ?)",
        (provider, today_str),
    ).fetchone()["n"]
    running = int(live or 0) - int(expiring or 0)
    if running < 0:
        running = 0
    snap = conn.execute(
        "SELECT wallet_balance, active_count, inactive_count, total_count, operator_name, "
        "checked_at FROM provider_status WHERE provider = ?",
        (provider,),
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "active": int(row["active"] or 0),
        "live": int(live or 0),
        "running": running,
        "customers": int(row["customers"] or 0),
        "expiring": int(expiring or 0),
        "expired": int(expired or 0),
        "packages": int(packs or 0),
        "wallet_balance": (snap["wallet_balance"] if snap else "") or "",
        "portal_active": (snap["active_count"] if snap else "") or "",
        "portal_expired": (snap["inactive_count"] if snap else "") or "",
        "portal_total": (snap["total_count"] if snap else "") or "",
        "operator": (snap["operator_name"] if snap else "") or "",
        "checked_at": (snap["checked_at"] if snap else "") or "",
    }


def _railtel_expiry_breakdown(conn: sqlite3.Connection, today_str: str) -> dict:
    """Expired / 1d late / 2d late from stored expiry dates (any connection status)."""
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN expiry_date IS NOT NULL AND expiry_date != '' AND expiry_date < ? "
        "    THEN 1 ELSE 0 END) AS expired, "
        "SUM(CASE WHEN expiry_date IS NOT NULL AND expiry_date != '' "
        "    AND julianday(?) - julianday(expiry_date) = 1 THEN 1 ELSE 0 END) AS late_1d, "
        "SUM(CASE WHEN expiry_date IS NOT NULL AND expiry_date != '' "
        "    AND julianday(?) - julianday(expiry_date) = 2 THEN 1 ELSE 0 END) AS late_2d "
        "FROM connections WHERE provider = 'railtel'",
        (today_str, today_str, today_str),
    ).fetchone()
    return {
        "expired": int(row["expired"] or 0),
        "late_1d": int(row["late_1d"] or 0),
        "late_2d": int(row["late_2d"] or 0),
    }


def latest_railtel_subscribers(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM railtel_subscriber_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()


def railtel_stats(conn: sqlite3.Connection) -> dict:
    """My Subscribers snapshot (authoritative expiry) + online list + wallet KPIs."""
    data = prepaid_connection_stats(conn, "railtel")
    today_str = today().strftime("%Y-%m-%d")
    subs = latest_railtel_subscribers(conn)
    if subs:
        data["expired"] = int(subs["expired_count"] or 0)
        data["late_1d"] = int(subs["late_1d"] or 0)
        data["late_2d"] = int(subs["late_2d"] or 0)
        data["portal_expiring_7d"] = int(subs["expiring_7d"] or 0)
        data["portal_active"] = int(subs["active_count"] or 0)
        data["subscribers_at"] = (subs["fetched_at"] or "") or ""
        data["portal_total"] = int(subs["total_count"] or 0)
    else:
        expiry = _railtel_expiry_breakdown(conn, today_str)
        data["expired"] = expiry["expired"]
        data["late_1d"] = expiry["late_1d"]
        data["late_2d"] = expiry["late_2d"]
        ps = conn.execute(
            "SELECT active_count, inactive_count, checked_at FROM provider_status "
            "WHERE provider = 'railtel'"
        ).fetchone()
        if ps:
            data["portal_active"] = (ps["active_count"] or "") or data.get("portal_active", "")
            data["portal_expiring_7d"] = (ps["inactive_count"] or "") or ""
            data["portal_active_at"] = (ps["checked_at"] or "") or ""
    snap = latest_railtel_online(conn)
    data["online"] = int(snap["online_count"] or 0) if snap else 0
    data["online_at"] = (snap["fetched_at"] if snap else "") or ""
    return data


def iptv_stats(conn: sqlite3.Connection) -> dict:
    """Local ANT IPTV connections — no live portal counts until OTP login is automated."""
    return prepaid_connection_stats(conn, "iptv")


def ott_stats(conn: sqlite3.Connection) -> dict:
    """Local SmartPlay OTT connections plus the last dealer-wallet snapshot."""
    return prepaid_connection_stats(conn, "ott")


def list_prepaid_subscriptions(
    conn: sqlite3.Connection,
    provider: str,
    *,
    query: str = "",
    view: str = "all",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sort: str = "",
    late_days: int | None = None,
    export_all: bool = False,
) -> dict:
    """Prepaid phone subscriptions: all / active / expiring / expired."""
    today_str = today().strftime("%Y-%m-%d")
    soon_str = add_days(today(), settings.expiring_soon_days).strftime("%Y-%m-%d")
    where: list[str] = ["cn.provider = ?"]
    params: list = [provider]
    view = (view or "all").strip()
    if view not in ("all", "active", "expiring", "expired"):
        view = "all"

    if view == "active":
        where.append("cn.status = 'active'")
        where.append("(cn.expiry_date IS NULL OR cn.expiry_date = '' OR cn.expiry_date > ?)")
        params.append(soon_str)
    elif view == "expiring":
        where.append("cn.status = 'active'")
        where.append("cn.expiry_date IS NOT NULL AND cn.expiry_date != ''")
        where.append("cn.expiry_date BETWEEN ? AND ?")
        params.extend([today_str, soon_str])
    elif view == "expired":
        where.append("cn.expiry_date IS NOT NULL AND cn.expiry_date != '' AND cn.expiry_date < ?")
        params.append(today_str)

    text = (query or "").strip()
    if text:
        like = f"%{text}%"
        where.append(
            "(c.name LIKE ? OR c.phone LIKE ? OR c.code LIKE ? OR cn.upstream_id LIKE ? "
            "OR COALESCE(p.name, cn.upstream_plan_name, '') LIKE ?)"
        )
        params.extend([like, like, like, like, like])

    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM connections cn "
        f"JOIN customers c ON c.id = cn.customer_id "
        f"LEFT JOIN packages p ON p.id = cn.package_id WHERE {clause}",
        params,
    ).fetchone()["n"]

    order_sql, order_params = _lateness_order(
        sort=sort,
        late_days=late_days,
        expiry_sql="cn.expiry_date",
        today_str=today_str,
    )
    if export_all:
        page = 1
        page_size = EXPORT_PAGE_SIZE
        limit_sql = " LIMIT ?"
        limit_params = [page_size]
    else:
        page = max(1, int(page or 1))
        offset = (page - 1) * page_size
        limit_sql = " LIMIT ? OFFSET ?"
        limit_params = [page_size, offset]
    rows = conn.execute(
        f"SELECT cn.id, cn.upstream_id, cn.status, cn.expiry_date, cn.upstream_plan_name, "
        f"       cn.last_synced_at, p.name AS package_name, p.price_paise AS package_price_paise, "
        f"       c.id AS customer_id, c.name AS customer_name, c.phone, "
        f"       c.code AS customer_code, c.area, c.sub_area, "
        f"       (SELECT j.status FROM upstream_jobs j WHERE j.connection_id = cn.id "
        f"        ORDER BY j.id DESC LIMIT 1) AS last_job_status, "
        f"       (SELECT j.id FROM upstream_jobs j WHERE j.connection_id = cn.id "
        f"        ORDER BY j.id DESC LIMIT 1) AS last_job_id "
        f"FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        f"LEFT JOIN packages p ON p.id = cn.package_id "
        f"WHERE {clause} ORDER BY {order_sql}{limit_sql}",
        [*params, *order_params, *limit_params],
    ).fetchall()

    return {
        "rows": rows,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "pages": max(1, (int(total) + page_size - 1) // page_size),
        "view": view,
    }


def list_iptv_subscriptions(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    view: str = "all",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sort: str = "",
    late_days: int | None = None,
    export_all: bool = False,
) -> dict:
    """ANT IPTV connections, same buckets as the CRM: all / active / expiring / expired."""
    return list_prepaid_subscriptions(
        conn,
        "iptv",
        query=query,
        view=view,
        page=page,
        page_size=page_size,
        sort=sort,
        late_days=late_days,
        export_all=export_all,
    )


def list_ott_subscriptions(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    view: str = "all",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sort: str = "",
    late_days: int | None = None,
    export_all: bool = False,
) -> dict:
    """SmartPlay OTT connections, same buckets as the IPTV page."""
    return list_prepaid_subscriptions(
        conn,
        "ott",
        query=query,
        view=view,
        page=page,
        page_size=page_size,
        sort=sort,
        late_days=late_days,
        export_all=export_all,
    )


def expiring_connections(conn: sqlite3.Connection, *, days: int | None = None,
                         limit: int = 25) -> list[sqlite3.Row]:
    days = settings.expiring_soon_days if days is None else days
    horizon = add_days(today(), days).strftime("%Y-%m-%d")
    return conn.execute(
        "SELECT cn.*, c.name AS customer_name, c.code AS customer_code, c.phone, "
        "       p.name AS package_name, p.price_paise AS package_price_paise "
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        "LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.status = 'active' AND cn.expiry_date IS NOT NULL AND cn.expiry_date != '' "
        "AND cn.expiry_date <= ? ORDER BY cn.expiry_date LIMIT ?",
        (horizon, limit),
    ).fetchall()


def latest_railtel_online(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM railtel_online_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()


def railtel_online_rows(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    q: str = "",
    view: str = "all",
) -> list[dict]:
    """Online sessions from one snapshot, joined to local customers when we have them."""
    query = (q or "").strip()
    like = f"%{query.lower()}%" if query else None
    rows = conn.execute(
        "SELECT r.*, cn.id AS connection_id, cn.customer_id, cn.status AS connection_status, "
        "       cn.expiry_date, cu.name AS customer_name, cu.code AS customer_code, "
        "       cu.phone AS customer_phone "
        "FROM railtel_online_rows r "
        "LEFT JOIN connections cn ON cn.id = ("
        "    SELECT id FROM connections "
        "    WHERE provider = 'railtel' AND lower(upstream_id) = lower(r.username) "
        "    LIMIT 1) "
        "LEFT JOIN customers cu ON cu.id = cn.customer_id "
        "WHERE r.snapshot_id = ? "
        "ORDER BY r.start_at DESC, r.username",
        (snapshot_id,),
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        item = dict(row)
        if like:
            hay = " ".join(
                str(item.get(k) or "")
                for k in ("username", "customer_name", "customer_code",
                          "customer_phone", "framed_ip", "mac")
            ).lower()
            if like.strip("%") not in hay:
                continue
        if view == "unknown" and item.get("customer_id"):
            continue
        if view == "known" and not item.get("customer_id"):
            continue
        out.append(item)
    return out


def railtel_online_local_offline(conn: sqlite3.Connection, snapshot_id: int) -> list[sqlite3.Row]:
    """Local Railtel connections that do not appear in this online snapshot."""
    return conn.execute(
        "SELECT cn.id, cn.upstream_id, cn.status, cn.expiry_date, cn.link_state, "
        "       cu.id AS customer_id, cu.name AS customer_name, cu.code AS customer_code "
        "FROM connections cn JOIN customers cu ON cu.id = cn.customer_id "
        "WHERE cn.provider = 'railtel' AND cn.status IN ('active', 'suspended') "
        "AND lower(cn.upstream_id) NOT IN ("
        "    SELECT lower(username) FROM railtel_online_rows WHERE snapshot_id = ?"
        ") ORDER BY cu.name, cn.upstream_id",
        (snapshot_id,),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Complaints
# --------------------------------------------------------------------------- #

COMPLAINT_STATUSES = ("open", "in_progress", "fixed")


def list_agents(conn: sqlite3.Connection, *, active_only: bool = True) -> list[sqlite3.Row]:
    clause = "WHERE active = 1" if active_only else ""
    return conn.execute(
        f"SELECT id, name, username, role FROM agents {clause} ORDER BY name COLLATE NOCASE"
    ).fetchall()


def list_complaints(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    agent_id: int | None = None,
    customer_id: int | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []
    if status:
        where.append("cp.status = ?")
        params.append(status)
    if agent_id:
        where.append("cp.assigned_agent_id = ?")
        params.append(agent_id)
    if customer_id:
        where.append("cp.customer_id = ?")
        params.append(customer_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"SELECT cp.*, c.name AS customer_name, c.code AS customer_code, c.phone AS customer_phone, "
        f"a.name AS agent_name "
        f"FROM complaints cp JOIN customers c ON c.id = cp.customer_id "
        f"LEFT JOIN agents a ON a.id = cp.assigned_agent_id "
        f"{clause} ORDER BY CASE cp.status WHEN 'fixed' THEN 1 ELSE 0 END, cp.id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()


def get_complaint(conn: sqlite3.Connection, complaint_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT cp.*, c.name AS customer_name, c.code AS customer_code, c.phone AS customer_phone, "
        "a.name AS agent_name "
        "FROM complaints cp JOIN customers c ON c.id = cp.customer_id "
        "LEFT JOIN agents a ON a.id = cp.assigned_agent_id "
        "WHERE cp.id = ?",
        (complaint_id,),
    ).fetchone()


def recent_fixed_complaints(conn: sqlite3.Connection, limit: int = 8) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT cp.*, c.name AS customer_name, c.code AS customer_code "
        "FROM complaints cp JOIN customers c ON c.id = cp.customer_id "
        "WHERE cp.status = 'fixed' ORDER BY cp.resolved_at DESC, cp.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
