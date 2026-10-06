"""Agent cash / UPI settlements and field expenditures.

Date range on /settlements reads recorded collections: Hathway as cable,
Railtel as internet. Scanner / owner UPI / shop QR already reached the
owner. Cash, agent UPI, bank and cheque stay with the agent until they
hand that amount over. Reports are for expenses, proofs and notes.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from . import auth
from .config import settings
from .money import now_iso, parse_date, to_paise, today

EXPENSE_KINDS = (
    ("meals", "Meals"),
    ("parcel", "Parcel"),
    ("other", "Other"),
)
EXPENSE_KIND_KEYS = tuple(key for key, _label in EXPENSE_KINDS)
LINE_INTERNET = "internet_cash"
LINE_UPI_HANDOVER = "upi_handover"
LINE_CASH_HANDOVER = "cash_handover"
LINE_CUSTOMER_CASH = "customer_cash"
HANDOVER_LINE_KINDS = (LINE_UPI_HANDOVER, LINE_CASH_HANDOVER)

# Scanner / owner UPI / shop QR already landed with the owner.
# Cash, agent UPI, bank, cheque stay with the collector until they settle.
OWNER_HELD_MODES = frozenset({"scanner", "owner_upi"})
CABLE_PROVIDERS = frozenset({"hathway"})
INTERNET_PROVIDERS = frozenset({"railtel"})
def owner_display_name() -> str:
    return (settings.operator or "Owner").strip() or "Owner"


def payment_mode_labels() -> dict[str, str]:
    name = owner_display_name()
    return {
        "cash": "Cash",
        "upi": "UPI to agent",
        "bank": "Bank",
        "gateway": "Card",
        "cheque": "Cheque",
        "scanner": "Scanner / shop QR",
        "owner_upi": f"UPI to {name}",
    }


# Kept for older imports; UI should call payment_mode_labels().
PAYMENT_MODE_LABELS = payment_mode_labels()


def _owner_held_mode(mode: str, notes: str = "") -> bool:
    if (mode or "").strip().lower() in OWNER_HELD_MODES:
        return True
    return "qr pay" in (notes or "").lower()


def _service_group(provider: str) -> str:
    key = (provider or "").strip().lower()
    if key in CABLE_PROVIDERS:
        return "cable"
    if key in INTERNET_PROVIDERS:
        return "internet"
    return "other"


def _empty_service() -> dict:
    return {
        "payment_count": 0,
        "total_paise": 0,
        "agent_paise": 0,
        "owner_paise": 0,
        "by_mode": {},
    }


def _add_payment(service: dict, amount: int, mode: str, owner_held: bool) -> None:
    service["payment_count"] += 1
    service["total_paise"] += amount
    if owner_held:
        service["owner_paise"] += amount
    else:
        service["agent_paise"] += amount
    service["by_mode"][mode] = int(service["by_mode"].get(mode) or 0) + amount


def _empty_agent_bucket(agent_id: int, name: str, username: str) -> dict:
    return {
        "id": agent_id,
        "name": name or ("Unassigned" if not agent_id else f"Agent #{agent_id}"),
        "username": username or "",
        "cable": _empty_service(),
        "internet": _empty_service(),
        "other": _empty_service(),
        "agent_held_paise": 0,
        "owner_held_paise": 0,
        "total_paise": 0,
        "payment_count": 0,
        "owner_upi_paise": 0,
        "owner_scanner_paise": 0,
        "payments": [],
        "expenses_paise": 0,
        "handed_cash_paise": 0,
        "handed_upi_paise": 0,
        "handed_paise": 0,
        "should_pay_paise": 0,
        "balance_paise": 0,
        "reports": [],
        "proof_count": 0,
        "report_count": 0,
        "meals_paise": 0,
        "parcel_paise": 0,
        "other_exp_paise": 0,
        "inflow_paise": 0,
        "settled_paise": 0,
        "is_owner": False,
        "role": "",
        "customer_cash_lines": [],
        "customer_cash_paise": 0,
    }


def _finish_agent_bucket(bucket: dict) -> dict:
    bucket["agent_held_paise"] = (
        int(bucket["cable"]["agent_paise"])
        + int(bucket["internet"]["agent_paise"])
        + int(bucket["other"]["agent_paise"])
    )
    bucket["owner_held_paise"] = (
        int(bucket["cable"]["owner_paise"])
        + int(bucket["internet"]["owner_paise"])
        + int(bucket["other"]["owner_paise"])
    )
    bucket["total_paise"] = bucket["agent_held_paise"] + bucket["owner_held_paise"]
    bucket["inflow_paise"] = bucket["agent_held_paise"]
    bucket["settled_paise"] = bucket["owner_held_paise"]
    bucket["handed_paise"] = int(bucket.get("handed_cash_paise") or 0) + int(
        bucket.get("handed_upi_paise") or 0
    )
    bucket["should_pay_paise"] = (
        bucket["agent_held_paise"]
        - int(bucket.get("expenses_paise") or 0)
        - int(bucket.get("handed_paise") or 0)
    )
    bucket["balance_paise"] = bucket["should_pay_paise"]
    if bucket.get("is_owner"):
        # Owner/admin collections are already in the shop.
        bucket["should_pay_paise"] = 0
        bucket["balance_paise"] = 0
    return bucket


def snapshot_from_bucket(bucket: dict) -> dict:
    return {
        "cable_agent_paise": int(bucket["cable"]["agent_paise"]),
        "cable_owner_paise": int(bucket["cable"]["owner_paise"]),
        "internet_agent_paise": int(bucket["internet"]["agent_paise"]),
        "internet_owner_paise": int(bucket["internet"]["owner_paise"]),
        "other_agent_paise": int(bucket["other"]["agent_paise"]),
        "other_owner_paise": int(bucket["other"]["owner_paise"]),
        "agent_held_paise": int(bucket["agent_held_paise"]),
        "owner_held_paise": int(bucket["owner_held_paise"]),
        "owner_upi_paise": int(bucket.get("owner_upi_paise") or 0),
        "owner_scanner_paise": int(bucket.get("owner_scanner_paise") or 0),
        "payment_count": int(bucket["payment_count"]),
        "by_mode": dict(bucket["cable"]["by_mode"]),  # overwritten below
    }


def apply_collection_snapshot(payload: dict, bucket: dict) -> dict:
    """Copy live collections onto a report so it is not typed in by hand."""
    _finish_agent_bucket(bucket)
    snap = snapshot_from_bucket(bucket)
    snap["by_mode"] = {}
    for key in ("cable", "internet", "other"):
        for mode, paise in (bucket[key].get("by_mode") or {}).items():
            snap["by_mode"][mode] = int(snap["by_mode"].get(mode) or 0) + int(paise)
    payload["cable_cash_paise"] = int(bucket["cable"]["agent_paise"])
    payload["cable_upi_agent_paise"] = int(bucket["internet"]["agent_paise"])
    payload["owner_upi_paise"] = int(bucket.get("owner_upi_paise") or 0)
    payload["owner_scanner_paise"] = int(bucket.get("owner_scanner_paise") or 0)
    payload["collection_json"] = json.dumps(snap)
    payload["collection"] = bucket
    return payload


def fetch_collections(
    conn: sqlite3.Connection,
    *,
    day_from: str,
    day_to: str,
    agent_id: int | None = None,
) -> dict[int, dict]:
    """Collections already on the books for this date range, grouped by collector."""
    sql = (
        "SELECT p.id, p.amount_paise, p.mode, p.notes, p.paid_at, p.reference, "
        "p.collected_agent_id, p.customer_id, c.name AS customer_name, "
        "COALESCE("
        "  NULLIF(trim(cn.provider), ''), "
        "  (SELECT cn2.provider FROM connections cn2 "
        "   WHERE cn2.customer_id = p.customer_id "
        "   ORDER BY CASE WHEN cn2.status = 'active' THEN 0 ELSE 1 END, cn2.id LIMIT 1)"
        ") AS provider, "
        "a.name AS agent_name, a.username AS agent_username, a.role AS agent_role "
        "FROM payments p "
        "JOIN customers c ON c.id = p.customer_id "
        "LEFT JOIN connections cn ON cn.id = p.connection_id "
        "LEFT JOIN agents a ON a.id = p.collected_agent_id "
        "WHERE substr(p.paid_at, 1, 10) >= ? AND substr(p.paid_at, 1, 10) <= ? "
        "AND lower(COALESCE(p.mode, '')) != 'adjustment' "
    )
    params: list = [day_from, day_to]
    if agent_id:
        sql += "AND p.collected_agent_id = ? "
        params.append(int(agent_id))
    sql += "ORDER BY p.paid_at DESC, p.id DESC"
    rows = conn.execute(sql, params).fetchall()
    agents: dict[int, dict] = {}
    for row in rows:
        aid = int(row["collected_agent_id"] or 0)
        role = (row["agent_role"] or "").strip().lower() if "agent_role" in row.keys() else ""
        is_owner_login = role == "admin"
        bucket = agents.get(aid)
        if bucket is None:
            bucket = _empty_agent_bucket(
                aid,
                row["agent_name"] or ("Unassigned" if not aid else ""),
                row["agent_username"] or "",
            )
            bucket["role"] = role
            bucket["is_owner"] = is_owner_login
            agents[aid] = bucket
        amount = int(row["amount_paise"] or 0)
        mode = (row["mode"] or "cash").strip().lower() or "cash"
        # Cash the owner collected is already in the shop — not a handover from an agent.
        owner_held = is_owner_login or _owner_held_mode(mode, row["notes"] or "")
        group = _service_group(row["provider"] or "")
        _add_payment(bucket[group], amount, mode, owner_held)
        bucket["payment_count"] += 1
        if owner_held:
            if mode == "owner_upi":
                bucket["owner_upi_paise"] += amount
            elif mode == "scanner" or "qr pay" in (row["notes"] or "").lower():
                bucket["owner_scanner_paise"] += amount
        if len(bucket["payments"]) < 80:
            bucket["payments"].append(
                {
                    "id": int(row["id"]),
                    "paid_at": row["paid_at"],
                    "mode": mode,
                    "amount_paise": amount,
                    "customer_name": row["customer_name"],
                    "customer_id": int(row["customer_id"]),
                    "provider": (row["provider"] or "").strip().lower(),
                    "owner_held": owner_held,
                    "group": group,
                }
            )
    for bucket in agents.values():
        _finish_agent_bucket(bucket)
    return agents


def live_summaries(
    conn: sqlite3.Connection,
    *,
    day_from: str,
    day_to: str,
    only_agent_id: int | None = None,
) -> dict:
    """Date-range view: collections from payments, expenses from handover reports."""
    agents = fetch_collections(
        conn, day_from=day_from, day_to=day_to, agent_id=only_agent_id
    )
    reports = list_reports(conn, day_from=day_from, day_to=day_to, agent_id=only_agent_id)
    for report in reports:
        aid = int(report["agent_id"])
        bucket = agents.get(aid)
        if bucket is None:
            bucket = _empty_agent_bucket(
                aid, report.get("agent_name") or "", report.get("agent_username") or ""
            )
            role = str(report.get("agent_role") or "").strip().lower()
            bucket["role"] = role
            bucket["is_owner"] = role == "admin"
            agents[aid] = bucket
        bucket["reports"].append(report)
        bucket["report_count"] += 1
        bucket["expenses_paise"] += int(report.get("expenses_paise") or 0)
        bucket["meals_paise"] += int(report.get("meals_paise") or 0)
        bucket["parcel_paise"] += int(report.get("parcel_paise") or 0)
        bucket["other_exp_paise"] += int(report.get("other_exp_paise") or 0)
        bucket["proof_count"] += int(report.get("proof_count") or 0)
        bucket["handed_cash_paise"] += int(report.get("handed_cash_paise") or 0)
        bucket["handed_upi_paise"] += int(report.get("handed_upi_paise") or 0)
        for line in report.get("customer_cash_lines") or []:
            amount = int(line.get("amount_paise") or 0)
            group = _service_group(line.get("provider") or "")
            bucket[group]["agent_paise"] += amount
            bucket[group]["total_paise"] += amount
            bucket[group]["payment_count"] += 1
            bucket["customer_cash_paise"] = int(bucket.get("customer_cash_paise") or 0) + amount
            bucket.setdefault("customer_cash_lines", []).append(
                {**line, "settlement_id": int(report["id"])}
            )
    office = _empty_agent_bucket(0, "All agents", "")
    rows = []
    for bucket in sorted(agents.values(), key=lambda row: row["name"].lower()):
        _finish_agent_bucket(bucket)
        rows.append(bucket)
        office["cable"]["agent_paise"] += bucket["cable"]["agent_paise"]
        office["cable"]["owner_paise"] += bucket["cable"]["owner_paise"]
        office["cable"]["total_paise"] += bucket["cable"]["total_paise"]
        office["cable"]["payment_count"] += bucket["cable"]["payment_count"]
        office["internet"]["agent_paise"] += bucket["internet"]["agent_paise"]
        office["internet"]["owner_paise"] += bucket["internet"]["owner_paise"]
        office["internet"]["total_paise"] += bucket["internet"]["total_paise"]
        office["internet"]["payment_count"] += bucket["internet"]["payment_count"]
        office["other"]["agent_paise"] += bucket["other"]["agent_paise"]
        office["other"]["owner_paise"] += bucket["other"]["owner_paise"]
        office["other"]["total_paise"] += bucket["other"]["total_paise"]
        office["other"]["payment_count"] += bucket["other"]["payment_count"]
        office["payment_count"] += bucket["payment_count"]
        office["expenses_paise"] += bucket["expenses_paise"]
        office["meals_paise"] += bucket["meals_paise"]
        office["parcel_paise"] += bucket["parcel_paise"]
        office["other_exp_paise"] += bucket["other_exp_paise"]
        office["report_count"] += bucket["report_count"]
        office["proof_count"] += bucket["proof_count"]
        office["owner_upi_paise"] += bucket["owner_upi_paise"]
        office["owner_scanner_paise"] += bucket["owner_scanner_paise"]
        office["handed_cash_paise"] += bucket["handed_cash_paise"]
        office["handed_upi_paise"] += bucket["handed_upi_paise"]
        office["customer_cash_paise"] = int(office.get("customer_cash_paise") or 0) + int(
            bucket.get("customer_cash_paise") or 0
        )
        office.setdefault("customer_cash_lines", []).extend(bucket.get("customer_cash_lines") or [])
    _finish_agent_bucket(office)
    return {
        "day_from": day_from,
        "day_to": day_to,
        "rows": rows,
        "office": office,
        "reports": reports,
    }


def collection_for_agent(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    day_from: str,
    day_to: str,
) -> dict:
    agents = fetch_collections(conn, day_from=day_from, day_to=day_to, agent_id=agent_id)
    bucket = agents.get(int(agent_id))
    if bucket is None:
        row = conn.execute(
            "SELECT name, username, role FROM agents WHERE id = ?", (int(agent_id),)
        ).fetchone()
        bucket = _empty_agent_bucket(
            int(agent_id),
            (row["name"] if row else "") or "",
            (row["username"] if row else "") or "",
        )
        role = ((row["role"] if row else "") or "").strip().lower()
        bucket["role"] = role
        bucket["is_owner"] = role == "admin"
    return _finish_agent_bucket(bucket)


def attach_live_collections(conn: sqlite3.Connection, agent_id: int, payload: dict) -> dict:
    bucket = collection_for_agent(
        conn,
        agent_id=agent_id,
        day_from=payload["period_from"],
        day_to=payload["period_to"],
    )
    apply_collection_snapshot(payload, bucket)
    return payload


PROOF_KINDS = (
    ("owner_upi", "UPI to owner"),
    ("upi_handover", "UPI handover"),
    ("scanner", "Scanner / QR"),
    ("agent_upi", "UPI to agent"),
    ("cash", "Cash collected"),
    ("cash_handover", "Cash handover"),
    ("expense", "Expense receipt"),
    ("other", "Other proof"),
)
PROOF_KIND_KEYS = tuple(key for key, _label in PROOF_KINDS)


def proof_kinds() -> tuple[tuple[str, str], ...]:
    name = owner_display_name()
    return (
        ("owner_upi", f"UPI to {name}"),
        ("upi_handover", "UPI handover"),
        ("scanner", "Scanner / QR"),
        ("agent_upi", "UPI to agent"),
        ("cash", "Cash collected"),
        ("cash_handover", "Cash handover"),
        ("expense", "Expense receipt"),
        ("other", "Other proof"),
    )
MAX_PROOF_BYTES = 8 * 1024 * 1024
_MAX_PROOFS_PER_SAVE = 20


class SettlementError(ValueError):
    """User-facing validation error for a settlement form."""


def _day(raw: str | None) -> str | None:
    parsed = parse_date(raw)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def _paise(raw) -> int:
    return max(0, int(to_paise(raw or 0)))


def _listed(form, key: str) -> list[str]:
    if hasattr(form, "getlist"):
        values = form.getlist(key)
    else:
        value = form.get(key)
        values = value if isinstance(value, list) else [value]
    out = []
    for item in values:
        if item is None:
            continue
        out.append(str(item).strip())
    return out


def _one(form, key: str, default: str = "") -> str:
    value = form.get(key)
    if value is None:
        return default
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value or default).strip()


def _parse_snap(row: dict) -> dict | None:
    snap = row.get("collection_snap")
    if isinstance(snap, dict) and snap:
        return snap
    raw = row.get("collection_json") or ""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _handover_view(line: dict) -> dict:
    kind = (line.get("kind") or "").strip().lower()
    handed_to = (line.get("customer_name") or "").strip()
    vpa = (line.get("comment") or "").strip() if kind == LINE_UPI_HANDOVER else ""
    if kind == LINE_UPI_HANDOVER and not handed_to:
        handed_to = owner_display_name()
    return {
        "kind": kind,
        "line_date": line.get("line_date") or "",
        "handed_to": handed_to,
        "vpa": vpa,
        "comment": (line.get("comment") or "").strip(),
        "amount_paise": int(line.get("amount_paise") or 0),
        "is_upi": kind == LINE_UPI_HANDOVER,
        "is_cash": kind == LINE_CASH_HANDOVER,
        "customer_name": handed_to,
    }


def _customer_cash_view(line: dict) -> dict:
    from .upstream.providers import PROVIDER_LABELS_VIEW

    provider = (line.get("provider") or "").strip().lower()
    return {
        "kind": LINE_CUSTOMER_CASH,
        "line_date": line.get("line_date") or "",
        "customer_id": int(line["customer_id"]) if line.get("customer_id") else None,
        "customer_name": (line.get("customer_name") or "").strip(),
        "area": (line.get("area") or "").strip(),
        "provider": provider,
        "provider_label": PROVIDER_LABELS_VIEW.get(provider, provider.title() if provider else ""),
        "comment": (line.get("comment") or "").strip(),
        "amount_paise": int(line.get("amount_paise") or 0),
    }


def compute_totals(row: dict, lines: list[dict] | None = None) -> dict:
    lines = lines if lines is not None else list(row.get("lines") or [])
    expenses_by_kind = {key: 0 for key in EXPENSE_KIND_KEYS}
    expense_lines = []
    internet_lines = []
    handover_lines = []
    cash_handover_lines = []
    customer_cash_lines = []
    handed_upi = 0
    handed_cash = 0
    extra_cable = 0
    extra_internet = 0
    extra_other = 0
    for line in lines:
        amount = int(line["amount_paise"] or 0)
        kind = (line.get("kind") or "").strip().lower()
        if kind == LINE_INTERNET:
            internet_lines.append(line)
        elif kind == LINE_UPI_HANDOVER:
            handed_upi += amount
            handover_lines.append(_handover_view(line))
        elif kind == LINE_CASH_HANDOVER:
            handed_cash += amount
            view = _handover_view(line)
            handover_lines.append(view)
            cash_handover_lines.append(view)
        elif kind == LINE_CUSTOMER_CASH:
            customer_cash_lines.append(_customer_cash_view(line))
            group = _service_group(line.get("provider") or "")
            if group == "cable":
                extra_cable += amount
            elif group == "internet":
                extra_internet += amount
            else:
                extra_other += amount
        elif kind in expenses_by_kind:
            expenses_by_kind[kind] += amount
            expense_lines.append(line)
    expenses = sum(expenses_by_kind.values())
    handed = handed_upi + handed_cash
    extra_cash = extra_cable + extra_internet + extra_other
    snap = _parse_snap(row)
    if snap:
        cable_agent = int(snap.get("cable_agent_paise") or 0) + extra_cable
        internet_agent = int(snap.get("internet_agent_paise") or 0) + extra_internet
        other_agent = int(snap.get("other_agent_paise") or 0) + extra_other
        cable_owner = int(snap.get("cable_owner_paise") or 0)
        internet_owner = int(snap.get("internet_owner_paise") or 0)
        agent_held = int(snap.get("agent_held_paise") or 0) + extra_cash
        owner_upi = int(snap.get("owner_upi_paise") or 0)
        scanner = int(snap.get("owner_scanner_paise") or 0)
        owner_held = int(snap.get("owner_held_paise") or (owner_upi + scanner))
        inflow = agent_held
        settled = owner_held
        should_pay = inflow - expenses - handed
        balance = should_pay
        return {
            "cable_cash_paise": cable_agent,
            "cable_upi_agent_paise": internet_agent,
            "internet_paise": internet_agent,
            "other_agent_paise": other_agent,
            "cable_owner_paise": cable_owner,
            "internet_owner_paise": internet_owner,
            "inflow_paise": inflow,
            "agent_held_paise": agent_held,
            "owner_held_paise": owner_held,
            "owner_upi_paise": owner_upi,
            "owner_scanner_paise": scanner,
            "settled_paise": settled,
            "meals_paise": expenses_by_kind["meals"],
            "parcel_paise": expenses_by_kind["parcel"],
            "other_exp_paise": expenses_by_kind["other"],
            "expenses_paise": expenses,
            "handed_upi_paise": handed_upi,
            "handed_cash_paise": handed_cash,
            "handed_paise": handed,
            "customer_cash_paise": extra_cash,
            "should_pay_paise": should_pay,
            "balance_paise": balance,
            "internet_lines": internet_lines,
            "expense_lines": expense_lines,
            "handover_lines": handover_lines,
            "cash_handover_lines": cash_handover_lines,
            "customer_cash_lines": customer_cash_lines,
            "collection_snap": snap,
        }
    internet = sum(int(line["amount_paise"] or 0) for line in internet_lines) + extra_internet
    cable_cash = int(row.get("cable_cash_paise") or 0) + extra_cable
    cable_upi = int(row.get("cable_upi_agent_paise") or 0)
    owner_upi = int(row.get("owner_upi_paise") or 0)
    scanner = int(row.get("owner_scanner_paise") or 0)
    inflow = cable_cash + cable_upi + internet + extra_other
    settled = owner_upi + scanner
    should_pay = inflow - expenses - handed
    balance = should_pay - settled
    return {
        "cable_cash_paise": cable_cash,
        "cable_upi_agent_paise": cable_upi,
        "internet_paise": internet,
        "inflow_paise": inflow,
        "agent_held_paise": inflow,
        "owner_held_paise": settled,
        "owner_upi_paise": owner_upi,
        "owner_scanner_paise": scanner,
        "settled_paise": settled,
        "meals_paise": expenses_by_kind["meals"],
        "parcel_paise": expenses_by_kind["parcel"],
        "other_exp_paise": expenses_by_kind["other"],
        "expenses_paise": expenses,
        "handed_upi_paise": handed_upi,
        "handed_cash_paise": handed_cash,
        "handed_paise": handed,
        "customer_cash_paise": extra_cash,
        "should_pay_paise": should_pay,
        "balance_paise": balance,
        "internet_lines": internet_lines,
        "expense_lines": expense_lines,
        "handover_lines": handover_lines,
        "cash_handover_lines": cash_handover_lines,
        "customer_cash_lines": customer_cash_lines,
    }


def empty_totals() -> dict:
    return compute_totals(
        {
            "cable_cash_paise": 0,
            "cable_upi_agent_paise": 0,
            "owner_upi_paise": 0,
            "owner_scanner_paise": 0,
        },
        [],
    )


def _with_totals(row) -> dict:
    item = dict(row)
    item.update(compute_totals(item, item.get("lines") or []))
    return item


def parse_form(form) -> dict:
    period_from = _day(_one(form, "period_from"))
    period_to = _day(_one(form, "period_to"))
    settled_on = _day(_one(form, "settled_on")) or today().strftime("%Y-%m-%d")
    if not period_from or not period_to:
        raise SettlementError("Choose the collection from and to dates.")
    if period_from > period_to:
        raise SettlementError("Collection 'from' must be on or before 'to'.")

    expenses = []
    kinds = _listed(form, "exp_kind")
    exp_dates = _listed(form, "exp_date")
    comments = _listed(form, "exp_comment")
    exp_amounts = _listed(form, "exp_amount")
    count = max(len(kinds), len(exp_dates), len(comments), len(exp_amounts))
    for i in range(count):
        kind = (kinds[i] if i < len(kinds) else "other").strip().lower()
        if kind not in EXPENSE_KIND_KEYS:
            kind = "other"
        when = _day(exp_dates[i] if i < len(exp_dates) else "") or ""
        comment = comments[i] if i < len(comments) else ""
        amount = _paise(exp_amounts[i] if i < len(exp_amounts) else 0)
        if not comment and not amount and not when:
            continue
        if amount <= 0:
            raise SettlementError("Expenditure rows need an amount.")
        expenses.append(
            {
                "kind": kind,
                "line_date": when,
                "customer_name": "",
                "comment": comment,
                "amount_paise": amount,
            }
        )

    vpa = _one(form, "owner_upi_vpa")
    upi_amount = _paise(_one(form, "upi_handover_amount"))
    handovers = []
    if upi_amount > 0:
        handovers.append(
            {
                "kind": LINE_UPI_HANDOVER,
                "line_date": settled_on,
                "customer_name": owner_display_name(),
                "comment": vpa,
                "amount_paise": upi_amount,
            }
        )

    cash_to = _listed(form, "cash_to")
    cash_amounts = _listed(form, "cash_amount")
    cash_dates = _listed(form, "cash_date")
    cash_n = max(len(cash_to), len(cash_amounts), len(cash_dates))
    for i in range(cash_n):
        who = cash_to[i] if i < len(cash_to) else ""
        amount = _paise(cash_amounts[i] if i < len(cash_amounts) else 0)
        when = _day(cash_dates[i] if i < len(cash_dates) else "") or settled_on
        if not who and not amount:
            continue
        if amount <= 0:
            raise SettlementError("Cash handover rows need an amount.")
        if not who:
            raise SettlementError("Cash handover needs who received the cash (you, Mom, …).")
        handovers.append(
            {
                "kind": LINE_CASH_HANDOVER,
                "line_date": when,
                "customer_name": who,
                "comment": "",
                "amount_paise": amount,
            }
        )

    cash_ids = _listed(form, "cash_cust_id")
    cash_names = _listed(form, "cash_cust_name")
    cash_areas = _listed(form, "cash_cust_area")
    cash_providers = _listed(form, "cash_cust_provider")
    cash_cust_amounts = _listed(form, "cash_cust_amount")
    cash_cust_dates = _listed(form, "cash_cust_date")
    cash_cust_n = max(
        len(cash_ids),
        len(cash_names),
        len(cash_areas),
        len(cash_providers),
        len(cash_cust_amounts),
        len(cash_cust_dates),
    )
    customer_cash = []
    for i in range(cash_cust_n):
        name = cash_names[i] if i < len(cash_names) else ""
        amount = _paise(cash_cust_amounts[i] if i < len(cash_cust_amounts) else 0)
        raw_id = cash_ids[i] if i < len(cash_ids) else ""
        if not name and not amount and not raw_id:
            continue
        if amount <= 0:
            raise SettlementError("Cash collected from a customer needs an amount.")
        if not name:
            raise SettlementError("Search and pick the customer who paid cash.")
        cid = int(raw_id) if raw_id.isdigit() else 0
        if not cid:
            raise SettlementError(f"Pick {name} from the search list so area and provider are saved.")
        provider = (cash_providers[i] if i < len(cash_providers) else "").strip().lower()
        if provider not in ("railtel", "hathway", "iptv", "ott"):
            provider = "railtel" if not provider else provider
        when = _day(cash_cust_dates[i] if i < len(cash_cust_dates) else "") or settled_on
        customer_cash.append(
            {
                "kind": LINE_CUSTOMER_CASH,
                "line_date": when,
                "customer_id": cid,
                "customer_name": name,
                "area": cash_areas[i] if i < len(cash_areas) else "",
                "provider": provider,
                "comment": "",
                "amount_paise": amount,
            }
        )

    return {
        "period_from": period_from,
        "period_to": period_to,
        "settled_on": settled_on,
        "cable_cash_paise": 0,
        "cable_upi_agent_paise": 0,
        "owner_upi_paise": 0,
        "owner_upi_vpa": vpa,
        "owner_scanner_paise": 0,
        "notes": _one(form, "notes"),
        "collection_json": "",
        "lines": expenses + handovers + customer_cash,
    }


def _replace_lines(conn: sqlite3.Connection, settlement_id: int, lines: list[dict]) -> None:
    conn.execute("DELETE FROM agent_settlement_lines WHERE settlement_id = ?", (settlement_id,))
    for line in lines:
        cid = line.get("customer_id")
        conn.execute(
            "INSERT INTO agent_settlement_lines("
            "settlement_id, kind, line_date, customer_name, comment, amount_paise, "
            "customer_id, provider, area) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                settlement_id,
                line["kind"],
                line.get("line_date") or "",
                line.get("customer_name") or "",
                line.get("comment") or "",
                int(line.get("amount_paise") or 0),
                int(cid) if cid else None,
                (line.get("provider") or "").strip().lower(),
                line.get("area") or "",
            ),
        )


def save(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    payload: dict,
    actor: str = "",
    settlement_id: int | None = None,
) -> int:
    stamp = now_iso()
    attach_live_collections(conn, int(agent_id), payload)
    totals = compute_totals(payload, payload.get("lines") or [])
    if (
        int(payload.get("cable_cash_paise") or 0) == 0
        and int(payload.get("cable_upi_agent_paise") or 0) == 0
        and int(payload.get("owner_upi_paise") or 0) == 0
        and int(payload.get("owner_scanner_paise") or 0) == 0
        and int((payload.get("collection") or {}).get("total_paise") or 0) == 0
        and int(totals.get("expenses_paise") or 0) == 0
        and int(totals.get("handed_paise") or 0) == 0
        and int(totals.get("customer_cash_paise") or 0) == 0
    ):
        raise SettlementError(
            "No collections, expenses, cash collected, or cash/UPI handover in this date range."
        )
    values = (
        int(agent_id),
        payload["period_from"],
        payload["period_to"],
        payload["settled_on"],
        int(payload["cable_cash_paise"]),
        int(payload["cable_upi_agent_paise"]),
        int(payload["owner_upi_paise"]),
        payload.get("owner_upi_vpa") or "",
        int(payload["owner_scanner_paise"]),
        payload.get("notes") or "",
        payload.get("collection_json") or "",
        actor or "",
        stamp,
        stamp,
    )
    if settlement_id:
        conn.execute(
            "UPDATE agent_settlements SET agent_id = ?, period_from = ?, period_to = ?, "
            "settled_on = ?, cable_cash_paise = ?, cable_upi_agent_paise = ?, "
            "owner_upi_paise = ?, owner_upi_vpa = ?, owner_scanner_paise = ?, "
            "notes = ?, collection_json = ?, updated_at = ? WHERE id = ?",
            values[:11] + (stamp, int(settlement_id)),
        )
        sid = int(settlement_id)
    else:
        cursor = conn.execute(
            "INSERT INTO agent_settlements("
            "agent_id, period_from, period_to, settled_on, cable_cash_paise, "
            "cable_upi_agent_paise, owner_upi_paise, owner_upi_vpa, owner_scanner_paise, "
            "notes, collection_json, created_by, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        sid = int(cursor.lastrowid)
    _replace_lines(conn, sid, payload.get("lines") or [])
    return sid


def delete(conn: sqlite3.Connection, settlement_id: int) -> None:
    proofs = list_proofs(conn, settlement_id)
    conn.execute("DELETE FROM agent_settlement_lines WHERE settlement_id = ?", (settlement_id,))
    conn.execute("DELETE FROM agent_settlement_proofs WHERE settlement_id = ?", (settlement_id,))
    conn.execute("DELETE FROM agent_settlements WHERE id = ?", (settlement_id,))
    for proof in proofs:
        _unlink_proof_file(proof.get("stored_name") or "")


def _load_lines(conn: sqlite3.Connection, settlement_ids: list[int]) -> dict[int, list[dict]]:
    if not settlement_ids:
        return {}
    placeholders = ",".join("?" * len(settlement_ids))
    rows = conn.execute(
        f"SELECT * FROM agent_settlement_lines WHERE settlement_id IN ({placeholders}) "
        "ORDER BY line_date, id",
        settlement_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {sid: [] for sid in settlement_ids}
    for row in rows:
        out[int(row["settlement_id"])].append(dict(row))
    return out


def get(conn: sqlite3.Connection, settlement_id: int) -> dict | None:
    row = conn.execute(
        "SELECT s.*, a.name AS agent_name, a.username AS agent_username "
        "FROM agent_settlements s JOIN agents a ON a.id = s.agent_id WHERE s.id = ?",
        (settlement_id,),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["lines"] = _load_lines(conn, [int(row["id"])]).get(int(row["id"]), [])
    item["proofs"] = list_proofs(conn, int(row["id"]))
    item["proof_count"] = len(item["proofs"])
    item["collection_snap"] = _parse_snap(item) or {}
    item["collection"] = collection_for_agent(
        conn,
        agent_id=int(item["agent_id"]),
        day_from=item["period_from"],
        day_to=item["period_to"],
    )
    return _with_totals(item)


def list_reports(
    conn: sqlite3.Connection,
    *,
    day_from: str,
    day_to: str,
    agent_id: int | None = None,
) -> list[dict]:
    sql = (
        "SELECT s.*, a.name AS agent_name, a.username AS agent_username, a.role AS agent_role, "
        "(SELECT COUNT(*) FROM agent_settlement_proofs p WHERE p.settlement_id = s.id) "
        "AS proof_count "
        "FROM agent_settlements s JOIN agents a ON a.id = s.agent_id "
        "WHERE (NOT (s.period_to < ? OR s.period_from > ?) "
        "       OR (s.settled_on >= ? AND s.settled_on <= ?)) "
    )
    params: list = [day_from, day_to, day_from, day_to]
    if agent_id:
        sql += "AND s.agent_id = ? "
        params.append(int(agent_id))
    sql += "ORDER BY a.name COLLATE NOCASE, s.settled_on DESC, s.id DESC"
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    lines = _load_lines(conn, [int(row["id"]) for row in rows])
    out = []
    for row in rows:
        row["lines"] = lines.get(int(row["id"]), [])
        out.append(_with_totals(row))
    return out


def cash_recipient_options(conn: sqlite3.Connection, owner_name: str = "") -> list[str]:
    """People cash has been handed to before, plus the owner."""
    names: list[str] = []
    seen: set[str] = set()
    owner = (owner_name or owner_display_name()).strip()
    if owner:
        names.append(owner)
        seen.add(owner.lower())
    rows = conn.execute(
        "SELECT DISTINCT trim(customer_name) AS name FROM agent_settlement_lines "
        "WHERE kind = ? AND trim(customer_name) != '' "
        "ORDER BY name COLLATE NOCASE",
        (LINE_CASH_HANDOVER,),
    ).fetchall()
    for row in rows:
        name = (row["name"] or "").strip()
        key = name.lower()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    return names


def search_cash_customers(conn: sqlite3.Connection, query: str, *, limit: int = 12) -> list[dict]:
    """Name/phone search across every ISP — used when an agent logs field cash."""
    from .upstream.providers import PROVIDER_LABELS_VIEW
    from . import repo

    text = (query or "").strip()
    if len(text) < 2:
        return []
    like = f"%{text}%"
    clause, params = repo.customer_text_search_clause(
        text,
        extra_or=(
            "EXISTS (SELECT 1 FROM connections cn WHERE cn.customer_id = c.id "
            "AND (cn.upstream_id LIKE ? OR cn.card_number LIKE ?))"
        ),
        extra_params=[like, like],
    )
    rows = conn.execute(
        "SELECT c.id, c.name, c.phone, c.sub_area, c.area, "
        "(SELECT GROUP_CONCAT(DISTINCT cn.provider) FROM connections cn "
        " WHERE cn.customer_id = c.id) AS providers "
        "FROM customers c WHERE " + clause + " "
        "ORDER BY c.name COLLATE NOCASE LIMIT ?",
        [*params, int(limit)],
    ).fetchall()
    out = []
    for row in rows:
        providers = [
            p.strip().lower()
            for p in str(row["providers"] or "").split(",")
            if p.strip()
        ]
        preferred = "railtel" if "railtel" in providers else (providers[0] if providers else "")
        out.append(
            {
                "id": int(row["id"]),
                "name": row["name"] or "",
                "phone": row["phone"] or "",
                "area": (row["sub_area"] or row["area"] or "").strip(),
                "providers": providers,
                "provider_labels": [
                    PROVIDER_LABELS_VIEW.get(p, p.title()) for p in providers
                ],
                "preferred_provider": preferred,
            }
        )
    return out


def handover_statement(
    conn: sqlite3.Connection,
    *,
    day_from: str,
    day_to: str,
    agent_id: int | None = None,
) -> dict:
    """UPI + cash handovers in the date range, as a statement."""
    reports = list_reports(conn, day_from=day_from, day_to=day_to, agent_id=agent_id)
    rows: list[dict] = []
    by_to: dict[str, int] = {}
    cash_paise = 0
    upi_paise = 0
    for report in reports:
        for line in report.get("handover_lines") or []:
            amount = int(line.get("amount_paise") or 0)
            if amount <= 0:
                continue
            kind = line.get("kind") or ""
            handed_to = (line.get("handed_to") or "").strip()
            item = {
                "settlement_id": int(report["id"]),
                "settled_on": (line.get("line_date") or report.get("settled_on") or "")[:10],
                "agent_id": int(report["agent_id"]),
                "agent_name": report.get("agent_name") or "",
                "kind": kind,
                "amount_paise": amount,
                "handed_to": handed_to,
                "vpa": (line.get("vpa") or "").strip(),
                "is_upi": bool(line.get("is_upi")),
                "is_cash": bool(line.get("is_cash")),
            }
            rows.append(item)
            if item["is_upi"]:
                upi_paise += amount
            else:
                cash_paise += amount
            label = handed_to or ("UPI" if item["is_upi"] else "Cash")
            by_to[label] = int(by_to.get(label) or 0) + amount
    rows.sort(key=lambda row: (row["settled_on"], row["agent_name"].lower(), row["settlement_id"]))
    recipients = [
        {"name": name, "amount_paise": paise}
        for name, paise in sorted(by_to.items(), key=lambda pair: pair[0].lower())
    ]
    return {
        "rows": rows,
        "recipients": recipients,
        "cash_paise": cash_paise,
        "upi_paise": upi_paise,
        "total_paise": cash_paise + upi_paise,
    }


def _add_totals(left: dict, right: dict) -> dict:
    keys = (
        "cable_cash_paise",
        "cable_upi_agent_paise",
        "internet_paise",
        "inflow_paise",
        "owner_upi_paise",
        "owner_scanner_paise",
        "settled_paise",
        "meals_paise",
        "parcel_paise",
        "other_exp_paise",
        "expenses_paise",
        "handed_upi_paise",
        "handed_cash_paise",
        "handed_paise",
        "should_pay_paise",
        "balance_paise",
    )
    for key in keys:
        left[key] = int(left.get(key) or 0) + int(right.get(key) or 0)
    left["report_count"] = int(left.get("report_count") or 0) + 1
    return left


def agent_summaries(
    conn: sqlite3.Connection,
    *,
    day_from: str,
    day_to: str,
    only_agent_id: int | None = None,
) -> dict:
    reports = list_reports(conn, day_from=day_from, day_to=day_to, agent_id=only_agent_id)
    agents: dict[int, dict] = {}
    office = empty_totals()
    office["report_count"] = 0
    for report in reports:
        aid = int(report["agent_id"])
        bucket = agents.get(aid)
        if bucket is None:
            bucket = {
                "id": aid,
                "name": report["agent_name"],
                "username": report["agent_username"],
                "reports": [],
                **empty_totals(),
                "report_count": 0,
            }
            agents[aid] = bucket
        bucket["reports"].append(report)
        _add_totals(bucket, report)
        _add_totals(office, report)
    rows = sorted(agents.values(), key=lambda row: row["name"].lower())
    return {
        "day_from": day_from,
        "day_to": day_to,
        "rows": rows,
        "office": office,
        "reports": reports,
    }


def active_agents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, username FROM agents WHERE active = 1 AND role != 'admin' "
        "ORDER BY name COLLATE NOCASE"
    ).fetchall()


def can_edit(viewer: dict | None, report: dict) -> bool:
    if not viewer:
        return False
    if auth.sees_all_settlements(viewer):
        return True
    return int(viewer.get("id") or 0) == int(report.get("agent_id") or 0)


def proof_dir() -> Path:
    path = settings.screenshot_dir.parent / "settlement_proofs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_proofs(conn: sqlite3.Connection, settlement_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM agent_settlement_proofs WHERE settlement_id = ? ORDER BY id",
        (settlement_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_proof(conn: sqlite3.Connection, settlement_id: int, proof_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_settlement_proofs WHERE id = ? AND settlement_id = ?",
        (proof_id, settlement_id),
    ).fetchone()
    return dict(row) if row else None


def proof_file_path(stored_name: str) -> Path:
    name = Path(stored_name or "").name
    return proof_dir() / name


def _unlink_proof_file(stored_name: str) -> None:
    path = proof_file_path(stored_name)
    try:
        if path.is_file() and path.resolve().parent == proof_dir().resolve():
            path.unlink()
    except OSError:
        pass


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    if not data or len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def parse_remove_proof_ids(form) -> list[int]:
    out = []
    for raw in _listed(form, "remove_proof"):
        if raw.isdigit():
            out.append(int(raw))
    return out


async def _accept_image_upload(
    upload,
    *,
    kind: str,
    caption: str,
    accepted: list[dict],
) -> int:
    filename = str(getattr(upload, "filename", "") or "").strip()
    if not filename:
        return 0
    if len(accepted) >= _MAX_PROOFS_PER_SAVE:
        return 1
    raw = await upload.read()
    if not raw or len(raw) > MAX_PROOF_BYTES:
        return 1
    sniffed = _sniff_image(raw)
    if sniffed is None:
        return 1
    ext, content_type = sniffed
    kind_key = (kind or "other").strip().lower()
    if kind_key not in PROOF_KIND_KEYS:
        kind_key = "other"
    accepted.append(
        {
            "kind": kind_key,
            "caption": (caption or "")[:200],
            "original_name": filename[:180],
            "content_type": content_type,
            "ext": ext,
            "data": raw,
        }
    )
    return 0


async def read_proof_uploads(form) -> tuple[list[dict], int]:
    """Return (accepted image payloads, skipped count) from a multipart form."""
    kinds = _listed(form, "proof_kind")
    captions = _listed(form, "proof_caption")
    files = form.getlist("proof_file") if hasattr(form, "getlist") else []
    upi_files = form.getlist("upi_proof_file") if hasattr(form, "getlist") else []
    accepted: list[dict] = []
    skipped = 0
    default_kind = (kinds[0] if kinds else "other").strip().lower()
    if default_kind not in PROOF_KIND_KEYS:
        default_kind = "other"
    default_caption = captions[0] if captions else ""
    for i, upload in enumerate(files):
        kind = (kinds[i] if i < len(kinds) else default_kind).strip().lower()
        caption = captions[i] if i < len(captions) else default_caption
        skipped += await _accept_image_upload(
            upload, kind=kind, caption=caption, accepted=accepted
        )
    for upload in upi_files:
        skipped += await _accept_image_upload(
            upload,
            kind="upi_handover",
            caption="UPI handover screenshot",
            accepted=accepted,
        )
    return accepted, skipped


def save_proofs(
    conn: sqlite3.Connection,
    settlement_id: int,
    uploads: list[dict],
    *,
    actor: str = "",
) -> int:
    stamp = now_iso()
    saved = 0
    for item in uploads:
        stored = f"{int(settlement_id)}_{uuid.uuid4().hex}{item['ext']}"
        dest = proof_file_path(stored)
        dest.write_bytes(item["data"])
        conn.execute(
            "INSERT INTO agent_settlement_proofs("
            "settlement_id, kind, caption, stored_name, original_name, content_type, "
            "size_bytes, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(settlement_id),
                item["kind"],
                item.get("caption") or "",
                stored,
                item.get("original_name") or "",
                item.get("content_type") or "image/jpeg",
                len(item["data"]),
                actor or "",
                stamp,
            ),
        )
        saved += 1
    return saved


def delete_proof(conn: sqlite3.Connection, settlement_id: int, proof_id: int) -> bool:
    proof = get_proof(conn, settlement_id, proof_id)
    if proof is None:
        return False
    conn.execute(
        "DELETE FROM agent_settlement_proofs WHERE id = ? AND settlement_id = ?",
        (proof_id, settlement_id),
    )
    _unlink_proof_file(proof.get("stored_name") or "")
    return True


def delete_proofs(conn: sqlite3.Connection, settlement_id: int, proof_ids: list[int]) -> int:
    removed = 0
    for proof_id in proof_ids:
        if delete_proof(conn, settlement_id, proof_id):
            removed += 1
    return removed
