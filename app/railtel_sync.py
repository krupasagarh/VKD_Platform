"""Apply Railtel My Subscribers portal data to local connections."""
from __future__ import annotations

import re
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
    from . import repo

    row = conn.execute(
        "SELECT expiry_date, subscription_expiry FROM connections WHERE id = ?",
        (connection_id,),
    ).fetchone()
    if not row:
        return ""
    if repo.connection_is_railtel_term(conn, int(connection_id)):
        return railtel_renew_block_reason(row["subscription_expiry"])
    return railtel_renew_block_reason(row["expiry_date"])


def _list_renewal_date(row: dict) -> str:
    """My Subscribers renewal / monthly cycle date."""
    return fmt_date(parse_date(row.get("renewal_date") or row.get("renewal_at") or ""))


def _profile_subscription_expiry(row: dict) -> str:
    """Subscriber Details xpath tr[4] actual term end, if scraped."""
    d = parse_date(row.get("subscription_expiry") or "")
    # Railtel prints 01/01/70 when no term pack is active.
    return fmt_date(d) if d is not None and d.year >= 2000 else ""


def summarize_subscriber_rows(rows: list[dict], *, on_date=None) -> dict:
    """Count active / expired / late / expiring from portal expiry + red styling."""
    ref = on_date or today()
    active = expired = late_1d = late_2d = expiring_7d = 0
    for row in rows:
        is_red = bool(row.get("is_red"))
        sub_exp = parse_date(row.get("subscription_expiry") or "")
        renewal = parse_date(row.get("renewal_date") or row.get("renewal_at") or "")
        list_date = sub_exp or renewal
        if sub_exp is not None:
            is_red = sub_exp < ref
        elif is_red:
            pass
        if is_red:
            expired += 1
        else:
            active += 1
        if list_date is None:
            continue
        days_late = (ref - list_date).days
        if days_late == 1:
            late_1d += 1
        elif days_late == 2:
            late_2d += 1
        if list_date >= ref and (list_date - ref).days <= 7:
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
        renewal_at = _list_renewal_date(row)
        sub_exp = parse_date(row.get("subscription_expiry") or "")
        list_renewal = parse_date(row.get("renewal_date") or row.get("renewal_at") or "")
        package = str(row.get("package") or row.get("package_name") or "").strip()
        portal_status = str(row.get("status") or "").strip().lower()
        is_red = bool(row.get("is_red"))

        apply_expiry = renewal_at
        if sub_exp is not None:
            apply_status = "active" if sub_exp >= ref else "inactive"
        elif is_red or portal_status in {"inactive", "expired", "suspended"}:
            apply_status = "inactive"
        elif list_renewal and list_renewal >= ref:
            apply_status = "active"
        else:
            apply_status = "active"
        cur_row = conn.execute(
            "SELECT expiry_date FROM connections "
            "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
            (username,),
        ).fetchone()
        if cur_row:
            local_exp = parse_date(cur_row["expiry_date"])
            portal_exp = parse_date(apply_expiry)
            # My Subscribers can lag after a recent renew — never downgrade a newer local date.
            if local_exp and (portal_exp is None or local_exp > portal_exp):
                apply_expiry = fmt_date(local_exp)
                if sub_exp is None:
                    apply_status = "active" if local_exp >= ref else "inactive"
                elif sub_exp >= ref:
                    apply_status = "active"

        sub_iso = _profile_subscription_expiry(row)
        exp_iso = apply_expiry or ""

        cur = conn.execute(
            "UPDATE connections SET "
            "subscription_expiry = CASE WHEN ? != '' THEN ? ELSE subscription_expiry END, "
            "expiry_date = CASE WHEN ? != '' THEN ? ELSE expiry_date END, "
            "status = ?, "
            "upstream_plan_name = CASE WHEN ? != '' THEN ? ELSE upstream_plan_name END, "
            "link_state = CASE WHEN ? = 'inactive' THEN '' ELSE link_state END, "
            "link_since = CASE WHEN ? = 'inactive' THEN '' ELSE link_since END, "
            "link_days = CASE WHEN ? = 'inactive' THEN NULL ELSE link_days END, "
            "last_synced_at = ?, updated_at = ? "
            "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
            (
                sub_iso,
                sub_iso,
                exp_iso,
                exp_iso,
                apply_status,
                package,
                package,
                apply_status,
                apply_status,
                apply_status,
                stamp,
                stamp,
                username,
            ),
        )
        if cur.rowcount:
            matched += 1
            if apply_expiry or package:
                updated += 1
            if apply_status == "active":
                cid = conn.execute(
                    "SELECT customer_id FROM connections "
                    "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
                    (username,),
                ).fetchone()
                if cid:
                    from . import repo

                    repo.sync_customer_account_status(conn, int(cid["customer_id"]))
    return {"total": len(rows), "matched": matched, "updated": updated}


def _norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[-10:]
    return digits if len(digits) == 10 else ""


