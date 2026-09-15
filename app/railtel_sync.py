"""Apply Railtel My Subscribers portal data to local connections."""
from __future__ import annotations

import sqlite3
from datetime import date

from .money import fmt_date, fmt_date_display, parse_date, today


def railtel_renew_block_reason(
    expiry_date: str | None,
    *,
    on_date: date | None = None,
) -> str:
    """If the stored renewal date is still today or in the future, block top-up."""
    ref = on_date or today()
    exp = parse_date(expiry_date or "")
    if exp is None:
        return ""
    if exp >= ref:
        return (
            f"Railtel is not expired yet — renewal date is {fmt_date_display(exp)}. "
            f"Top-up was not started."
        )
    return ""


def railtel_renew_block_reason_from_conn(conn: sqlite3.Connection, connection_id: int) -> str:
    row = conn.execute(
        "SELECT expiry_date FROM connections WHERE id = ?", (connection_id,)
    ).fetchone()
    if not row:
        return ""
    return railtel_renew_block_reason(row["expiry_date"])


def summarize_subscriber_rows(rows: list[dict], *, on_date=None) -> dict:
    """Count active / expired / late / expiring from portal Renewal Date + red styling."""
    ref = on_date or today()
    active = expired = late_1d = late_2d = expiring_7d = 0
    for row in rows:
        is_red = bool(row.get("is_red"))
        renewal = parse_date(row.get("renewal_date") or row.get("renewal_at") or "")
        if is_red:
            expired += 1
        else:
            active += 1
        if renewal is None:
            continue
        days_late = (ref - renewal).days
        if days_late == 1:
            late_1d += 1
        elif days_late == 2:
            late_2d += 1
        if renewal >= ref and (renewal - ref).days <= 7:
            expiring_7d += 1
    return {
        "active": active,
        "expired": expired,
        "late_1d": late_1d,
        "late_2d": late_2d,
        "expiring_7d": expiring_7d,
        "total": len(rows),
    }


def sync_railtel_subscribers(conn: sqlite3.Connection, rows: list[dict], stamp: str) -> dict:
    """Match portal rows to local Railtel connections and refresh expiry + plan."""
    updated = 0
    matched = 0
    ref = today()
    for row in rows:
        username = str(row.get("username") or "").strip()
        if not username:
            continue
        renewal_at = fmt_date(parse_date(row.get("renewal_date") or row.get("renewal_at") or ""))
        package = str(row.get("package") or row.get("package_name") or "").strip()
        portal_status = str(row.get("status") or "").strip().lower()
        is_red = bool(row.get("is_red"))
        local_status = "active"
        if is_red or portal_status in {"inactive", "expired", "suspended"}:
            local_status = "inactive"

        apply_expiry = renewal_at
        apply_status = local_status
        cur_row = conn.execute(
            "SELECT expiry_date FROM connections "
            "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
            (username,),
        ).fetchone()
        if cur_row:
            local_exp = parse_date(cur_row["expiry_date"])
            portal_exp = parse_date(renewal_at)
            # My Subscribers can lag after a recent renew — never downgrade a newer local date.
            if local_exp and (portal_exp is None or local_exp > portal_exp):
                apply_expiry = fmt_date(local_exp)
                apply_status = "active" if local_exp >= ref else "inactive"

        cur = conn.execute(
            "UPDATE connections SET "
            "expiry_date = CASE WHEN ? != '' THEN ? ELSE expiry_date END, "
            "status = ?, "
            "upstream_plan_name = CASE WHEN ? != '' THEN ? ELSE upstream_plan_name END, "
            "last_synced_at = ?, updated_at = ? "
            "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
            (apply_expiry, apply_expiry, apply_status, package, package, stamp, stamp, username),
        )
        if cur.rowcount:
            matched += 1
            if apply_expiry or package:
                updated += 1
    return {"total": len(rows), "matched": matched, "updated": updated}


def _snapshot_rows_for_summary(conn: sqlite3.Connection, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT renewal_date, renewal_at, is_red, status FROM railtel_subscriber_rows "
        "WHERE snapshot_id = ?",
        (snapshot_id,),
    ).fetchall()
    return [
        {
            "renewal_date": row["renewal_date"],
            "renewal_at": row["renewal_at"],
            "is_red": bool(row["is_red"]),
            "status": row["status"],
        }
        for row in rows
    ]


def _refresh_subscriber_snapshot_counts(conn: sqlite3.Connection, snapshot_id: int) -> None:
    summary = summarize_subscriber_rows(_snapshot_rows_for_summary(conn, snapshot_id))
    conn.execute(
        "UPDATE railtel_subscriber_snapshots SET "
        "active_count = ?, expired_count = ?, late_1d = ?, late_2d = ?, expiring_7d = ? "
        "WHERE id = ?",
        (
            summary["active"],
            summary["expired"],
            summary["late_1d"],
            summary["late_2d"],
            summary["expiring_7d"],
            snapshot_id,
        ),
    )


def patch_railtel_subscriber_after_renew(
    conn: sqlite3.Connection,
    username: str,
    expiry_date: str,
    *,
    package: str = "",
) -> bool:
    """Update the latest My Subscribers snapshot row after a successful renew."""
    username = (username or "").strip()
    exp = parse_date(expiry_date or "")
    if not username or exp is None:
        return False
    snap = conn.execute(
        "SELECT id FROM railtel_subscriber_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not snap:
        return False
    snap_id = int(snap["id"])
    row = conn.execute(
        "SELECT id FROM railtel_subscriber_rows "
        "WHERE snapshot_id = ? AND lower(username) = lower(?)",
        (snap_id, username),
    ).fetchone()
    if not row:
        return False
    ref = today()
    is_red = 0 if exp >= ref else 1
    renewal_display = fmt_date_display(exp)
    conn.execute(
        "UPDATE railtel_subscriber_rows SET "
        "renewal_date = ?, renewal_at = ?, is_red = ?, status = ?, "
        "package_name = CASE WHEN ? != '' THEN ? ELSE package_name END "
        "WHERE id = ?",
        (
            renewal_display,
            fmt_date(exp),
            is_red,
            "active" if not is_red else "inactive",
            package,
            package,
            row["id"],
        ),
    )
    _refresh_subscriber_snapshot_counts(conn, snap_id)
    return True
