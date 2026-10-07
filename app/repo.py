"""Read queries for the UI. Writes live in billing.py and upstream/jobs.py."""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import timedelta

from .config import settings
from .money import add_days, fmt_date_display, now_iso, today

PAGE_SIZE = 50
EXPORT_PAGE_SIZE = 100_000

# Bix due-align writes mode=adjustment to move the ledger. That is not cash collected.
NOT_ADJUSTMENT = "lower(COALESCE(mode, '')) != 'adjustment'"


def _phone_sql_normalized(column: str) -> str:
    """Strip spaces, dashes, brackets and + so pasted +91 numbers match stored phones."""
    return (
        f"REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), '-', ''), "
        f"'(', ''), ')', ''), '+', '')"
    )


def phone_search_or_columns(columns: list[str], query: str) -> tuple[list[str], list]:
    """Build OR conditions for phone fields — ignores +91, spaces, and punctuation."""
    text = (query or "").strip()
    if not text or not columns:
        return [], []
    conds: list[str] = []
    params: list = []
    like = f"%{text}%"
    for col in columns:
        conds.append(f"{col} LIKE ?")
        params.append(like)
    digits = re.sub(r"\D", "", text)
    if not digits:
        return conds, params
    dlike = f"%{digits}%"
    for col in columns:
        norm = _phone_sql_normalized(col)
        conds.append(f"{norm} LIKE ?")
        params.append(dlike)
    if len(digits) >= 10:
        last10 = digits[-10:]
        for col in columns:
            norm = _phone_sql_normalized(col)
            conds.append(f"({norm} LIKE ? OR {norm} LIKE ?)")
            params.extend([f"%{last10}", f"%91{last10}"])
    return conds, params


def customer_text_search_clause(
    query: str,
    *,
    phone_columns: tuple[str, ...] = ("c.phone", "c.alt_phone"),
    extra_or: str = "",
    extra_params: list | None = None,
) -> tuple[str, list]:
    """Name/code/area/phone search used on customer lists."""
    text = (query or "").strip()
    if not text:
        return "", []
    like = f"%{text}%"
    parts = ["c.name LIKE ?", "c.code LIKE ?", "c.sub_area LIKE ?"]
    params: list = [like, like, like]
    phone_conds, phone_params = phone_search_or_columns(list(phone_columns), text)
    if phone_conds:
        parts.append("(" + " OR ".join(phone_conds) + ")")
        params.extend(phone_params)
    if extra_or:
        parts.append(extra_or)
        params.extend(extra_params or [])
    return "(" + " OR ".join(parts) + ")", params


def _plan_name_sql(cn: str = "cn", p: str = "p") -> str:
    """Catalog plan name first — portal status sync may report a per-day renewal label."""
    return (
        f"lower(COALESCE(NULLIF(trim({p}.name), ''), "
        f"NULLIF(trim({cn}.upstream_plan_name), ''), ''))"
    )


def _term_suffix_sql(col: str) -> str:
    """Match x3/x6/x10/x12 even when extra text follows the token."""
    parts = []
    for token in ("x3", "x6", "x10", "x12"):
        parts.append(
            f"{col} LIKE '% {token}' OR {col} LIKE '% {token} %' OR {col} LIKE '% {token}-%'"
        )
    return " OR ".join(parts)


def _is_railtel_term_renewal_plan_sql(cn: str = "cn", p: str = "p") -> str:
    """Railtel x3/x6/x10/x12 — catalog name or portal plan name, not monthly packs."""
    pkg = f"lower(COALESCE({p}.name, ''))"
    up = f"lower(COALESCE({cn}.upstream_plan_name, ''))"
    return f"({_term_suffix_sql(pkg)}) OR ({_term_suffix_sql(up)})"


def _is_railtel_prepaid_term_sql(cn: str = "cn", p: str = "p") -> str:
    """Railtel x3/x6/x10-style prepaid terms — portal expiry is a top-up cycle, not paid-through."""
    return (
        f"COALESCE({cn}.validity_days, 0) > 0 "
        f"OR COALESCE({p}.validity_days, 0) > 30 "
        f"OR ({_is_railtel_term_renewal_plan_sql(cn, p)}) "
        f"OR {_plan_name_sql(cn, p)} LIKE '%term%'"
    )


# Actual paid-through / term end for expiry lists and Next expiry:
# • Railtel x3/x6/x10/x12: Subscriber Details xpath tr[4] stored as subscription_expiry.
#   Until that date has been read, fall back to My Subscribers renewal (expiry_date)
#   so a term customer is never hidden from expiry lists.
#   Monthly top-up of those plans is the separate "term_month" window.
# • Monthly packs and everyone else: portal expiry_date as usual.
# Custom household payment math is not used here — same date as the connection card.
CONNECTION_EFFECTIVE_EXPIRY_SQL = f"""
(
  CASE
    WHEN cn.provider = 'railtel' AND ({_is_railtel_term_renewal_plan_sql()})
         AND trim(COALESCE(cn.subscription_expiry, '')) >= '2000'
      THEN trim(cn.subscription_expiry)
    ELSE NULLIF(trim(COALESCE(cn.expiry_date, '')), '')
  END
)
"""

# Soonest connection paid-through among active connections (same rule as the card).
NEXT_EXPIRY_SQL = f"""
(SELECT MIN({CONNECTION_EFFECTIVE_EXPIRY_SQL}) FROM connections cn
   LEFT JOIN packages p ON p.id = cn.package_id
   WHERE cn.customer_id = c.id AND cn.status = 'active'
     AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL)
"""


EXPIRY_WHEN_WINDOWS: dict[str, dict] = {
    "tonight": {"kind": "tonight", "label": "Expiring tonight"},
    "term_month": {
        "kind": "month",
        "label": "Railtel term plans this month",
    },
    "1d": {"kind": "exact", "days_late": 1, "label": "Expired 1 day ago"},
    "2d": {"kind": "exact", "days_late": 2, "label": "Expired 2 days ago"},
    "7d": {"kind": "from", "days_late": 7, "label": "Expired 7+ days ago"},
}

# Backward-compat for old links.
_EXPIRY_WHEN_ALIASES = {"3d": "2d"}

EXPIRY_WHEN_LABELS: dict[str, str] = {
    key: spec["label"] for key, spec in EXPIRY_WHEN_WINDOWS.items()
}


def connection_is_railtel_term(conn: sqlite3.Connection, connection_id: int) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        f"WHERE cn.id = ? AND cn.provider = 'railtel' AND ({_is_railtel_term_renewal_plan_sql()})",
        (int(connection_id),),
    ).fetchone()
    return row is not None


def parse_expiry_when(when: str = "") -> tuple[str, dict]:
    key = (when or "tonight").strip().lower()
    key = _EXPIRY_WHEN_ALIASES.get(key, key)
    if key not in EXPIRY_WHEN_WINDOWS:
        key = "tonight"
    return key, EXPIRY_WHEN_WINDOWS[key]


def _expiry_window_where(
    window: dict,
    *,
    expiry_sql: str = CONNECTION_EFFECTIVE_EXPIRY_SQL,
) -> tuple[str, list]:
    today_str = today().strftime("%Y-%m-%d")
    kind = window.get("kind", "tonight")
    if kind == "tonight":
        return (
            "cn.status = 'active' AND "
            f"({expiry_sql}) IS NOT NULL AND "
            f"({expiry_sql}) = ?",
            [today_str],
        )
    status = "cn.status IN ('active', 'inactive', 'expired', 'suspended')"
    base = f"{status} AND ({expiry_sql}) IS NOT NULL AND julianday({expiry_sql}) < julianday(?)"
    params: list = [today_str]
    days_late = int(window.get("days_late") or 0)
    if kind == "exact":
        where = (
            f"{base} AND date({expiry_sql}) = date(?, '-' || ? || ' days')"
        )
        params.extend([today_str, str(days_late)])
        return where, params
    if kind == "from":
        where = f"{base} AND CAST(julianday(?) - julianday({expiry_sql}) AS INTEGER) >= ?"
        params.extend([today_str, days_late])
        return where, params
    if kind == "month":
        now = today()
        month_start = now.replace(day=1).strftime("%Y-%m-%d")
        month_end = now.replace(
            day=calendar.monthrange(now.year, now.month)[1]
        ).strftime("%Y-%m-%d")
        # Portal renewal date in the calendar month; include active/inactive/suspended.
        where = (
            "cn.provider = 'railtel' AND "
            f"({ _is_railtel_term_renewal_plan_sql() }) AND "
            "cn.status != 'terminated' AND "
            "cn.expiry_date IS NOT NULL AND cn.expiry_date != '' AND "
            "cn.expiry_date >= ? AND cn.expiry_date <= ?"
        )
        params = [month_start, month_end]
        return where, params
    raise ValueError(f"Unknown expiry window kind: {kind!r}")


