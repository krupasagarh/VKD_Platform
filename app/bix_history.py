"""Bix Balance History as a read-only archive inside this app.

The file `vk_digital_history.db` is a copy of Bix's per-customer history page.
Monthly bills and collections sit in one table with a running Bix balance.
We copy that here so a collector can see old payments. Those rows are never
posted as bills or payments on the platform ledger — net due stays this app's
own books (plus a Bix due-file apply).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from .money import now_iso, to_paise

_PHONE_DIGITS = re.compile(r"\D")


def norm_phone(value: str) -> str:
    digits = _PHONE_DIGITS.sub("", value or "")
    return digits[-10:] if len(digits) >= 10 else ""


def classify_label(label: str) -> str:
    text = (label or "").strip().lower()
    if text.startswith("payment") or text.startswith("online payment"):
        return "payment"
    if text.startswith("bill from") or text.startswith("initial bill"):
        return "bill"
    if "adjust" in text:
        return "adjustment"
    return "other"


def default_archive_path() -> Path:
    from .config import settings

    return settings.bix_history_db


def archive_peek(path: Path | None = None) -> dict:
    """Read-only summary of the extract file, if it is on disk."""
    path = Path(path) if path else default_archive_path()
    info = {
        "path": str(path),
        "exists": path.is_file(),
        "customers": 0,
        "txns": 0,
        "first_at": "",
        "last_at": "",
    }
    if not path.is_file():
        return info
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "balance_history" not in tables:
            return info
        info["txns"] = src.execute("SELECT COUNT(*) FROM balance_history").fetchone()[0]
        bounds = src.execute(
            "SELECT MIN(date) AS first_at, MAX(date) AS last_at FROM balance_history"
        ).fetchone()
        info["first_at"] = bounds["first_at"] or ""
        info["last_at"] = bounds["last_at"] or ""
        if "customers" in tables:
            info["customers"] = src.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        else:
            info["customers"] = src.execute(
                "SELECT COUNT(DISTINCT customer_id) FROM balance_history"
            ).fetchone()[0]
    finally:
        src.close()
    return info


def imported_stats(conn: sqlite3.Connection) -> dict:
    customers = conn.execute("SELECT COUNT(*) AS n FROM bix_history_customers").fetchone()["n"]
    matched = conn.execute(
        "SELECT COUNT(*) AS n FROM bix_history_customers WHERE platform_customer_id IS NOT NULL"
    ).fetchone()["n"]
    txns = conn.execute("SELECT COUNT(*) AS n FROM bix_history_txns").fetchone()["n"]
    payments = conn.execute(
        "SELECT COUNT(*) AS n FROM bix_history_txns WHERE kind = 'payment'"
    ).fetchone()["n"]
    bounds = conn.execute(
        "SELECT MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at FROM bix_history_txns"
    ).fetchone()
    latest = conn.execute(
        "SELECT * FROM bix_history_imports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "customers": customers,
        "matched": matched,
        "unmatched": customers - matched,
        "txns": txns,
        "payments": payments,
        "first_at": bounds["first_at"] or "",
        "last_at": bounds["last_at"] or "",
        "latest_import": latest,
    }


def customer_history(conn: sqlite3.Connection, customer_id: int, *, kind: str = "", limit: int = 400) -> dict:
    link = conn.execute(
        "SELECT * FROM bix_history_customers WHERE platform_customer_id = ? "
        "ORDER BY last_txn_at DESC",
        (customer_id,),
    ).fetchall()
    if not link:
        return {"accounts": [], "rows": [], "kind": kind}
    ids = [row["bix_customer_id"] for row in link]
    placeholders = ",".join("?" * len(ids))
    sql = (
        f"SELECT * FROM bix_history_txns WHERE bix_customer_id IN ({placeholders})"
    )
    args: list = list(ids)
    if kind in {"payment", "bill", "adjustment", "other"}:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    return {"accounts": link, "rows": rows, "kind": kind}


def list_archive_payments(
    conn: sqlite3.Connection,
    *,
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where = ["t.kind = 'payment'"]
    args: list = []
    if date_from:
        where.append("t.occurred_at >= ?")
        args.append(date_from)
    if date_to:
        where.append("t.occurred_at < ?")
        # Inclusive calendar day: next midnight is handled by appending time if needed.
        args.append(date_to if " " in date_to else f"{date_to} 23:59:59")
    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(t.amount_paise), 0) AS total_paise "
        f"FROM bix_history_txns t WHERE {clause}",
        args,
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT t.*, c.name AS bix_name, c.phone AS bix_phone,
               c.platform_customer_id, p.name AS customer_name, p.code AS customer_code
        FROM bix_history_txns t
        JOIN bix_history_customers c ON c.bix_customer_id = t.bix_customer_id
        LEFT JOIN customers p ON p.id = c.platform_customer_id
        WHERE {clause}
        ORDER BY t.occurred_at DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
        [*args, limit, offset],
    ).fetchall()
    return {
        "rows": rows,
        "count": total["n"],
        "total_paise": total["total_paise"],
    }


def unmatched_customers(conn: sqlite3.Connection, limit: int = 40) -> list:
    return conn.execute(
        "SELECT * FROM bix_history_customers WHERE platform_customer_id IS NULL "
        "ORDER BY name LIMIT ?",
        (limit,),
    ).fetchall()


def rematch(conn: sqlite3.Connection) -> dict:
    """Link archive customers to platform customers by unique 10-digit phone."""
    from . import bix_sync

    phone_to_platform: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, phone, alt_phone FROM customers"):
        if not bix_sync._hathway_ok(conn, int(row["id"])):
            continue
        for raw in (row["phone"], row["alt_phone"]):
            phone = norm_phone(raw or "")
            if phone:
                phone_to_platform.setdefault(phone, [])
                if int(row["id"]) not in phone_to_platform[phone]:
                    phone_to_platform[phone].append(int(row["id"]))

    phone_to_bix: dict[str, list[str]] = {}
    for row in conn.execute("SELECT bix_customer_id, phone FROM bix_history_customers"):
        phone = norm_phone(row["phone"] or "")
        if phone:
            phone_to_bix.setdefault(phone, []).append(row["bix_customer_id"])

    matched = 0
    ambiguous = 0
    conn.execute("UPDATE bix_history_customers SET platform_customer_id = NULL, match_method = ''")
    for phone, bix_ids in phone_to_bix.items():
        platform_ids = phone_to_platform.get(phone) or []
        if len(bix_ids) != 1 or len(platform_ids) != 1:
            ambiguous += 1
            continue
        conn.execute(
            "UPDATE bix_history_customers SET platform_customer_id = ?, match_method = 'phone' "
            "WHERE bix_customer_id = ?",
            (platform_ids[0], bix_ids[0]),
        )
        matched += 1
    return {"matched": matched, "ambiguous": ambiguous}


def import_archive(conn: sqlite3.Connection, path: Path, *, actor: str = "") -> dict:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Bix history file not found: {path}")

    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "balance_history" not in tables:
            raise ValueError("That file has no balance_history table.")
        hist_cols = {row[1] for row in src.execute("PRAGMA table_info(balance_history)")}
        needed = {"customer_id", "date", "bill_name", "txn_amount", "balance"}
        missing = needed - hist_cols
        if missing:
            raise ValueError(f"balance_history is missing columns: {', '.join(sorted(missing))}")
        has_txn_id = "txn_id" in hist_cols

        customers_seen = _upsert_archive_customers(conn, src, tables)
        txns_seen, txns_new, touched = _copy_txns(conn, src, has_txn_id=has_txn_id)
    finally:
        src.close()

    _refresh_lasts(conn, touched)
    match = rematch(conn)
    unmatched = conn.execute(
        "SELECT COUNT(*) AS n FROM bix_history_customers WHERE platform_customer_id IS NULL"
    ).fetchone()["n"]
    stamp = now_iso()
    conn.execute(
        "INSERT INTO bix_history_imports("
        "source, imported_by, imported_at, customers_seen, txns_seen, txns_new, matched, unmatched) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(path),
            actor,
            stamp,
            customers_seen,
            txns_seen,
            txns_new,
            match["matched"],
            unmatched,
        ),
    )
    return {
        "customers_seen": customers_seen,
        "txns_seen": txns_seen,
        "txns_new": txns_new,
        "matched": match["matched"],
        "unmatched": unmatched,
        "ambiguous": match["ambiguous"],
    }


def _upsert_archive_customers(conn: sqlite3.Connection, src: sqlite3.Connection, tables: set[str]) -> int:
    stamp = now_iso()
    if "customers" in tables:
        rows = src.execute(
            "SELECT customer_id, customer_name, phone, status FROM customers"
        ).fetchall()
        payloads = [
            (
                str(row["customer_id"] or "").strip(),
                (row["customer_name"] or "").strip(),
                (row["phone"] or "").strip(),
                (row["status"] or "").strip(),
                stamp,
            )
            for row in rows
            if str(row["customer_id"] or "").strip()
        ]
    else:
        rows = src.execute(
            "SELECT customer_id, MAX(customer_name) AS customer_name "
            "FROM balance_history GROUP BY customer_id"
            if "customer_name" in {r[1] for r in src.execute("PRAGMA table_info(balance_history)")}
            else "SELECT DISTINCT customer_id FROM balance_history"
        ).fetchall()
        payloads = []
        for row in rows:
            bid = str(row["customer_id"] or "").strip()
            if not bid:
                continue
            name = (row["customer_name"] if "customer_name" in row.keys() else "") or ""
            payloads.append((bid, name.strip(), "", "", stamp))

    conn.executemany(
        """
        INSERT INTO bix_history_customers(
            bix_customer_id, name, phone, status, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(bix_customer_id) DO UPDATE SET
            name = excluded.name,
            phone = excluded.phone,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        payloads,
    )
    return len(payloads)


