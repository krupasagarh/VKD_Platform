"""Field-agent tracking: GPS pings, collections, and complaints attended."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .money import now_iso, today

SOURCE_LABELS = {
    "ping": "GPS ping",
    "house": "House pin",
    "payment": "Collected payment",
    "complaint": "Complaint visit",
    "manual": "Shared location",
}


def maps_dir(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}"


def maps_view(lat: float, lng: float) -> str:
    return f"https://www.google.com/maps?q={lat},{lng}"


def parse_coords(lat: str = "", lng: str = "") -> tuple[float, float] | None:
    try:
        if not (lat or "").strip() or not (lng or "").strip():
            return None
        pair = (float(lat.strip()), float(lng.strip()))
    except ValueError:
        return None
    if abs(pair[0]) > 90 or abs(pair[1]) > 180:
        return None
    return pair


def parse_accuracy(raw: str = "") -> float | None:
    try:
        if not (raw or "").strip():
            return None
        return float(raw.strip())
    except ValueError:
        return None


def backfill_agent_ids(conn: sqlite3.Connection) -> None:
    """Match older name-only rows to the agent who collected or fixed them."""
    try:
        conn.execute(
            "UPDATE payments SET collected_agent_id = ("
            "  SELECT a.id FROM agents a "
            "  WHERE lower(trim(a.name)) = lower(trim(payments.collected_by)) LIMIT 1"
            ") WHERE collected_agent_id IS NULL "
            "AND collected_by IS NOT NULL AND trim(collected_by) != ''"
        )
        conn.execute(
            "UPDATE complaints SET resolved_agent_id = ("
            "  SELECT a.id FROM agents a "
            "  WHERE lower(trim(a.name)) = lower(trim(complaints.resolved_by)) LIMIT 1"
            ") WHERE resolved_agent_id IS NULL "
            "AND resolved_by IS NOT NULL AND trim(resolved_by) != ''"
        )
    except sqlite3.OperationalError:
        pass


def record_location(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    lat: float,
    lng: float,
    accuracy: float | None = None,
    source: str = "ping",
    customer_id: int | None = None,
    stamp: str | None = None,
) -> int | None:
    """Store a point. Rapid GPS pings from the same phone are collapsed."""
    if not agent_id:
        return None
    when = stamp or now_iso()
    source = (source or "ping").strip().lower() or "ping"
    if source == "ping":
        last = conn.execute(
            "SELECT id, recorded_at FROM agent_locations "
            "WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if last and _seconds_apart(last["recorded_at"], when) < 90:
            conn.execute(
                "UPDATE agent_locations SET lat = ?, lng = ?, accuracy = ?, "
                "recorded_at = ? WHERE id = ?",
                (lat, lng, accuracy, when, last["id"]),
            )
            return int(last["id"])
    cursor = conn.execute(
        "INSERT INTO agent_locations(agent_id, customer_id, lat, lng, accuracy, source, recorded_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (agent_id, customer_id, lat, lng, accuracy, source, when),
    )
    return int(cursor.lastrowid)


def record_visit(
    conn: sqlite3.Connection,
    *,
    agent_id: int | None,
    customer_id: int | None,
    lat: float | None = None,
    lng: float | None = None,
    accuracy: float | None = None,
    source: str = "payment",
) -> int | None:
    """Prefer live GPS; otherwise mark the agent at the customer's house pin."""
    if not agent_id:
        return None
    if lat is not None and lng is not None:
        return record_location(
            conn,
            agent_id=agent_id,
            lat=lat,
            lng=lng,
            accuracy=accuracy,
            source=source,
            customer_id=customer_id,
        )
    if not customer_id:
        return None
    house = conn.execute(
        "SELECT lat, lng FROM customers WHERE id = ? AND lat IS NOT NULL AND lng IS NOT NULL",
        (customer_id,),
    ).fetchone()
    if house is None:
        return None
    return record_location(
        conn,
        agent_id=agent_id,
        lat=float(house["lat"]),
        lng=float(house["lng"]),
        source="house",
        customer_id=customer_id,
    )