def _expiry_window_joins() -> str:
    return (
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        "LEFT JOIN packages p ON p.id = cn.package_id "
    )


def expiry_window_stats(
    conn: sqlite3.Connection,
    *,
    when: str = "tonight",
    provider: str = "",
    collector_id: int | None = None,
) -> dict:
    key, window = parse_expiry_when(when)
    where, params = _expiry_window_where(window)
    if provider and key != "term_month":
        where += " AND cn.provider = ?"
        params.append(provider)
    if collector_id:
        territory, territory_params = customer_territory_sql(collector_id)
        where += f" AND {territory}"
        params.extend(territory_params)
    row = conn.execute(
        f"SELECT COUNT(*) AS connections, COUNT(DISTINCT cn.customer_id) AS customers "
        f"{_expiry_window_joins()}WHERE {where}",
        params,
    ).fetchone()
    return {
        "connections": int(row["connections"] or 0),
        "customers": int(row["customers"] or 0),
        "when": key,
        "kind": window.get("kind"),
        "days_late": window.get("days_late"),
    }


def expiry_window_connections(
    conn: sqlite3.Connection,
    *,
    when: str = "tonight",
    provider: str = "",
    q: str = "",
    limit: int = 500,
    collector_id: int | None = None,
) -> list[sqlite3.Row]:
    key, window = parse_expiry_when(when)
    where, params = _expiry_window_where(window)
    if provider and key != "term_month":
        where += " AND cn.provider = ?"
        params.append(provider)
    if collector_id:
        territory, territory_params = customer_territory_sql(collector_id)
        where += f" AND {territory}"
        params.extend(territory_params)
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        where += (
            " AND (c.name LIKE ? OR c.code LIKE ? OR cn.upstream_id LIKE ? "
            "OR c.phone LIKE ?)"
        )
        params.extend([like, like, like, like])
    kind = window.get("kind")
    if kind == "month":
        order = "cn.expiry_date ASC, c.name COLLATE NOCASE, cn.id"
    elif kind == "from":
        order = (
            f"julianday({CONNECTION_EFFECTIVE_EXPIRY_SQL}) ASC, c.name COLLATE NOCASE, cn.id"
        )
    else:
        order = "cn.provider, c.name COLLATE NOCASE, cn.id"
    params.append(limit)
    return conn.execute(
        "SELECT cn.*, c.name AS customer_name, c.code AS customer_code, c.phone, "
        "       c.area, c.sub_area, c.address, c.lat, c.lng, "
        f"      ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) AS effective_expiry, "
        "       p.name AS package_name, p.price_paise AS package_price_paise, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'railtel' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS railtel_ids, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'hathway' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS hathway_ids, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'iptv' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS iptv_ids, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'ott' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS ott_ids, "
        "       (SELECT GROUP_CONCAT(DISTINCT x.provider) FROM connections x "
        "         WHERE x.customer_id = c.id) AS providers, "
        "       (SELECT j.completed_at FROM upstream_jobs j WHERE j.connection_id = cn.id "
        "        AND j.action = 'status' AND j.status = 'done' ORDER BY j.id DESC LIMIT 1) "
        "       AS last_status_at, "
        "       CASE WHEN EXISTS (SELECT 1 FROM upstream_jobs j "
        "         WHERE j.connection_id = cn.id AND j.action = 'status' "
        "           AND j.status IN ('queued', 'running', 'awaiting_otp')) "
        "         OR EXISTS (SELECT 1 FROM upstream_jobs j "
        "         WHERE j.action = 'status_batch' AND j.provider = cn.provider "
        "           AND j.status IN ('queued', 'running')) "
        "       THEN 1 END AS status_check_pending "
        f"{_expiry_window_joins()}WHERE {where} "
        f"ORDER BY {order} LIMIT ?",
        params,
    ).fetchall()


def connection_verification_status(
    conn,
    connection_ids: list[int],
) -> dict[int, dict[str, object]]:
    """Lightweight status for expiring-list cards (no full page reload)."""
    from .money import fmt_datetime_human

    ids = [int(x) for x in connection_ids if int(x or 0) > 0]
    if not ids:
        return {}

    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT cn.id, cn.provider, cn.last_synced_at, "
        f"(SELECT j.completed_at FROM upstream_jobs j WHERE j.connection_id = cn.id "
        f" AND j.action = 'status' AND j.status = 'done' ORDER BY j.id DESC LIMIT 1) "
        f"AS last_status_at FROM connections cn WHERE cn.id IN ({placeholders})",
        ids,
    ).fetchall()

    pending_ids = {
        int(r["connection_id"])
        for r in conn.execute(
            f"SELECT connection_id FROM upstream_jobs WHERE connection_id IN ({placeholders}) "
            "AND action = 'status' AND status IN ('queued', 'running', 'awaiting_otp')",
            ids,
        ).fetchall()
        if r["connection_id"] is not None
    }
    batch_providers = {
        (r["provider"] or "").strip().lower()
        for r in conn.execute(
            "SELECT provider FROM upstream_jobs WHERE action = 'status_batch' "
            "AND status IN ('queued', 'running')"
        ).fetchall()
    }

    out: dict[int, dict[str, object]] = {}
    for row in rows:
        cid = int(row["id"])
        prov = (row["provider"] or "").strip().lower()
        raw_verified = (row["last_status_at"] or row["last_synced_at"] or "").strip()
        pending = cid in pending_ids or prov in batch_providers
        label = "Checking portal…"
        if not pending:
            label = (
                f"Verified {fmt_datetime_human(raw_verified)}"
                if raw_verified
                else "Not verified yet"
            )
        out[cid] = {
            "pending": pending,
            "verified_at": raw_verified,
            "label": label,
        }
    return out


def _customer_expiry_sql(provider: str) -> str:
    if provider:
        prov = provider.replace("'", "''")
        return (
            f"(SELECT MIN({CONNECTION_EFFECTIVE_EXPIRY_SQL}) FROM connections cn "
            f"LEFT JOIN packages p ON p.id = cn.package_id "
            f"WHERE cn.customer_id = c.id AND cn.provider = '{prov}' "
            f"AND cn.status = 'active')"
        )
    return f"({NEXT_EXPIRY_SQL})"


def _provider_id_maps_sql(*, customer_col: str = "c.id") -> str:
    """Per-provider upstream ids and id:login maps for inline portal actions."""
    parts: list[str] = []
    for prov in ("railtel", "hathway", "iptv", "ott"):
        parts.append(
            f"(SELECT GROUP_CONCAT(cn.upstream_id, ', ') FROM connections cn "
            f"WHERE cn.customer_id = {customer_col} AND cn.provider = '{prov}' "
            f"AND TRIM(COALESCE(cn.upstream_id, '')) != '') AS {prov}_ids"
        )
        parts.append(
            f"(SELECT GROUP_CONCAT(cn.id || ':' || TRIM(cn.upstream_id) || ':' || COALESCE(cn.status, ''), '|') FROM connections cn "
            f"WHERE cn.customer_id = {customer_col} AND cn.provider = '{prov}' "
            f"AND TRIM(COALESCE(cn.upstream_id, '')) != '') AS {prov}_conns"
        )
    return ",\n    ".join(parts)


def _customer_aggregates_sql(*, provider: str = "", collector_id: int | None = None) -> str:
    expiry = _customer_expiry_sql(provider)
    if collector_id:
        collected_sql = (
            "COALESCE((SELECT SUM(p.amount_paise) FROM payments p WHERE p.customer_id = c.id "
            f"AND p.collected_agent_id = {int(collector_id)} "
            "AND lower(COALESCE(p.mode, '')) != 'adjustment'), 0) AS collected_paise"
        )
    else:
        collected_sql = (
            "COALESCE((SELECT SUM(p.amount_paise) FROM payments p WHERE p.customer_id = c.id "
            "AND lower(COALESCE(p.mode, '')) != 'adjustment'), 0) AS collected_paise"
        )
    return f"""
    (SELECT COUNT(*) FROM connections cn WHERE cn.customer_id = c.id) AS connection_count,
    (SELECT COUNT(*) FROM connections cn WHERE cn.customer_id = c.id AND cn.status = 'active') AS active_count,
    (SELECT GROUP_CONCAT(DISTINCT cn.provider) FROM connections cn WHERE cn.customer_id = c.id) AS providers,
    {_provider_id_maps_sql()},
    (SELECT GROUP_CONCAT(DISTINCT CASE
        WHEN COALESCE(cn.portal_account_id, '') IN ('', 'default') THEN 'default'
        ELSE lower(cn.portal_account_id) END)
     FROM connections cn
     WHERE cn.customer_id = c.id AND cn.provider = 'railtel') AS railtel_dealers,
    {expiry} AS next_expiry,
    COALESCE((SELECT SUM(b.total_paise - b.paid_paise) FROM bills b
       WHERE b.customer_id = c.id AND b.status IN ('pending', 'partial')), 0) AS outstanding_paise,
    {collected_sql}
"""