def _copy_txns(conn: sqlite3.Connection, src: sqlite3.Connection, *, has_txn_id: bool) -> tuple[int, int, set[str]]:
    select = "SELECT customer_id, date, bill_name, txn_amount, balance"
    if has_txn_id:
        select += ", txn_id"
    select += " FROM balance_history"
    seen = 0
    before_count = conn.execute("SELECT COUNT(*) AS n FROM bix_history_txns").fetchone()["n"]
    touched: set[str] = set()
    batch: list[tuple] = []

    def flush() -> None:
        if not batch:
            return
        conn.executemany(
            """
            INSERT OR IGNORE INTO bix_history_txns(
                row_key, bix_customer_id, occurred_at, label, kind, amount_paise, balance_paise)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
        batch.clear()

    for row in src.execute(select):
        bid = str(row["customer_id"] or "").strip()
        if not bid:
            continue
        label = (row["bill_name"] or "").strip() or "Bix entry"
        occurred = (row["date"] or "").strip()
        amount_paise = to_paise(row["txn_amount"])
        balance_paise = to_paise(row["balance"])
        if has_txn_id and row["txn_id"]:
            row_key = f"txn:{row['txn_id']}"
        else:
            raw = f"{bid}|{occurred}|{label}|{amount_paise}|{balance_paise}"
            row_key = hashlib.sha256(raw.encode()).hexdigest()
        batch.append(
            (
                row_key,
                bid,
                occurred,
                label,
                classify_label(label),
                amount_paise,
                balance_paise,
            )
        )
        touched.add(bid)
        seen += 1
        if len(batch) >= 500:
            flush()
    flush()
    after_count = conn.execute("SELECT COUNT(*) AS n FROM bix_history_txns").fetchone()["n"]
    return seen, after_count - before_count, touched


def _refresh_lasts(conn: sqlite3.Connection, bix_ids: set[str]) -> None:
    stamp = now_iso()
    for bid in bix_ids:
        row = conn.execute(
            "SELECT occurred_at, balance_paise FROM bix_history_txns "
            "WHERE bix_customer_id = ? ORDER BY occurred_at DESC, id DESC LIMIT 1",
            (bid,),
        ).fetchone()
        if row is None:
            continue
        conn.execute(
            "UPDATE bix_history_customers SET last_balance_paise = ?, last_txn_at = ?, updated_at = ? "
            "WHERE bix_customer_id = ?",
            (row["balance_paise"], row["occurred_at"], stamp, bid),
        )
