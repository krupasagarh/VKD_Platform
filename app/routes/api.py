"""JSON API.

Mirrors the web actions so the Telegram bot (or any script) can drive the platform
later without scraping HTML.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth, billing, repo
from ..db import connection, log_activity, transaction
from ..money import from_paise, now_iso, to_paise
from ..upstream import jobs as job_queue
from ..upstream.providers import PROVIDER_ACTIONS

router = APIRouter(prefix="/api")


def _require(request: Request, permission: str) -> dict:
    agent = getattr(request.state, "agent", None)
    if not auth.can(agent, permission):
        raise HTTPException(status_code=403, detail="Not allowed")
    return agent or {}


def _rows(rows) -> list[dict]:
    return [dict(row) for row in rows]


def _money(row: dict, *keys: str) -> dict:
    for key in keys:
        if key in row and row[key] is not None:
            row[key.replace("_paise", "_rupees")] = float(from_paise(row[key]))
    return row


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #

@router.get("/stats")
async def api_stats():
    with connection() as conn:
        stats = repo.dashboard_stats(conn)
    stats["by_provider"] = _rows(stats["by_provider"])
    return _money(stats, "outstanding_paise", "collected_today_paise", "collected_month_paise")


@router.get("/customers")
async def api_customers(q: str = "", provider: str = "", view: str = "", area: str = "", page: int = 1):
    with connection() as conn:
        result = repo.search_customers(
            conn, query=q, provider=provider, area=area, view=view, page=page
        )
    return {
        "total": result["total"],
        "page": result["page"],
        "pages": result["pages"],
        "customers": [_money(dict(r), "outstanding_paise", "collected_paise") for r in result["rows"]],
    }


@router.get("/customers/{customer_id}")
async def api_customer(customer_id: int):
    with connection() as conn:
        customer = repo.get_customer(conn, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        payload = {
            "customer": _money(dict(customer), "outstanding_paise", "collected_paise"),
            "ledger": _money(billing.customer_ledger(conn, customer_id),
                             "outstanding_paise", "credit_paise", "net_due_paise", "billed_paise",
                             "collected_paise"),
            "connections": _rows(repo.customer_connections(conn, customer_id)),
            "bills": _rows(repo.customer_bills(conn, customer_id)),
            "payments": _rows(repo.customer_payments(conn, customer_id)),
            "jobs": _rows(repo.customer_jobs(conn, customer_id)),
        }
    return payload


@router.get("/lookup")
async def api_lookup(q: str):
    """Find a connection by STB number, Railtel login, VC number or phone."""
    text = (q or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="q is required")
    like = f"%{text}%"
    with connection() as conn:
        rows = conn.execute(
            "SELECT cn.*, c.name AS customer_name, c.code AS customer_code, c.phone, "
            "       p.name AS package_name, p.price_paise AS package_price_paise "
            "FROM connections cn JOIN customers c ON c.id = cn.customer_id "
            "LEFT JOIN packages p ON p.id = cn.package_id "
            "WHERE cn.upstream_id LIKE ? OR cn.card_number LIKE ? OR c.phone LIKE ? "
            "   OR c.name LIKE ? OR c.code LIKE ? LIMIT 25",
            (like, like, like, like, like),
        ).fetchall()
    return {"matches": _rows(rows)}


@router.get("/jobs")
async def api_jobs(status: str = "", limit: int = 100):
    with connection() as conn:
        rows = repo.list_jobs(conn, status=status, limit=limit)
    return {"jobs": _rows(rows)}


@router.get("/bills")
async def api_bills(status: str = "open", limit: int = 100):
    with connection() as conn:
        rows = repo.list_bills(conn, status=status, limit=limit)
    return {"bills": [_money(dict(r), "total_paise", "paid_paise") for r in rows]}


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #

class PaymentIn(BaseModel):
    amount: float = Field(gt=0)
    connection_id: int | None = None
    mode: str = "cash"
    reference: str = ""
    notes: str = ""
    paid_at: str | None = None
    renew: bool = False
    auto_confirm: bool = False


@router.post("/customers/{customer_id}/payments")
async def api_record_payment(request: Request, customer_id: int, body: PaymentIn):
    agent = _require(request, "payments")
    if body.renew:
        _require(request, "portal_actions")
    with transaction() as conn:
        exists = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Customer not found")

        payment_id = billing.record_payment(
            conn,
            customer_id=customer_id,
            connection_id=body.connection_id,
            amount_paise=to_paise(body.amount),
            mode=body.mode,
            reference=body.reference,
            collected_by=agent.get("name"),
            collected_agent_id=int(agent["id"]) if agent.get("id") else None,
            paid_at=body.paid_at or now_iso(),
            notes=body.notes,
        )
        billing.reconcile_customer(conn, customer_id)
        receipt = conn.execute(
            "SELECT receipt_no FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()["receipt_no"]
        log_activity(conn, "payment_recorded", f"Payment {receipt} received (API)",
                     customer_id=customer_id, connection_id=body.connection_id)

        job_id = None
        if body.renew and body.connection_id:
            try:
                job_id = job_queue.enqueue_job(
                    conn,
                    connection_id=body.connection_id,
                    action="renew",
                    payment_id=payment_id,
                    needs_confirmation=not body.auto_confirm,
                )
            except job_queue.RenewNotAllowed as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        ledger = billing.customer_ledger(conn, customer_id)

    return {
        "payment_id": payment_id,
        "receipt_no": receipt,
        "job_id": job_id,
        "ledger": _money(ledger, "outstanding_paise", "credit_paise", "net_due_paise"),
    }


class JobIn(BaseModel):
    action: str
    auto_confirm: bool = False


@router.post("/connections/{connection_id}/jobs")
async def api_create_job(request: Request, connection_id: int, body: JobIn):
    _require(request, "portal_actions")
    with transaction() as conn:
        row = conn.execute(
            "SELECT provider FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        if body.action not in PROVIDER_ACTIONS.get(row["provider"], ()):
            raise HTTPException(
                status_code=400,
                detail=f"{row['provider']} does not support action '{body.action}'",
            )
        needs = (not body.auto_confirm) and row["provider"] not in ("iptv", "ott")
        try:
            job_id = job_queue.enqueue_job(
                conn,
                connection_id=connection_id,
                action=body.action,
                needs_confirmation=needs,
            )
        except job_queue.RenewNotAllowed as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job_id": job_id, "status": "awaiting_confirm" if needs else "queued"}


@router.post("/jobs/{job_id}/otp")
async def api_submit_otp(request: Request, job_id: int):
    _require(request, "portal_actions")
    body = await request.json()
    code = str((body or {}).get("otp") or (body or {}).get("code") or "")
    with transaction() as conn:
        if not job_queue.submit_otp(conn, job_id, code):
            raise HTTPException(
                status_code=409,
                detail="OTP must be 6 digits and the job must be waiting for it",
            )
    return {"job_id": job_id, "status": "running"}


@router.post("/jobs/{job_id}/confirm")
async def api_confirm_job(request: Request, job_id: int):
    _require(request, "portal_actions")
    with transaction() as conn:
        if not job_queue.confirm_job(conn, job_id):
            raise HTTPException(status_code=409, detail="Job is not awaiting confirmation")
    return {"job_id": job_id, "status": "queued"}


@router.post("/jobs/{job_id}/cancel")
async def api_cancel_job(request: Request, job_id: int):
    _require(request, "portal_actions")
    with transaction() as conn:
        if not job_queue.cancel_job(conn, job_id):
            raise HTTPException(status_code=409, detail="Job cannot be cancelled now")
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/jobs/{job_id}/retry")
async def api_retry_job(request: Request, job_id: int):
    _require(request, "portal_actions")
    with transaction() as conn:
        if not job_queue.retry_job(conn, job_id):
            raise HTTPException(status_code=409, detail="Job cannot be retried now")
    return {"job_id": job_id, "status": "queued"}


@router.post("/bills/run-checker")
async def api_run_bill_checker(request: Request):
    _require(request, "bills")
    with transaction() as conn:
        summary = billing.run_bill_checker(conn)
    return summary