CUSTOMER_AGGREGATES = _customer_aggregates_sql()


# --------------------------------------------------------------------------- #
# Customers
# --------------------------------------------------------------------------- #

NONE_AREA = "__none__"

# CableWay / Hathway-style Tiptur localities — offered when picking an area.
SUGGESTED_AREAS = (
    "Anjeneyaswamy Temple Road",
    "Basanappa compound",
    "BH Road",
    "CB Compound",
    "Changalrayappa beedi",
    "CN Road",
    "Doppete",
    "Heggadigara Beedi",
    "Hospete",
    "Kallapa Garden",
    "KK Compound",
    "Kodi Circle",
    "Kote",
    "Madike asagara Galli",
    "Main Area",
    "MST Road",
    "Panduranga swamy Temple Road",
    "RST Road",
    "Shankar Road",
    "Sub Area",
    "Tiptur",
)


def list_area_options(conn: sqlite3.Connection) -> list[str]:
    """Known localities from customers plus the standard CableWay area names."""
    existing = list_customer_areas(conn)
    seen = {name.lower() for name in existing}
    options = list(existing)
    for name in SUGGESTED_AREAS:
        key = name.lower()
        if key not in seen:
            options.append(name)
            seen.add(key)
    return sorted(options, key=lambda value: value.casefold())


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


def agent_area_options(conn: sqlite3.Connection) -> list[str]:
    """Localities from customers plus any already assigned to agents."""
    rows = conn.execute(
        "SELECT sub_area FROM ("
        "  SELECT MIN(sub_area) AS sub_area FROM customers "
        "  WHERE TRIM(COALESCE(sub_area, '')) != '' "
        "  GROUP BY lower(trim(sub_area))"
        "  UNION "
        "  SELECT MIN(sub_area) AS sub_area FROM agent_areas "
        "  WHERE TRIM(COALESCE(sub_area, '')) != '' "
        "  GROUP BY lower(trim(sub_area))"
        ") ORDER BY sub_area COLLATE NOCASE"
    ).fetchall()
    return [row["sub_area"] for row in rows]


def list_agent_areas(conn: sqlite3.Connection, agent_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT sub_area FROM agent_areas WHERE agent_id = ? "
        "ORDER BY sub_area COLLATE NOCASE",
        (agent_id,),
    ).fetchall()
    return [row["sub_area"] for row in rows]


def list_all_agent_areas(conn: sqlite3.Connection) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT agent_id, sub_area FROM agent_areas ORDER BY agent_id, sub_area COLLATE NOCASE"
    ):
        out.setdefault(int(row["agent_id"]), []).append(row["sub_area"])
    return out


def set_agent_areas(conn: sqlite3.Connection, agent_id: int, areas: list[str]) -> None:
    conn.execute("DELETE FROM agent_areas WHERE agent_id = ?", (agent_id,))
    seen: set[str] = set()
    for raw in areas:
        area = (raw or "").strip()
        if not area:
            continue
        key = area.lower()
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO agent_areas(agent_id, sub_area) VALUES(?, ?)",
            (agent_id, area),
        )


def customer_territory_sql(
    collector_id: int,
    *,
    customer_alias: str = "c",
) -> tuple[str, list]:
    """Collectors see override customers or unassigned customers in their areas.

    Customers with an owner-collected box belong to the owner and are never shown to collectors.
    """
    clause = (
        f"((({customer_alias}.assigned_agent_id = ?) OR "
        f"({customer_alias}.assigned_agent_id IS NULL AND EXISTS ("
        f"  SELECT 1 FROM agent_areas aa WHERE aa.agent_id = ? "
        f"  AND lower(trim(aa.sub_area)) = lower(trim({customer_alias}.sub_area))"
        f"))) AND NOT EXISTS (SELECT 1 FROM connections ocn "
        f"  WHERE ocn.customer_id = {customer_alias}.id AND COALESCE(ocn.owner_reason, '') != ''))"
    )
    return clause, [collector_id, collector_id]


OWNER_CUSTOMER_SQL = (
    "EXISTS (SELECT 1 FROM connections ocn WHERE ocn.customer_id = c.id "
    "AND COALESCE(ocn.owner_reason, '') != '')"
)


def owner_customer_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT customer_id FROM connections WHERE COALESCE(owner_reason, '') != ''"
        )
    }


def without_owner_customers(conn: sqlite3.Connection, rows) -> list:
    """Drop rows (anything with customer_id) that belong to owner-collected customers."""
    hidden = owner_customer_ids(conn)
    if not hidden:
        return list(rows or [])
    return [r for r in rows or [] if int(r["customer_id"] or 0) not in hidden]


# No active collector owns this customer: no live override and no active agent covers the locality.
UNASSIGNED_CUSTOMER_SQL = (
    f"(NOT {OWNER_CUSTOMER_SQL} AND "
    "NOT EXISTS (SELECT 1 FROM agents a WHERE a.id = c.assigned_agent_id AND a.active = 1) "
    " AND NOT EXISTS (SELECT 1 FROM agent_areas aa JOIN agents a ON a.id = aa.agent_id AND a.active = 1 "
    "   WHERE trim(COALESCE(c.sub_area, '')) != '' "
    "   AND lower(trim(aa.sub_area)) = lower(trim(c.sub_area))))"
)


def unassigned_customer_stats(conn: sqlite3.Connection) -> dict:
    """Customers with a live Railtel / Hathway line that no collector covers."""
    out = {}
    for provider in ("hathway", "railtel"):
        out[provider] = int(
            conn.execute(
                "SELECT COUNT(*) FROM customers c WHERE EXISTS ("
                "SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider = ? AND cn.status = 'active') AND "
                + UNASSIGNED_CUSTOMER_SQL,
                (provider,),
            ).fetchone()[0]
            or 0
        )
    return out


def hathway_territory_stats(conn: sqlite3.Connection, collector_id: int) -> dict:
    """Hathway tile numbers limited to one collector's areas and override customers."""
    terr_sql, terr_params = customer_territory_sql(int(collector_id))
    row = conn.execute(
        "SELECT COUNT(*) AS stbs, "
        "SUM(CASE WHEN cn.status = 'active' THEN 1 ELSE 0 END) AS running, "
        "COUNT(DISTINCT cn.customer_id) AS customers, "
        "COUNT(DISTINCT CASE WHEN cn.status = 'active' THEN cn.customer_id END) AS live "
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        f"WHERE cn.provider = 'hathway' AND upper(cn.upstream_id) GLOB ? AND {terr_sql}",
        (HATHWAY_STB_GLOB, *terr_params),
    ).fetchone()
    stbs = int(row["stbs"] or 0)
    running = int(row["running"] or 0)
    return {
        "territory": True,
        "portal_active": 0,
        "portal_inactive": 0,
        "portal_total": 0,
        "checked_at": "",
        "mapped_total": stbs,
        "mapped_active": running,
        "mapped_inactive": stbs - running,
        "live_customers": int(row["live"] or 0),
        "hathway_customers": int(row["customers"] or 0),
        "unmapped_local": 0,
        "portal_gap": 0,
        "extra_local": 0,
        "unmapped": 0,
    }


def customer_visible_to_collector(
    conn: sqlite3.Connection,
    collector_id: int,
    customer,
) -> bool:
    assigned = None
    sub_area = ""
    if customer is not None:
        if isinstance(customer, dict):
            assigned = customer.get("assigned_agent_id")
            sub_area = customer.get("sub_area") or ""
        elif hasattr(customer, "keys"):
            assigned = customer["assigned_agent_id"] if "assigned_agent_id" in customer.keys() else None
            sub_area = (customer["sub_area"] if "sub_area" in customer.keys() else "") or ""
        customer_id = customer.get("id") if isinstance(customer, dict) else (
            customer["id"] if hasattr(customer, "keys") and "id" in customer.keys() else None
        )
        if customer_id and conn.execute(
            "SELECT 1 FROM connections WHERE customer_id = ? AND COALESCE(owner_reason, '') != '' LIMIT 1",
            (int(customer_id),),
        ).fetchone():
            return False
    if assigned:
        return int(assigned) == int(collector_id)
    if not (sub_area or "").strip():
        return False
    row = conn.execute(
        "SELECT 1 FROM agent_areas WHERE agent_id = ? "
        "AND lower(trim(sub_area)) = lower(trim(?)) LIMIT 1",
        (collector_id, sub_area),
    ).fetchone()
    return row is not None