def _portal_row_fields(row: dict, *, on_date: date | None = None) -> dict | None:
    username = str(row.get("username") or "").strip()
    if not username:
        return None
    ref = on_date or today()
    renewal_at = _list_renewal_date(row)
    sub_exp = parse_date(row.get("subscription_expiry") or "")
    list_renewal = parse_date(row.get("renewal_date") or row.get("renewal_at") or "")
    package = str(row.get("package") or row.get("package_name") or "").strip()
    portal_status = str(row.get("status") or "").strip().lower()
    is_red = bool(row.get("is_red"))

    apply_expiry = renewal_at
    if sub_exp is not None:
        apply_status = "active" if sub_exp >= ref else "inactive"
    elif is_red or portal_status in {"inactive", "expired", "suspended"}:
        apply_status = "inactive"
    elif list_renewal and list_renewal >= ref:
        apply_status = "active"
    else:
        apply_status = "active"

    return {
        "username": username,
        "apply_expiry": apply_expiry,
        "subscription_expiry": _profile_subscription_expiry(row),
        "apply_status": apply_status,
        "package": package,
        "name": str(row.get("name") or "").strip(),
        "mobile": _norm_phone(str(row.get("mobile") or "")),
        "email": str(row.get("email") or "").strip(),
        "address": str(row.get("address") or "").strip(),
        "subscriber_id": str(row.get("subscriber_id") or "").strip(),
    }


def import_portal_subscribers(
    conn: sqlite3.Connection,
    rows: list[dict],
    stamp: str,
    *,
    portal_account_id: str,
) -> dict:
    """Create or update local customers/connections from a My Subscribers scrape."""
    from . import repo

    portal_account_id = (portal_account_id or "").strip()
    created_customers = 0
    created_connections = 0
    updated = 0
    matched = 0
    skipped = 0

    for row in rows:
        fields = _portal_row_fields(row)
        if not fields:
            skipped += 1
            continue

        username = fields["username"]
        existing = conn.execute(
            "SELECT id, customer_id, portal_account_id, expiry_date FROM connections "
            "WHERE provider = 'railtel' AND lower(upstream_id) = lower(?)",
            (username,),
        ).fetchone()

        if existing:
            matched += 1
            local_exp = parse_date(existing["expiry_date"])
            portal_exp = parse_date(fields["apply_expiry"])
            apply_expiry = fields["apply_expiry"]
            apply_status = fields["apply_status"]
            if local_exp and (portal_exp is None or local_exp > portal_exp):
                apply_expiry = fmt_date(local_exp)
                apply_status = "active" if local_exp >= today() else "inactive"

            conn.execute(
                "UPDATE connections SET "
                "expiry_date = CASE WHEN ? != '' THEN ? ELSE expiry_date END, "
                "subscription_expiry = CASE WHEN ? != '' THEN ? ELSE subscription_expiry END, "
                "status = ?, "
                "upstream_plan_name = CASE WHEN ? != '' THEN ? ELSE upstream_plan_name END, "
                "portal_account_id = CASE WHEN ? != '' THEN ? ELSE portal_account_id END, "
                "link_state = CASE WHEN ? = 'inactive' THEN '' ELSE link_state END, "
                "link_since = CASE WHEN ? = 'inactive' THEN '' ELSE link_since END, "
                "link_days = CASE WHEN ? = 'inactive' THEN NULL ELSE link_days END, "
                "last_synced_at = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    apply_expiry,
                    apply_expiry,
                    fields.get("subscription_expiry") or "",
                    fields.get("subscription_expiry") or "",
                    apply_status,
                    fields["package"],
                    fields["package"],
                    portal_account_id,
                    portal_account_id,
                    apply_status,
                    apply_status,
                    apply_status,
                    stamp,
                    stamp,
                    existing["id"],
                ),
            )
            if apply_expiry or fields["package"]:
                updated += 1
            repo.sync_customer_account_status(conn, int(existing["customer_id"]))
            continue

        customer_id = None
        phone = fields["mobile"]
        if phone:
            hit = conn.execute(
                "SELECT id FROM customers WHERE phone = ? OR alt_phone = ? LIMIT 1",
                (phone, phone),
            ).fetchone()
            if hit:
                customer_id = int(hit["id"])

        if customer_id is None:
            display_name = fields["name"] or username
            cursor = conn.execute(
                "INSERT INTO customers(code, name, phone, alt_phone, email, address, area, "
                "pincode, status, notes, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, 'Tiptur', '572201', 'active', ?, ?, ?)",
                (
                    username,
                    display_name,
                    phone,
                    phone,
                    fields["email"],
                    fields["address"],
                    f"Imported from Railtel ({portal_account_id})",
                    stamp,
                    stamp,
                ),
            )
            customer_id = int(cursor.lastrowid)
            created_customers += 1
        else:
            if fields["name"]:
                conn.execute(
                    "UPDATE customers SET name = CASE WHEN trim(name) = '' THEN ? ELSE name END, "
                    "address = CASE WHEN ? != '' AND trim(COALESCE(address, '')) = '' THEN ? ELSE address END, "
                    "updated_at = ? WHERE id = ?",
                    (fields["name"], fields["address"], fields["address"], stamp, customer_id),
                )

        note_bits = [f"Railtel subscriber {fields['subscriber_id']}".strip()]
        if portal_account_id:
            note_bits.append(f"dealer {portal_account_id}")
        conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, billing_type, "
            "amount_paise, expiry_date, subscription_expiry, upstream_plan_name, portal_account_id, notes, "
            "last_synced_at, created_at, updated_at) "
            "VALUES(?, 'railtel', ?, ?, 'prepaid', 0, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                username,
                fields["apply_status"],
                fields["apply_expiry"] or None,
                fields.get("subscription_expiry") or None,
                fields["package"],
                portal_account_id,
                " — ".join(x for x in note_bits if x),
                stamp,
                stamp,
                stamp,
            ),
        )
        created_connections += 1
        repo.sync_customer_account_status(conn, customer_id)

    return {
        "total": len(rows),
        "matched": matched,
        "updated": updated,
        "created_customers": created_customers,
        "created_connections": created_connections,
        "skipped": skipped,
    }


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
