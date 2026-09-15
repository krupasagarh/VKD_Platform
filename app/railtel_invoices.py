"""Railtel portal invoice PDFs downloaded from Sub Invoice."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import settings
from .messaging import invoice_whatsapp_target_phone, service_label
from .money import now_iso


def save_invoice_record(
    conn: sqlite3.Connection,
    *,
    customer_id: int,
    connection_id: int | None,
    job_id: int | None,
    upstream_id: str,
    raw: dict,
    stamp: str,
) -> int:
    file_path = str(raw.get("file_path") or "").strip()
    file_name = str(raw.get("file_name") or Path(file_path).name if file_path else "invoice.pdf")
    return int(
        conn.execute(
            "INSERT INTO railtel_invoices("
            "customer_id, connection_id, job_id, upstream_id, invoice_no, "
            "receipt_date, gross_amount, file_path, file_name, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                connection_id,
                job_id,
                upstream_id,
                str(raw.get("invoice_no") or "").strip(),
                str(raw.get("receipt_date") or "").strip(),
                str(raw.get("gross_amount") or "").strip(),
                file_path,
                file_name,
                stamp,
            ),
        ).lastrowid
    )


def invoice_public_url(invoice_id: int, base_url: str = "") -> str:
    root = (base_url or settings.public_base_url or "").strip().rstrip("/")
    if not root:
        return f"/railtel-invoices/{invoice_id}/pdf"
    return f"{root}/railtel-invoices/{invoice_id}/pdf"


def invoice_whatsapp_caption(customer_name: str, invoice_no: str) -> str:
    name = (customer_name or "Customer").strip() or "Customer"
    inv = (invoice_no or "bill").strip()
    service = service_label("railtel")
    return (
        f"Hi {name},\n\n"
        f"Please find your {service} bill ({inv}).\n\n"
        f"Thanks,\n"
        f"VK DIGITAL"
    )


def _record_invoice_whatsapp_result(
    conn: sqlite3.Connection,
    invoice_id: int,
    result: dict,
) -> None:
    stamp = now_iso()
    if result.get("ok"):
        conn.execute(
            "UPDATE railtel_invoices SET whatsapp_sent_at = ?, whatsapp_error = '' WHERE id = ?",
            (stamp, invoice_id),
        )
    else:
        err = str(result.get("error") or "WhatsApp send failed")
        conn.execute(
            "UPDATE railtel_invoices SET whatsapp_error = ? WHERE id = ?",
            (err[:500], invoice_id),
        )


def send_invoice_whatsapp_work(
    invoice_id: int,
    *,
    customer_name: str,
    customer_phone: str | None,
) -> dict:
    """Run Playwright WhatsApp send in a worker thread (not inside asyncio).

    Reads invoice path, sends the PDF, then updates the DB. Safe to call via
    asyncio.to_thread() from FastAPI routes or directly from the job worker.
    """
    from .db import connection, transaction
    from .whatsapp_send import send_whatsapp_document

    with connection() as conn:
        row = conn.execute(
            "SELECT file_path, invoice_no FROM railtel_invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
    if row is None:
        return {"ok": False, "error": "Invoice not found."}

    phone = invoice_whatsapp_target_phone(customer_phone)
    caption = invoice_whatsapp_caption(customer_name, row["invoice_no"])
    result = send_whatsapp_document(phone, row["file_path"], caption)

    with transaction() as conn:
        _record_invoice_whatsapp_result(conn, invoice_id, result)
    return result


def send_invoice_whatsapp(
    conn: sqlite3.Connection,
    invoice_id: int,
    *,
    customer_name: str,
    customer_phone: str | None,
) -> dict:
    """Legacy wrapper — prefer send_invoice_whatsapp_work outside asyncio."""
    row = conn.execute(
        "SELECT 1 FROM railtel_invoices WHERE id = ?", (invoice_id,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": "Invoice not found."}
    return send_invoice_whatsapp_work(
        invoice_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
    )