def agent_followup_stats(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Open collect_later bills for customers in this agent's territory."""
    territory, params = customer_territory_sql(agent_id, customer_alias="c")
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(b.total_paise - b.paid_paise), 0) AS due "
        "FROM bills b JOIN customers c ON c.id = b.customer_id "
        f"WHERE b.collect_later = 1 AND b.status IN ('pending', 'partial') AND {territory}",
        params,
    ).fetchone()
    return {
        "count": int(row["n"] or 0),
        "due_paise": int(row["due"] or 0),
    }


def agent_collection_stats(
    conn: sqlite3.Connection,
    agent_id: int,
    *,
    day: str | None = None,
) -> dict:
    """Today/month collections and follow-ups for one collector."""
    day = day or today().strftime("%Y-%m-%d")
    start = f"{day} 00:00:00"
    end = f"{day} 23:59:59"
    month_start = f"{day[:7]}-01 00:00:00"
    pay = conn.execute(
        f"SELECT COALESCE(SUM(amount_paise), 0) AS paise, COUNT(*) AS n FROM payments "
        f"WHERE collected_agent_id = ? AND {NOT_ADJUSTMENT} "
        "AND paid_at >= ? AND paid_at <= ?",
        (agent_id, start, end),
    ).fetchone()
    month = conn.execute(
        f"SELECT COALESCE(SUM(amount_paise), 0) AS paise, COUNT(*) AS n FROM payments "
        f"WHERE collected_agent_id = ? AND {NOT_ADJUSTMENT} "
        "AND paid_at >= ? AND paid_at <= ?",
        (agent_id, month_start, end),
    ).fetchone()
    followup = agent_followup_stats(conn, agent_id)
    return {
        "day": day,
        "collected_today_paise": int(pay["paise"] or 0),
        "payment_count": int(pay["n"] or 0),
        "collected_month_paise": int(month["paise"] or 0),
        "month_count": int(month["n"] or 0),
        "followup_count": followup["count"],
        "followup_due_paise": followup["due_paise"],
    }


def search_customers(
    conn: sqlite3.Connection,
    *,
    query: str = "",
    provider: str = "",
    status: str = "",
    area: str = "",
    view: str = "",
    railtel_account: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sort: str = "",
    late_days: int | None = None,
    export_all: bool = False,
    agent_scope: str = "",
    collector_id: int | None = None,
    hide_owner: bool = False,
) -> dict:
    from .railtel_accounts import normalize_railtel_account_id, railtel_account_filter_sql_for
    from .upstream import PROVIDERS

    where: list[str] = []
    params: list = []
    if hide_owner:
        where.append(f"NOT {OWNER_CUSTOMER_SQL}")
    provider = (provider or "").strip()
    agent_scope = (agent_scope or "").strip().lower()
    if agent_scope not in ("railtel", "hathway"):
        agent_scope = ""
    if agent_scope:
        provider = agent_scope
    if provider and provider not in PROVIDERS:
        provider = ""
    today_str = today().strftime("%Y-%m-%d")

    text = (query or "").strip()
    # Typing a name/login finds that customer. Do not keep them trapped in
    # Expired / Expiring — term-plan monthly cycle dates hid people like Nijaguna.
    if text and view in ("expired", "expiring"):
        view = ""
    if text:
        like = f"%{text}%"
        search_sql, search_params = customer_text_search_clause(
            text,
            extra_or=(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND (cn.upstream_id LIKE ? OR cn.card_number LIKE ?))"
            ),
            extra_params=[like, like],
        )
        where.append(search_sql)
        params.extend(search_params)

    # Expiry tabs with a provider filter use that provider's connection expiry only —
    # not household next_expiry, so bundle customers are not listed when OTT/IPTV lapses first.
    provider_expiry_view = provider and view in ("expired", "expiring")
    if provider and not provider_expiry_view:
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id AND cn.provider = ?)"
        )
        params.append(provider)

    railtel_account = normalize_railtel_account_id(railtel_account)
    if railtel_account:
        acc_sql, acc_params = railtel_account_filter_sql_for(railtel_account)
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            f"AND cn.provider = 'railtel' AND {acc_sql})"
        )
        params.extend(acc_params)

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
                "EXISTS (SELECT 1 FROM connections cn "
                "LEFT JOIN packages p ON p.id = cn.package_id "
                "WHERE cn.customer_id = c.id AND cn.provider = ? AND cn.status = 'active' "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) BETWEEN ? AND ?)"
            )
            params.extend([provider, today_str, limit_date])
        else:
            where.append(f"({NEXT_EXPIRY_SQL}) IS NOT NULL AND ({NEXT_EXPIRY_SQL}) <= ?")
            params.append(limit_date)
    elif view == "expired":
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn "
                "LEFT JOIN packages p ON p.id = cn.package_id "
                "WHERE cn.customer_id = c.id AND cn.provider = ? "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) < ?)"
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
    elif view == "owner":
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider = ? AND COALESCE(cn.owner_reason, '') != '')"
            )
            params.append(provider)
        else:
            where.append(OWNER_CUSTOMER_SQL)
    elif view == "free":
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider = ? AND COALESCE(cn.free_reason, '') != '')"
            )
            params.append(provider)
        else:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND COALESCE(cn.free_reason, '') != '')"
            )
    elif view == "unassigned":
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider = ? AND cn.status = 'active')"
            )
            params.append(provider)
        else:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
                "AND cn.provider IN ('hathway', 'railtel') AND cn.status = 'active')"
            )
        where.append(UNASSIGNED_CUSTOMER_SQL)
    elif view == "terminated_stb":
        term_provider = provider or "hathway"
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = ? AND cn.status = 'terminated')"
        )
        params.append(term_provider)
    elif view in ("inactive", "inactive_stb"):
        inactive_provider = provider or "hathway"
        where.append(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND cn.provider = ? AND cn.status != 'active')"
        )
        params.append(inactive_provider)
    elif view == "faulty":
        where.append(
            "COALESCE((SELECT SUM(b.total_paise - b.paid_paise) FROM bills b "
            "WHERE b.customer_id = c.id AND b.status IN ('pending','partial')), 0) > 0"
        )
        if provider:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn "
                "LEFT JOIN packages p ON p.id = cn.package_id "
                "WHERE cn.customer_id = c.id AND cn.provider = ? "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) < ?)"
            )
            params.extend([provider, today_str])
        else:
            where.append(
                "EXISTS (SELECT 1 FROM connections cn "
                "LEFT JOIN packages p ON p.id = cn.package_id "
                "WHERE cn.customer_id = c.id "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
                f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) < ?)"
            )
            params.append(today_str)
        where.append(
            "NOT EXISTS (SELECT 1 FROM bills b WHERE b.customer_id = c.id "
            "AND b.collect_later = 1 AND b.status IN ('pending', 'partial') "
            "AND COALESCE(b.followup_kind, '') != 'manual' "
            "AND substr(COALESCE(b.created_at, ''), 1, 10) >= ?)"
        )
        params.append(today().replace(day=1).strftime("%Y-%m-%d"))

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
    aggregates = _customer_aggregates_sql(provider=provider, collector_id=collector_id)
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


def get_customer(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    collector_id: int | None = None,
) -> sqlite3.Row | None:
    aggregates = _customer_aggregates_sql(collector_id=collector_id)
    return conn.execute(
        f"SELECT c.*, {aggregates}, a.name AS assigned_agent_name "
        "FROM customers c "
        "LEFT JOIN agents a ON a.id = c.assigned_agent_id "
        "WHERE c.id = ?",
        (customer_id,),
    ).fetchone()


def set_customer_assigned_agent(
    conn: sqlite3.Connection,
    customer_id: int,
    agent_id: int | None,
) -> None:
    conn.execute(
        "UPDATE customers SET assigned_agent_id = ?, updated_at = ? WHERE id = ?",
        (agent_id, now_iso(), customer_id),
    )