def can_see_roster(agent: dict | None) -> bool:
    if not agent or not agent.get("active"):
        return False
    if agent.get("role") == "admin":
        return True
    perms = agent.get("permissions") or []
    return any(key in perms for key in ("agents", "payments", "complaints", "customers_view"))


def can_see_agent(viewer: dict | None, target_id: int) -> bool:
    if not can_see_roster(viewer):
        return False
    if viewer.get("role") == "admin" or "agents" in (viewer.get("permissions") or []):
        return True
    return int(viewer.get("id") or 0) == int(target_id)


def sees_everyone(viewer: dict | None) -> bool:
    if not viewer:
        return False
    return viewer.get("role") == "admin" or "agents" in (viewer.get("permissions") or [])


def _seconds_apart(earlier: str, later: str) -> float:
    try:
        a = datetime.fromisoformat((earlier or "")[:19])
        b = datetime.fromisoformat((later or "")[:19])
    except ValueError:
        return 9999
    return abs((b - a).total_seconds())


def _day_bounds(day: str) -> tuple[str, str]:
    return f"{day} 00:00:00", f"{day} 23:59:59"


def office_summary(conn: sqlite3.Connection, day: str | None = None) -> dict:
    rows = agent_summaries(conn, day=day)
    return {
        "day": day or today().strftime("%Y-%m-%d"),
        "agents": len(rows),
        "seen": sum(1 for row in rows if row["seen_today"]),
        "collected_paise": sum(int(row["collected_paise"]) for row in rows),
        "payments": sum(int(row["payment_count"]) for row in rows),
        "complaints_open": sum(int(row["complaints_open"]) for row in rows),
        "complaints_fixed": sum(int(row["complaints_fixed"]) for row in rows),
    }


def agent_summaries(
    conn: sqlite3.Connection,
    *,
    day: str | None = None,
    only_agent_id: int | None = None,
) -> list[dict]:
    day = day or today().strftime("%Y-%m-%d")
    start, end = _day_bounds(day)
    month_start = f"{day[:7]}-01 00:00:00"
    agents = conn.execute(
        "SELECT id, name, username, role, active FROM agents "
        "WHERE active = 1 "
        + ("AND id = ? " if only_agent_id else "")
        + "ORDER BY CASE role WHEN 'collector' THEN 0 ELSE 1 END, name COLLATE NOCASE",
        (only_agent_id,) if only_agent_id else (),
    ).fetchall()

    out: list[dict] = []
    for agent in agents:
        aid = int(agent["id"])
        last = conn.execute(
            "SELECT * FROM agent_locations WHERE agent_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
            (aid,),
        ).fetchone()
        pay = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS paise, COUNT(*) AS n FROM payments "
            "WHERE collected_agent_id = ? "
            "AND lower(COALESCE(mode, '')) != 'adjustment' "
            "AND paid_at >= ? AND paid_at <= ?",
            (aid, start, end),
        ).fetchone()
        month = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS paise, COUNT(*) AS n FROM payments "
            "WHERE collected_agent_id = ? "
            "AND lower(COALESCE(mode, '')) != 'adjustment' "
            "AND paid_at >= ? AND paid_at <= ?",
            (aid, month_start, end),
        ).fetchone()
        open_n = conn.execute(
            "SELECT COUNT(*) AS n FROM complaints "
            "WHERE assigned_agent_id = ? AND status != 'fixed'",
            (aid,),
        ).fetchone()["n"]
        fixed_n = conn.execute(
            "SELECT COUNT(*) AS n FROM complaints "
            "WHERE status = 'fixed' AND resolved_at >= ? AND resolved_at <= ? "
            "AND (resolved_agent_id = ? OR ("
            "  resolved_agent_id IS NULL AND lower(trim(resolved_by)) = lower(trim(?))"
            "))",
            (start, end, aid, agent["name"]),
        ).fetchone()["n"]
        last_at = last["recorded_at"] if last else ""
        item = {
            "id": aid,
            "name": agent["name"],
            "username": agent["username"],
            "role": agent["role"],
            "lat": last["lat"] if last else None,
            "lng": last["lng"] if last else None,
            "accuracy": last["accuracy"] if last else None,
            "last_at": last_at,
            "last_source": last["source"] if last else "",
            "maps_dir": maps_dir(last["lat"], last["lng"]) if last else "",
            "maps_view": maps_view(last["lat"], last["lng"]) if last else "",
            "seen_today": bool(last_at and last_at[:10] == day),
            "collected_paise": int(pay["paise"] or 0),
            "payment_count": int(pay["n"] or 0),
            "month_paise": int(month["paise"] or 0),
            "month_count": int(month["n"] or 0),
            "complaints_open": int(open_n or 0),
            "complaints_fixed": int(fixed_n or 0),
        }
        out.append(item)

    out.sort(
        key=lambda row: (
            0 if (row["seen_today"] or row["payment_count"] or row["complaints_fixed"] or row["complaints_open"]) else 1,
            0 if row["role"] == "collector" else 1,
            row["name"].lower(),
        )
    )
    return out


