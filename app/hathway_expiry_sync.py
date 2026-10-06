"""Align Hathway STB expiry and staff-only pack from a PlanExpiry export.

The file is the Hathway PlanExpiry report (TSV saved as .xls, or CSV). Each
box may have HSP plus Al-la-carte rows. Only the main bouquet (HSP, then Basic,
then Addon) is kept. Al-la-carte is ignored.

Matched connections get expiry_date and hathway_pack_name updated. package_id
and billed plans stay on the Bix catalog — this pack is staff-only and is not
written onto bills or customer messages. Unmatched boxes are left out; nothing
is created.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .bix_sync import read_bix_file
from .db import log_activity
from .money import days_until, fmt_date, now_iso, parse_date

N_RE = re.compile(r"^N\d{11}$", re.I)
T_RE = re.compile(r"^T\d{12}$", re.I)
_ALACARTE = re.compile(r"la[\s\-]*carte|alacarte", re.I)

COLUMN_ALIASES = {
    "stb_n": ("stb no", "stb number", "stb id", "old stb no", "settop box"),
    "stb_t": ("new stb no", "new stb number", "new stb id"),
    "vc": ("vc id", "vc number", "vc"),
    "package": ("package", "bouquet", "pack"),
    "plan_type": ("plan type", "plantype"),
    "end_date": ("end date", "expiry", "valid upto", "valid until", "plan expiry"),
    "name": ("customer name", "name"),
}

_PLAN_RANK = {
    "hsp": 0,
    "basic": 1,
    "addon": 2,
}


def _clean(value) -> str:
    return str(value or "").strip().strip("'").strip('"').strip()


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower()).rstrip(".")


def _norm_id(value: str) -> str:
    return re.sub(r"[\s'\"]+", "", (value or "")).upper()


def _is_alacarte(plan_type: str, package: str = "") -> bool:
    return bool(_ALACARTE.search(plan_type or "") or _ALACARTE.search(package or ""))


def _plan_rank(plan_type: str) -> int:
    key = re.sub(r"[\s\-]+", "", (plan_type or "").strip().lower())
    return _PLAN_RANK.get(key, 9)


def _map_headers(headers: list[str]) -> dict[str, str]:
    normalized = {_norm_header(h): h for h in headers}
    mapping: dict[str, str] = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            src = normalized.get(_norm_header(alias))
            if src:
                mapping[target] = src
                break
    return mapping


def _pick_ids(raw_n: str, raw_t: str, raw_vc: str) -> tuple[str, str]:
    n = t = ""
    for value in (_norm_id(raw_n), _norm_id(raw_t), _norm_id(raw_vc)):
        if not value:
            continue
        if not n and N_RE.match(value):
            n = value
        elif not t and T_RE.match(value):
            t = value
    return n, t


def parse_plan_expiry_file(path: Path) -> list[dict]:
    """One row per STB, main pack only."""
    headers, raw_rows = read_bix_file(path)
    mapping = _map_headers(headers)
    if "end_date" not in mapping or "package" not in mapping:
        raise ValueError(
            "This file does not look like a Hathway PlanExpiry report. "
            "It needs Package and End Date columns, plus STB No / New STB No."
        )
    if "stb_n" not in mapping and "stb_t" not in mapping and "vc" not in mapping:
        raise ValueError(
            "Could not find STB No or New STB No in that file. "
            f"Headers: {', '.join(headers)}"
        )

    grouped: dict[str, dict] = {}
    skipped_alacarte = 0
    for raw in raw_rows:
        get = lambda key: _clean(raw.get(mapping[key], "")) if key in mapping else ""
        n, t = _pick_ids(get("stb_n"), get("stb_t"), get("vc"))
        if not n and not t:
            continue
        package = get("package")
        plan_type = get("plan_type")
        if _is_alacarte(plan_type, package):
            skipped_alacarte += 1
            key = n or t
            current = grouped.get(key)
            if current is not None:
                current["alacarte_ignored"] = int(current.get("alacarte_ignored") or 0) + 1
            continue
        parsed = parse_date(get("end_date"))
        end_date = fmt_date(parsed) if parsed else ""
        item = {
            "stb_n": n,
            "stb_t": t,
            "name": get("name"),
            "package": package,
            "plan_type": plan_type or "HSP",
            "end_date": end_date,
            "alacarte_ignored": 0,
        }
        key = n or t
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = item
            continue
        if t and not existing["stb_t"]:
            existing["stb_t"] = t
        if n and not existing["stb_n"]:
            existing["stb_n"] = n
        if _plan_rank(item["plan_type"]) < _plan_rank(existing["plan_type"]):
            existing["package"] = item["package"]
            existing["plan_type"] = item["plan_type"]
            existing["end_date"] = item["end_date"] or existing["end_date"]
        elif not existing["end_date"] and item["end_date"]:
            existing["end_date"] = item["end_date"]
        if item["name"] and not existing["name"]:
            existing["name"] = item["name"]

    items = list(grouped.values())
    if not items:
        raise ValueError(
            "No main-package rows in that file "
            f"(Al-la-carte skipped: {skipped_alacarte})."
        )
    return items


def _index_hathway(conn: sqlite3.Connection) -> tuple[dict[str, sqlite3.Row], dict[str, sqlite3.Row]]:
    rows = conn.execute(
        "SELECT cn.id, cn.customer_id, cn.upstream_id, cn.card_number, cn.expiry_date, "
        "       cn.hathway_pack_name, cn.package_id, "
        "       c.name AS customer_name, c.code AS customer_code, "
        "       p.name AS package_name "
        "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
        "LEFT JOIN packages p ON p.id = cn.package_id "
        "WHERE cn.provider = 'hathway'"
    ).fetchall()
    by_n: dict[str, sqlite3.Row] = {}
    by_t: dict[str, sqlite3.Row] = {}
    for row in rows:
        n = _norm_id(row["upstream_id"] or "")
        t = _norm_id(row["card_number"] or "")
        if N_RE.match(n):
            by_n[n] = row
        elif T_RE.match(n) and n not in by_t:
            by_t[n] = row
        if T_RE.match(t) and t not in by_t:
            by_t[t] = row
    return by_n, by_t


def _match_row(
    item: dict, by_n: dict[str, sqlite3.Row], by_t: dict[str, sqlite3.Row]
) -> sqlite3.Row | None:
    n, t = item.get("stb_n") or "", item.get("stb_t") or ""
    if n and n in by_n:
        return by_n[n]
    if t and t in by_t:
        return by_t[t]
    if t and t in by_n:
        return by_n[t]
    return None


def preview(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    by_n, by_t = _index_hathway(conn)
    rows: list[dict] = []
    for item in items:
        matched = _match_row(item, by_n, by_t)
        pack = (item.get("package") or "").strip()
        end_date = (item.get("end_date") or "").strip()
        row = {
            "stb_n": item.get("stb_n") or "",
            "stb_t": item.get("stb_t") or "",
            "sheet_name": item.get("name") or "",
            "plan_type": item.get("plan_type") or "",
            "package": pack,
            "end_date": end_date,
            "alacarte_ignored": int(item.get("alacarte_ignored") or 0),
            "connection_id": None,
            "customer_id": None,
            "customer_name": "",
            "customer_code": "",
            "current_expiry": "",
            "bix_plan": "",
            "current_pack": "",
            "current_card": "",
            "match": "",
            "action": "unmatched",
        }
        if not pack or not end_date:
            row["action"] = "no_main_pack" if matched else "unmatched"
            if matched:
                row.update(_match_fields(matched))
            rows.append(row)
            continue
        if matched is None:
            rows.append(row)
            continue
        row.update(_match_fields(matched))
        current_expiry = (matched["expiry_date"] or "").strip()
        current_pack = (matched["hathway_pack_name"] or "").strip()
        current_card = _norm_id(matched["card_number"] or "")
        will_fill_card = bool(row["stb_t"] and not current_card)
        expiry_change = current_expiry != end_date
        pack_change = current_pack != pack
        if expiry_change or pack_change or will_fill_card:
            row["action"] = "update"
        else:
            row["action"] = "unchanged"
        rows.append(row)

    order = {"update": 0, "no_main_pack": 1, "unmatched": 2, "unchanged": 3}
    rows.sort(key=lambda r: (order.get(r["action"], 9), r.get("customer_name") or "", r.get("stb_n") or ""))
    return rows


def _match_fields(matched: sqlite3.Row) -> dict:
    return {
        "connection_id": int(matched["id"]),
        "customer_id": int(matched["customer_id"]),
        "customer_name": matched["customer_name"] or "",
        "customer_code": matched["customer_code"] or "",
        "current_expiry": (matched["expiry_date"] or "").strip(),
        "bix_plan": matched["package_name"] or "",
        "current_pack": (matched["hathway_pack_name"] or "").strip(),
        "current_card": _norm_id(matched["card_number"] or ""),
        "match": (matched["upstream_id"] or "").strip(),
    }


def summarize(rows: list[dict]) -> dict:
    counts = {
        "total": len(rows),
        "update": 0,
        "unchanged": 0,
        "unmatched": 0,
        "no_main_pack": 0,
        "expiry_change": 0,
        "pack_change": 0,
        "card_fill": 0,
    }
    for row in rows:
        action = row.get("action") or ""
        if action in counts:
            counts[action] += 1
        if action != "update":
            continue
        if (row.get("current_expiry") or "") != (row.get("end_date") or ""):
            counts["expiry_change"] += 1
        if (row.get("current_pack") or "") != (row.get("package") or ""):
            counts["pack_change"] += 1
        if row.get("stb_t") and not row.get("current_card"):
            counts["card_fill"] += 1
    return counts


def apply_preview(conn: sqlite3.Connection, rows: list[dict], *, actor: str | None = None) -> dict:
    """Write expiry + staff pack on matched STBs. Never touches package_id."""
    stamp = now_iso()
    updated = 0
    skipped = 0
    for row in rows:
        if row.get("action") != "update" or not row.get("connection_id"):
            skipped += 1
            continue
        current = conn.execute(
            "SELECT id, card_number, package_id FROM connections "
            "WHERE id = ? AND provider = 'hathway'",
            (int(row["connection_id"]),),
        ).fetchone()
        if current is None:
            skipped += 1
            continue
        card = (current["card_number"] or "").strip()
        incoming_t = (row.get("stb_t") or "").strip()
        if not card and incoming_t:
            card = incoming_t
        conn.execute(
            "UPDATE connections SET expiry_date = ?, hathway_pack_name = ?, "
            "card_number = ?, updated_at = ? WHERE id = ?",
            (
                row.get("end_date") or None,
                (row.get("package") or "").strip(),
                card or None,
                stamp,
                int(row["connection_id"]),
            ),
        )
        updated += 1

    summary = summarize(rows)
    summary["updated"] = updated
    summary["skipped"] = skipped
    log_activity(
        conn,
        "hathway_expiry_upload",
        (
            f"Hathway expiry sheet applied: {updated} STB(s) updated, "
            f"{summary['unmatched']} not on this platform. "
            "Bix plans and bills were not changed."
        ),
        actor=actor,
        meta_json=json.dumps(summary),
    )
    return summary


def expiry_report_buckets(
    conn: sqlite3.Connection,
    *,
    date_from: str = "",
    date_to: str = "",
) -> list[dict]:
    """STB counts by PlanExpiry date (connections.expiry_date)."""
    sql = (
        "SELECT cn.expiry_date AS end_date, COUNT(*) AS stb_count "
        "FROM connections cn "
        "WHERE cn.provider = 'hathway' "
        "AND cn.status != 'terminated' "
        "AND cn.expiry_date IS NOT NULL AND trim(cn.expiry_date) != ''"
    )
    params: list = []
    if date_from:
        sql += " AND cn.expiry_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND cn.expiry_date <= ?"
        params.append(date_to)
    sql += " GROUP BY cn.expiry_date ORDER BY cn.expiry_date"
    out = []
    for row in conn.execute(sql, params):
        end_date = row["end_date"]
        out.append({
            "end_date": end_date,
            "stb_count": int(row["stb_count"] or 0),
            "days_away": days_until(end_date),
        })
    return out


def expiry_report_day(conn: sqlite3.Connection, day: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT cn.id AS connection_id, cn.customer_id, cn.upstream_id, cn.card_number, "
            "cn.status, cn.hathway_pack_name, cn.expiry_date, "
            "c.name AS customer_name, c.phone, c.area, c.code "
            "FROM connections cn "
            "JOIN customers c ON c.id = cn.customer_id "
            "WHERE cn.provider = 'hathway' AND cn.status != 'terminated' "
            "AND cn.expiry_date = ? "
            "ORDER BY c.name COLLATE NOCASE, cn.upstream_id",
            (day,),
        )
    ]