def list_customers_for_whatsapp_probe(
    conn: sqlite3.Connection,
    *,
    skip_checked: bool = False,
    limit: int = 0,
) -> list[sqlite3.Row]:
    """Customers with a primary phone, optionally skipping already-checked rows."""
    where = ["phone IS NOT NULL", "TRIM(phone) != ''"]
    if skip_checked:
        where.append("(whatsapp_status IS NULL OR TRIM(whatsapp_status) = '')")
    clause = " AND ".join(where)
    sql = (
        f"SELECT id, name, phone, whatsapp_status FROM customers WHERE {clause} "
        "ORDER BY id"
    )
    params: list = []
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def update_customer_whatsapp_status(
    conn: sqlite3.Connection,
    customer_id: int,
    status: str,
    *,
    checked_at: str | None = None,
) -> None:
    from .money import now_iso

    st = (status or "").strip().lower()
    if st not in ("yes", "no", "unknown", "error"):
        st = "unknown"
    stamp = checked_at or now_iso()
    conn.execute(
        "UPDATE customers SET whatsapp_status = ?, whatsapp_checked_at = ?, updated_at = ? "
        "WHERE id = ?",
        (st, stamp, stamp, int(customer_id)),
    )


def whatsapp_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(whatsapp_status), ''), 'unchecked') AS st, COUNT(*) AS n "
        "FROM customers WHERE phone IS NOT NULL AND TRIM(phone) != '' "
        "GROUP BY st"
    ).fetchall()
    return {str(r["st"]): int(r["n"]) for r in rows}


def sync_customer_account_status(conn: sqlite3.Connection, customer_id: int) -> None:
    """Align customers.status with connections after renew, sync, or status checks.

    Imports often leave customers.status='inactive' while the Railtel connection
    was renewed or synced back to active with a future expiry.
    """
    from .money import now_iso, parse_date, today

    ref = today()
    rows = conn.execute(
        "SELECT status, expiry_date FROM connections WHERE customer_id = ?",
        (customer_id,),
    ).fetchall()
    if not rows:
        return
    live = False
    for row in rows:
        st = (row["status"] or "").lower()
        if st in {"terminated", "suspended"}:
            continue
        exp = parse_date(row["expiry_date"])
        if st == "active" or (exp is not None and exp >= ref):
            live = True
            break
    if live:
        conn.execute(
            "UPDATE customers SET status = 'active', updated_at = ? "
            "WHERE id = ? AND status != 'active'",
            (now_iso(), customer_id),
        )


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
        "        ORDER BY j.id DESC LIMIT 1) AS last_job_action, "
        "       (SELECT j.completed_at FROM upstream_jobs j WHERE j.connection_id = cn.id "
        "        AND j.action = 'status' AND j.status = 'done' "
        "        AND (cn.last_synced_at IS NULL OR j.completed_at >= cn.last_synced_at) "
        "        ORDER BY j.id DESC LIMIT 1) "
        "       AS last_status_at, "
        f"       {CONNECTION_EFFECTIVE_EXPIRY_SQL} AS effective_expiry "
        "FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.customer_id = ? ORDER BY cn.provider, cn.upstream_id",
        (customer_id,),
    ).fetchall()


def customer_bills(conn: sqlite3.Connection, customer_id: int, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bills WHERE customer_id = ? "
        "AND NOT (status = 'cancelled' AND source = 'bix_sync') "
        "ORDER BY id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def customer_payments(
    conn: sqlite3.Connection,
    customer_id: int,
    limit: int = 30,
    *,
    collector_id: int | None = None,
) -> list[sqlite3.Row]:
    if collector_id:
        return conn.execute(
            "SELECT * FROM payments WHERE customer_id = ? AND collected_agent_id = ? "
            "ORDER BY paid_at DESC, id DESC LIMIT ?",
            (customer_id, int(collector_id), limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM payments WHERE customer_id = ? ORDER BY paid_at DESC, id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()


def customer_jobs(conn: sqlite3.Connection, customer_id: int, limit: int = 20) -> list:
    rows = conn.execute(
        f"SELECT j.*, cn.upstream_id, {JOB_CREATOR_COLS} FROM upstream_jobs j "
        f"LEFT JOIN connections cn ON cn.id = j.connection_id "
        f"{JOB_CREATOR_JOIN_SQL} "
        f"WHERE j.customer_id = ? ORDER BY j.id DESC LIMIT ?",
        (customer_id, limit),
    ).fetchall()
    return [_job_row(row) for row in rows]


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
        "SELECT id, receipt_no, amount_paise, mode, reference, paid_at, notes, collected_by "
        "FROM payments WHERE customer_id = ?",
        (customer_id,),
    ):
        mode_key = (row["mode"] or "cash").strip().lower()
        mode_word = {
            "cash": "cash",
            "upi": "UPI",
            "scanner": "scanner",
            "owner_upi": "UPI",
            "bank": "bank",
            "gateway": "card",
            "cheque": "cheque",
        }.get(mode_key, mode_key.replace("_", " "))
        ref = f" ref {row['reference']}" if row["reference"] else ""
        if mode_key == "adjustment":
            description = "Balance reduced"
            if row["notes"]:
                description += f" — {row['notes']}"
        else:
            description = f"Payment by {mode_word}{ref}"
            if row["collected_by"]:
                description += f" · collected by {row['collected_by']}"
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

JOB_CREATOR_JOIN_SQL = """
LEFT JOIN agents created_ag ON created_ag.id = (
  SELECT a2.id FROM agents a2
  WHERE lower(trim(a2.name)) = lower(trim(COALESCE(j.requested_by, '')))
     OR lower(trim(a2.username)) = lower(trim(COALESCE(j.requested_by, '')))
  ORDER BY CASE WHEN a2.role = 'admin' THEN 0 ELSE 1 END, a2.id
  LIMIT 1
)
"""

JOB_CREATOR_COLS = (
    "created_ag.name AS created_agent_name, "
    "created_ag.username AS created_agent_username, "
    "created_ag.role AS created_agent_role"
)


def job_created_by_label(row) -> str:
    """Who queued the job: owner, field agent, or a system/scheduled run."""
    raw = ""
    name = ""
    role = ""
    if row is not None:
        if isinstance(row, dict):
            raw = row.get("requested_by") or ""
            name = row.get("created_agent_name") or ""
            role = row.get("created_agent_role") or ""
        elif hasattr(row, "keys"):
            raw = row["requested_by"] if "requested_by" in row.keys() else ""
            name = row["created_agent_name"] if "created_agent_name" in row.keys() else ""
            role = row["created_agent_role"] if "created_agent_role" in row.keys() else ""
        else:
            raw = str(row or "")
    raw = (raw or "").strip()
    name = (name or "").strip()
    role = (role or "").strip().lower()
    if not raw and not name:
        return "—"
    key = raw.lower()
    if (
        key in {"scheduler", "post-renew term expiry"}
        or key.startswith("expired-")
        or key.startswith("sweep #")
    ):
        return "System"
    display = name or raw
    if role == "admin":
        return f"{display} · Owner"
    if role == "collector":
        return f"{display} · Agent"
    if role:
        return f"{display} · {role.title()}"
    return display


def _job_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["created_by_label"] = job_created_by_label(row)
    return item


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    limit: int = 200,
    provider: str = "",
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []
    if status == "open":
        where.append("j.status IN ('awaiting_confirm', 'awaiting_otp', 'queued', 'running')")
    elif status:
        where.append("j.status = ?")
        params.append(status)
    pf = (provider or "").strip().lower()
    if pf:
        where.append("j.provider = ?")
        params.append(pf)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT j.*, c.name AS customer_name, c.code AS customer_code, cn.upstream_id, "
        f"       cn.card_number, b.bill_no, b.total_paise AS bill_total_paise, "
        f"       {JOB_CREATOR_COLS} "
        f"FROM upstream_jobs j "
        f"LEFT JOIN customers c ON c.id = j.customer_id "
        f"LEFT JOIN connections cn ON cn.id = j.connection_id "
        f"LEFT JOIN bills b ON b.id = j.bill_id "
        f"{JOB_CREATOR_JOIN_SQL} {clause} "
        f"ORDER BY CASE j.status WHEN 'awaiting_otp' THEN 0 WHEN 'awaiting_confirm' THEN 1 "
        f"         WHEN 'running' THEN 2 WHEN 'queued' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END, "
        f"j.id DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [_job_row(row) for row in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    return _job_row(conn.execute(
        f"SELECT j.*, c.name AS customer_name, c.code AS customer_code, cn.upstream_id, "
        f"       {JOB_CREATOR_COLS} "
        f"FROM upstream_jobs j "
        f"LEFT JOIN customers c ON c.id = j.customer_id "
        f"LEFT JOIN connections cn ON cn.id = j.connection_id "
        f"{JOB_CREATOR_JOIN_SQL} WHERE j.id = ?",
        (job_id,),
    ).fetchone())


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
    collector_id: int | None = None,
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
    if collector_id:
        where.append("p.collected_agent_id = ?")
        params.append(int(collector_id))
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
    from .upstream import PROVIDERS, id_problem, provider_label

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
            "label": provider_label(provider),
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