def agent_day(
    conn: sqlite3.Connection,
    agent_id: int,
    *,
    day: str | None = None,
    trail_limit: int = 40,
) -> dict | None:
    rows = agent_summaries(conn, day=day, only_agent_id=agent_id)
    if not rows:
        return None
    day = day or today().strftime("%Y-%m-%d")
    start, end = _day_bounds(day)
    summary = rows[0]
    payments = conn.execute(
        "SELECT p.*, c.name AS customer_name, c.code AS customer_code, "
        "c.lat AS customer_lat, c.lng AS customer_lng "
        "FROM payments p JOIN customers c ON c.id = p.customer_id "
        "WHERE p.collected_agent_id = ? "
        "AND lower(COALESCE(p.mode, '')) != 'adjustment' "
        "AND p.paid_at >= ? AND p.paid_at <= ? "
        "ORDER BY p.paid_at DESC, p.id DESC",
        (agent_id, start, end),
    ).fetchall()
    complaints = conn.execute(
        "SELECT cp.*, c.name AS customer_name, c.code AS customer_code, c.phone AS customer_phone "
        "FROM complaints cp JOIN customers c ON c.id = cp.customer_id "
        "WHERE (cp.assigned_agent_id = ? AND cp.status != 'fixed') "
        "OR (cp.status = 'fixed' AND cp.resolved_at >= ? AND cp.resolved_at <= ? "
        "    AND (cp.resolved_agent_id = ? OR ("
        "      cp.resolved_agent_id IS NULL AND lower(trim(cp.resolved_by)) = lower(trim(?))"
        "    ))) "
        "ORDER BY CASE cp.status WHEN 'fixed' THEN 1 ELSE 0 END, cp.id DESC",
        (agent_id, start, end, agent_id, summary["name"]),
    ).fetchall()
    trail = conn.execute(
        "SELECT loc.*, c.name AS customer_name "
        "FROM agent_locations loc "
        "LEFT JOIN customers c ON c.id = loc.customer_id "
        "WHERE loc.agent_id = ? ORDER BY loc.recorded_at DESC, loc.id DESC LIMIT ?",
        (agent_id, trail_limit),
    ).fetchall()
    trail_out = []
    for row in trail:
        item = dict(row)
        item["maps_dir"] = maps_dir(row["lat"], row["lng"])
        item["source_label"] = SOURCE_LABELS.get(row["source"], row["source"])
        trail_out.append(item)
    return {
        "agent": summary,
        "day": day,
        "payments": payments,
        "complaints": complaints,
        "trail": trail_out,
    }


def stale_cutoff_hours(hours: int = 12) -> str:
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