ACTIVITY_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("collect", "Collections", ("payment_%", "public_pay_%", "balance_set", "bill_cancelled")),
    ("collect_later", "Renew, collect later", ("collect_later",)),
    ("renew", "Renewals & portal jobs", ("job_%",)),
    ("inventory", "Inventory", ("inventory_%",)),
    ("settlement", "Settlements", ("agent_settlement",)),
    ("complaint", "Complaints", ("complaint_%",)),
    ("customer", "Customer edits", ("customer_%", "connection_%", "custom_plan", "free_stb",
                                    "owner_collected", "area_set", "plan_edit")),
)


def activity_log_rows(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None = None,
    group: str = "",
    day_from: str = "",
    day_to: str = "",
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Activity rows filtered by agent, kind group and date range, newest first."""
    where: list[str] = []
    params: list = []
    if agent_id:
        where.append("a.agent_id = ?")
        params.append(int(agent_id))
    patterns = next((g[2] for g in ACTIVITY_GROUPS if g[0] == group), ())
    if patterns:
        where.append("(" + " OR ".join("a.kind LIKE ?" for _ in patterns) + ")")
        params.extend(patterns)
    if day_from:
        where.append("a.at >= ?")
        params.append(f"{day_from} 00:00:00")
    if day_to:
        where.append("a.at <= ?")
        params.append(f"{day_to} 23:59:59")
    sql = (
        "SELECT a.*, c.name AS customer_name, ag.name AS agent_name FROM activity_log a "
        "LEFT JOIN customers c ON c.id = a.customer_id "
        "LEFT JOIN agents ag ON ag.id = a.agent_id "
        + ("WHERE " + " AND ".join(where) + " " if where else "")
        + "ORDER BY a.id DESC LIMIT ?"
    )
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def recent_activity(
    conn: sqlite3.Connection,
    limit: int = 40,
    *,
    provider: str = "",
) -> list[sqlite3.Row]:
    pf = (provider or "").strip().lower()
    if pf not in ("railtel", "hathway"):
        return conn.execute(
            "SELECT a.*, c.name AS customer_name FROM activity_log a "
            "LEFT JOIN customers c ON c.id = a.customer_id ORDER BY a.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    other = "hathway" if pf == "railtel" else "railtel"
    return conn.execute(
        "SELECT a.*, c.name AS customer_name FROM activity_log a "
        "LEFT JOIN customers c ON c.id = a.customer_id "
        "LEFT JOIN connections cn ON cn.id = a.connection_id "
        "WHERE ( "
        "  (a.connection_id IS NOT NULL AND cn.provider = ?) "
        "  OR (a.connection_id IS NULL AND a.customer_id IS NULL) "
        "  OR (a.connection_id IS NULL AND a.customer_id IS NOT NULL AND EXISTS ( "
        "        SELECT 1 FROM connections cn2 "
        "        WHERE cn2.customer_id = a.customer_id AND cn2.provider = ? "
        "      ) AND lower(COALESCE(a.message, '')) NOT LIKE ? "
        "     ) "
        ") ORDER BY a.id DESC LIMIT ?",
        (pf, pf, f"%{other}%", limit),
    ).fetchall()


# --------------------------------------------------------------------------- #
# Renewed / enabled, not paid
# --------------------------------------------------------------------------- #

def followup_period_bounds(period: str = "") -> tuple[str, str]:
    """Return (since, until) ISO dates for follow-up filters. Empty = no date limit."""
    key = (period or "").strip().lower()
    if key in {"", "all"}:
        return "", ""
    ref = today()
    if key in {"month", "this_month", "this"}:
        start = ref.replace(day=1)
        return start.strftime("%Y-%m-%d"), ref.strftime("%Y-%m-%d")
    if key in {"last", "last_month"}:
        first_this = ref.replace(day=1)
        last_end = first_this - timedelta(days=1)
        last_start = last_end.replace(day=1)
        return last_start.strftime("%Y-%m-%d"), last_end.strftime("%Y-%m-%d")
    return "", ""


def _followup_where(
    *,
    kind: str = "",
    since: str = "",
    until: str = "",
    provider: str = "",
    collector_id: int | None = None,
) -> tuple[str, list]:
    clauses = ["b.collect_later = 1", "b.status IN ('pending', 'partial')"]
    params: list = []
    if kind == "manual":
        clauses.append("b.followup_kind = 'manual'")
    elif kind == "renew":
        clauses.append("b.followup_kind != 'manual'")
    if since:
        clauses.append("substr(b.created_at, 1, 10) >= ?")
        params.append(since[:10])
    if until:
        clauses.append("substr(b.created_at, 1, 10) <= ?")
        params.append(until[:10])
    if provider:
        clauses.append(
            "(cn.provider = ? OR (b.connection_id IS NULL AND EXISTS "
            "(SELECT 1 FROM connections x WHERE x.customer_id = b.customer_id AND x.provider = ?)))"
        )
        params.extend([provider, provider])
    if collector_id:
        territory, territory_params = customer_territory_sql(int(collector_id))
        clauses.append(territory)
        params.extend(territory_params)
    return " AND ".join(clauses), params


_FOLLOWUP_SELECT = (
    "SELECT b.*, c.name AS customer_name, c.phone, c.code AS customer_code, "
    "       c.area, c.sub_area, c.address, c.lat, c.lng, "
    "       (SELECT GROUP_CONCAT(DISTINCT cn2.provider) FROM connections cn2 "
    "         WHERE cn2.customer_id = c.id) AS providers, "
    "       cn.upstream_id, cn.provider, "
    f"       {_provider_id_maps_sql()} "
    "FROM bills b "
    "JOIN customers c ON c.id = b.customer_id "
    "LEFT JOIN connections cn ON cn.id = b.connection_id "
)


def collect_later_stats(
    conn: sqlite3.Connection,
    *,
    kind: str = "",
    since: str = "",
    until: str = "",
    provider: str = "",
    collector_id: int | None = None,
) -> dict:
    where, params = _followup_where(
        kind=kind, since=since, until=until, provider=provider, collector_id=collector_id
    )
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(b.total_paise - b.paid_paise), 0) AS due, "
        "COUNT(DISTINCT b.customer_id) AS customers, "
        "SUM(CASE WHEN b.followup_kind = 'manual' THEN 1 ELSE 0 END) AS manual_n, "
        "SUM(CASE WHEN b.followup_kind != 'manual' THEN 1 ELSE 0 END) AS renew_n "
        f"FROM bills b "
        "JOIN customers c ON c.id = b.customer_id "
        "LEFT JOIN connections cn ON cn.id = b.connection_id "
        f"WHERE {where}",
        params,
    ).fetchone()
    return {
        "count": int(row["n"] or 0),
        "customers": int(row["customers"] or 0),
        "due_paise": int(row["due"] or 0),
        "manual": int(row["manual_n"] or 0),
        "renew": int(row["renew_n"] or 0),
    }


def list_collect_later(
    conn: sqlite3.Connection,
    *,
    kind: str = "",
    since: str = "",
    until: str = "",
    provider: str = "",
    limit: int = 200,
    collector_id: int | None = None,
) -> list[sqlite3.Row]:
    """Open follow-up bills — newest first. kind is '', 'manual', or 'renew'."""
    where, params = _followup_where(
        kind=kind, since=since, until=until, provider=provider, collector_id=collector_id
    )
    params.append(limit)
    return conn.execute(
        _FOLLOWUP_SELECT
        + f"WHERE {where} "
        "ORDER BY CASE b.followup_kind WHEN 'manual' THEN 0 ELSE 1 END, b.created_at DESC, b.id DESC "
        "LIMIT ?",
        params,
    ).fetchall()


def group_followup_by_customer(rows: list[sqlite3.Row]) -> list[dict]:
    """Roll bill-level follow-up rows into one entry per customer."""
    grouped: dict[int, dict] = {}
    for row in rows:
        cid = int(row["customer_id"])
        due = int(row["total_paise"] or 0) - int(row["paid_paise"] or 0)
        if cid not in grouped:
            grouped[cid] = {
                "customer_id": cid,
                "customer_name": row["customer_name"],
                "customer_code": row["customer_code"],
                "phone": row["phone"],
                "area": row["area"],
                "sub_area": row["sub_area"],
                "address": row["address"],
                "lat": row["lat"],
                "lng": row["lng"],
                "providers": row["providers"],
                "railtel_ids": row["railtel_ids"],
                "hathway_ids": row["hathway_ids"],
                "iptv_ids": row["iptv_ids"],
                "ott_ids": row["ott_ids"],
                "railtel_conns": row["railtel_conns"],
                "hathway_conns": row["hathway_conns"],
                "iptv_conns": row["iptv_conns"],
                "ott_conns": row["ott_conns"],
                "due_paise": 0,
                "bills": [],
                "renewed_at": row["created_at"],
            }
        entry = grouped[cid]
        entry["due_paise"] += due
        entry["bills"].append(row)
        if (row["created_at"] or "") < (entry["renewed_at"] or ""):
            entry["renewed_at"] = row["created_at"]
    out = list(grouped.values())
    out.sort(key=lambda item: item["renewed_at"] or "", reverse=True)
    return out


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
        phone_conds, phone_params = phone_search_or_columns(["c.phone", "c.alt_phone"], text)
        phone_part = "(" + " OR ".join(phone_conds) + ")" if phone_conds else "0"
        where.append(
            f"(c.name LIKE ? OR c.code LIKE ? OR c.sub_area LIKE ? OR {phone_part} "
            "OR cn.upstream_id LIKE ? OR cn.card_number LIKE ?)"
        )
        params.extend([like, like, like])
        params.extend(phone_params)
        params.extend([like, like])

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
        f"SELECT cn.id, cn.upstream_id, cn.card_number, cn.provider, cn.status, cn.expiry_date, "
        f"       cn.upstream_plan_name, cn.hathway_pack_name, cn.last_synced_at, "
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

def field_office_summary(conn: sqlite3.Connection, *, only_agent_id: int | None = None) -> dict:
    from .field import office_summary

    return office_summary(conn, only_agent_id=only_agent_id)


def dashboard_stats(
    conn: sqlite3.Connection,
    *,
    agent_scope: str = "",
    collector_id: int | None = None,
) -> dict:
    now = today()
    today_str = now.strftime("%Y-%m-%d")
    soon_str = add_days(now, settings.expiring_soon_days).strftime("%Y-%m-%d")
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    agent_scope = (agent_scope or "").strip().lower()
    if agent_scope not in ("railtel", "hathway"):
        agent_scope = ""
    pf = agent_scope

    def one(sql: str, params: tuple = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0

    assigned_sql = ""
    assigned_params: list = []
    if collector_id:
        areas = list_agent_areas(conn, int(collector_id))
        if areas:
            assigned_sql, assigned_params = customer_territory_sql(int(collector_id))
            assigned_sql = " AND " + assigned_sql

    cust_where = "WHERE 1=1" + assigned_sql
    cust_params: list = list(assigned_params)
    if pf:
        cust_where += (
            " AND EXISTS (SELECT 1 FROM connections cn "
            "WHERE cn.customer_id = c.id AND cn.provider = ?)"
        )
        cust_params.append(pf)
    customers_total = one(f"SELECT COUNT(*) FROM customers c {cust_where}", tuple(cust_params))

    conn_where = "WHERE 1=1" + assigned_sql
    conn_params: list = list(assigned_params)
    if pf:
        conn_where += " AND cn.provider = ?"
        conn_params.append(pf)
    conn_from = "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
    connections_total = one(f"SELECT COUNT(*) {conn_from}{conn_where}", tuple(conn_params))
    connections_active = one(
        f"SELECT COUNT(*) {conn_from}{conn_where} AND cn.status = 'active'",
        tuple(conn_params),
    )

    by_provider = conn.execute(
        "SELECT cn.provider AS provider, COUNT(*) AS n, "
        "SUM(CASE WHEN cn.status = 'active' THEN 1 ELSE 0 END) AS active_n "
        f"{conn_from}{conn_where} "
        "GROUP BY cn.provider ORDER BY cn.provider",
        tuple(conn_params),
    ).fetchall()

    expiring_sql = (
        "SELECT COUNT(*) FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.status = 'active' "
        f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) BETWEEN ? AND ?"
    )
    expired_sql = (
        "SELECT COUNT(*) FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.status = 'active' "
        f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) < ?"
    )
    expiring_params: list = [today_str, soon_str]
    expired_params: list = [today_str]
    if pf:
        expiring_sql += " AND cn.provider = ?"
        expired_sql += " AND cn.provider = ?"
        expiring_params.append(pf)
        expired_params.append(pf)
    if assigned_sql:
        expiring_sql += " AND EXISTS (SELECT 1 FROM customers c WHERE c.id = cn.customer_id" + assigned_sql + ")"
        expired_sql += " AND EXISTS (SELECT 1 FROM customers c WHERE c.id = cn.customer_id" + assigned_sql + ")"
        expiring_params.extend(assigned_params)
        expired_params.extend(assigned_params)
    expiring = one(expiring_sql, tuple(expiring_params))
    expired = one(expired_sql, tuple(expired_params))

    out_sql = (
        "SELECT COALESCE(SUM(b.total_paise - b.paid_paise), 0), COUNT(*) "
        "FROM bills b JOIN customers c ON c.id = b.customer_id "
        "WHERE b.status IN ('pending', 'partial')"
    )
    out_params: list = []
    if pf:
        out_sql += (
            " AND (EXISTS (SELECT 1 FROM connections cn "
            "WHERE cn.id = b.connection_id AND cn.provider = ?) "
            "OR (b.connection_id IS NULL AND EXISTS ("
            "SELECT 1 FROM connections cn WHERE cn.customer_id = b.customer_id "
            "AND cn.provider = ?)))"
        )
        out_params.extend([pf, pf])
    out_sql += assigned_sql
    out_params.extend(assigned_params)
    out_row = conn.execute(out_sql, tuple(out_params)).fetchone()
    outstanding = int(out_row[0] or 0) if out_row else 0
    open_bills = int(out_row[1] or 0) if out_row else 0

    inactive_provider = pf or "hathway"
    inactive_sql = (
        "SELECT COUNT(DISTINCT cn.customer_id) FROM connections cn "
        "JOIN customers c ON c.id = cn.customer_id "
        "WHERE cn.provider = ? AND cn.status != 'active'"
    ) + assigned_sql
    inactive_customers = one(inactive_sql, tuple([inactive_provider, *assigned_params]))

    faulty_sql = (
        "SELECT COUNT(DISTINCT c.id) FROM customers c WHERE EXISTS ("
        "SELECT 1 FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.customer_id = c.id "
        + ("AND cn.provider = ? " if pf else "")
        + f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        + f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) < ?"
        ") AND COALESCE((SELECT SUM(b.total_paise - b.paid_paise) FROM bills b "
        "WHERE b.customer_id = c.id AND b.status IN ('pending', 'partial')), 0) > 0 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM bills b WHERE b.customer_id = c.id "
        "AND b.collect_later = 1 AND b.status IN ('pending', 'partial') "
        "AND COALESCE(b.followup_kind, '') != 'manual' "
        "AND substr(COALESCE(b.created_at, ''), 1, 10) >= ?"
        ")"
    ) + assigned_sql
    faulty_params: list = []
    if pf:
        faulty_params.append(pf)
    faulty_params.extend([today_str, month_start, *assigned_params])
    faulty_customers = one(faulty_sql, tuple(faulty_params))
    if collector_id:
        cid = int(collector_id)
        collected_today = one(
            f"SELECT COALESCE(SUM(amount_paise), 0) FROM payments "
            f"WHERE collected_agent_id = ? AND substr(paid_at, 1, 10) = ? AND {NOT_ADJUSTMENT}",
            (cid, today_str),
        )
        collected_month = one(
            f"SELECT COALESCE(SUM(amount_paise), 0) FROM payments "
            f"WHERE collected_agent_id = ? AND substr(paid_at, 1, 10) >= ? AND {NOT_ADJUSTMENT}",
            (cid, month_start),
        )
    else:
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
        "SELECT status, COUNT(*) AS n FROM upstream_jobs "
        + ("WHERE provider = ? " if pf else "")
        + "GROUP BY status",
        (pf,) if pf else (),
    ).fetchall()
    job_counts = {row["status"]: int(row["n"]) for row in jobs}

    free_stbs = one(
        f"SELECT COUNT(*) {conn_from}{conn_where} AND COALESCE(cn.free_reason, '') != ''",
        tuple(conn_params),
    )
    owner = {}
    if not collector_id:
        owner = {
            "stbs": one(
                f"SELECT COUNT(*) {conn_from}{conn_where} AND COALESCE(cn.owner_reason, '') != ''",
                tuple(conn_params),
            ),
            "due_paise": one(
                "SELECT COALESCE(SUM(b.total_paise - b.paid_paise), 0) FROM bills b "
                "JOIN customers c ON c.id = b.customer_id "
                f"WHERE b.status IN ('pending', 'partial') AND {OWNER_CUSTOMER_SQL}"
            ),
        }

    return {
        "customers_total": customers_total,
        "connections_total": connections_total,
        "connections_active": connections_active,
        "by_provider": by_provider,
        "expiring": expiring,
        "expired": expired,
        "outstanding_paise": outstanding,
        "open_bills": open_bills,
        "inactive_customers": inactive_customers,
        "faulty_customers": faulty_customers,
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
        "hathway": (
            hathway_territory_stats(conn, int(collector_id))
            if collector_id
            else hathway_mapping_stats(conn)
        ),
        "unassigned": {} if collector_id else unassigned_customer_stats(conn),
        "free_stbs": free_stbs,
        "owner": owner,
        "iptv": {} if pf else iptv_stats(conn),
        "ott": {} if pf else ott_stats(conn),
        "railtel": {} if pf == "hathway" else railtel_stats(conn),
        "followups": collect_later_stats(conn, provider=pf, collector_id=collector_id),
        "unpaid_renewals_month": collect_later_stats(
            conn,
            kind="renew",
            since=month_start,
            until=today_str,
            provider=pf,
            collector_id=collector_id,
        ),
        "expiring_today": expiring_today_stats(conn, provider=pf, collector_id=collector_id),
        "expired_yesterday": expired_yesterday_stats(conn, provider=pf, collector_id=collector_id),
        "term_expiring_month": (
            term_expiring_month_stats(conn)
            if pf != "hathway"
            else {"connections": 0, "customers": 0}
        ),
        "field": field_office_summary(conn, only_agent_id=int(collector_id) if collector_id else None),
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
    use_paid_through = provider == "railtel"
    expiry_sql = CONNECTION_EFFECTIVE_EXPIRY_SQL if use_paid_through else (
        "NULLIF(trim(COALESCE(cn.expiry_date, '')), '')"
    )
    join_sql = (
        "FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        if use_paid_through
        else "FROM connections cn "
    )
    expiring = conn.execute(
        f"SELECT COUNT(*) AS n {join_sql}"
        "WHERE cn.provider = ? AND cn.status = 'active' "
        f"AND ({expiry_sql}) IS NOT NULL "
        f"AND ({expiry_sql}) BETWEEN ? AND ?",
        (provider, today_str, soon_str),
    ).fetchone()["n"]
    expired = conn.execute(
        f"SELECT COUNT(*) AS n {join_sql}"
        "WHERE cn.provider = ? AND cn.status = 'active' "
        f"AND ({expiry_sql}) IS NOT NULL AND ({expiry_sql}) < ?",
        (provider, today_str),
    ).fetchone()["n"]
    packs = conn.execute(
        "SELECT COUNT(*) AS n FROM packages WHERE provider = ? AND active = 1",
        (provider,),
    ).fetchone()["n"]
    live = conn.execute(
        f"SELECT COUNT(*) AS n {join_sql}"
        "WHERE cn.provider = ? AND cn.status = 'active' "
        f"AND (({expiry_sql}) IS NULL OR ({expiry_sql}) >= ?)",
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
    """Expired / 1d late / 2d late from paid-through date (term xpath, else portal expiry)."""
    row = conn.execute(
        "SELECT "
        f"SUM(CASE WHEN ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        f"    AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) < ? THEN 1 ELSE 0 END) AS expired, "
        f"SUM(CASE WHEN ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        f"    AND julianday(?) - julianday({CONNECTION_EFFECTIVE_EXPIRY_SQL}) = 1 "
        f"    THEN 1 ELSE 0 END) AS late_1d, "
        f"SUM(CASE WHEN ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        f"    AND julianday(?) - julianday({CONNECTION_EFFECTIVE_EXPIRY_SQL}) = 2 "
        f"    THEN 1 ELSE 0 END) AS late_2d "
        "FROM connections cn LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.provider = 'railtel'",
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
    """Paid-through expiry KPIs + online list + wallet. Snapshot red is monthly cycle only."""
    data = prepaid_connection_stats(conn, "railtel")
    today_str = today().strftime("%Y-%m-%d")
    expiry = _railtel_expiry_breakdown(conn, today_str)
    data["expired"] = expiry["expired"]
    data["late_1d"] = expiry["late_1d"]
    data["late_2d"] = expiry["late_2d"]
    subs = latest_railtel_subscribers(conn)
    if subs:
        data["portal_expired"] = int(subs["expired_count"] or 0)
        data["portal_expiring_7d"] = int(subs["expiring_7d"] or 0)
        data["portal_active"] = int(subs["active_count"] or 0)
        data["subscribers_at"] = (subs["fetched_at"] or "") or ""
        data["portal_total"] = int(subs["total_count"] or 0)
    else:
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
        phone_conds, phone_params = phone_search_or_columns(["c.phone", "c.alt_phone"], text)
        phone_part = "(" + " OR ".join(phone_conds) + ")" if phone_conds else "0"
        where.append(
            f"(c.name LIKE ? OR c.code LIKE ? OR {phone_part} "
            "OR cn.upstream_id LIKE ? OR COALESCE(p.name, cn.upstream_plan_name, '') LIKE ?)"
        )
        params.extend([like, like])
        params.extend(phone_params)
        params.extend([like, like])

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
                         limit: int = 25, provider: str = "") -> list[sqlite3.Row]:
    days = settings.expiring_soon_days if days is None else days
    horizon = add_days(today(), days).strftime("%Y-%m-%d")
    sql = (
        "SELECT cn.*, c.name AS customer_name, c.code AS customer_code, c.phone, "
        "       c.area, c.sub_area, c.address, c.lat, c.lng, "
        "       p.name AS package_name, p.price_paise AS package_price_paise, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'railtel' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS railtel_ids, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'hathway' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS hathway_ids, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'iptv' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS iptv_ids, "
        "       (SELECT GROUP_CONCAT(x.upstream_id, ', ') FROM connections x "
        "         WHERE x.customer_id = c.id AND x.provider = 'ott' "
        "           AND TRIM(COALESCE(x.upstream_id, '')) != '') AS ott_ids, "
        "       (SELECT GROUP_CONCAT(DISTINCT x.provider) FROM connections x "
        "         WHERE x.customer_id = c.id) AS providers "
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        "LEFT JOIN packages p ON p.id = cn.package_id "
        f"WHERE cn.status = 'active' AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) IS NOT NULL "
        f"AND ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) <= ? "
    )
    params: list = [horizon]
    prov = (provider or "").strip().lower()
    if prov in ("railtel", "hathway", "iptv", "ott"):
        sql += "AND cn.provider = ? "
        params.append(prov)
    sql += f"ORDER BY ({CONNECTION_EFFECTIVE_EXPIRY_SQL}) LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def expiring_today_stats(
    conn: sqlite3.Connection, *, provider: str = "", collector_id: int | None = None
) -> dict:
    stats = expiry_window_stats(conn, when="tonight", provider=provider, collector_id=collector_id)
    return {"connections": stats["connections"], "customers": stats["customers"]}


def expiring_today_connections(
    conn: sqlite3.Connection,
    *,
    provider: str = "",
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Active connections whose paid-through date is today — any provider."""
    return expiry_window_connections(conn, when="tonight", provider=provider, limit=limit)


def expired_yesterday_stats(
    conn: sqlite3.Connection, *, provider: str = "", collector_id: int | None = None
) -> dict:
    stats = expiry_window_stats(conn, when="1d", provider=provider, collector_id=collector_id)
    return {"connections": stats["connections"], "customers": stats["customers"]}


def expired_yesterday_connections(
    conn: sqlite3.Connection,
    *,
    provider: str = "",
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Connections whose paid-through date was yesterday — any provider."""
    return expiry_window_connections(conn, when="1d", provider=provider, limit=limit)


def term_expiring_month_stats(conn: sqlite3.Connection, *, provider: str = "") -> dict:
    stats = expiry_window_stats(conn, when="term_month", provider=provider)
    return {"connections": stats["connections"], "customers": stats["customers"]}


def term_expiring_month_connections(
    conn: sqlite3.Connection,
    *,
    provider: str = "",
    limit: int = 500,
) -> list[sqlite3.Row]:
    """Railtel x-term connections whose portal renewal date falls this calendar month."""
    return expiry_window_connections(conn, when="term_month", provider=provider, limit=limit)


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
