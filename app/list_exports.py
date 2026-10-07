"""Build CSV rows for each table listing."""
from __future__ import annotations

from . import repo
from .csv_export import csv_response
from .money import days_until, fmt_date, fmt_rupees
from .upstream.providers import provider_label, providers_csv_label


def _provider_labels(raw: str | None) -> str:
    return providers_csv_label(raw)


def _late_days(expiry) -> str:
    left = days_until(expiry)
    if left is None or left >= 0:
        return ""
    return str(-left)


def customers_csv(rows) -> Response:
    data = []
    for c in rows:
        data.append([
            c["name"] or "",
            c["code"] or "",
            c["phone"] or "",
            c["sub_area"] or c["area"] or "",
            c["address"] or "",
            c["railtel_ids"] or "",
            c["hathway_ids"] or "",
            c["iptv_ids"] or "",
            c["ott_ids"] or "",
            c["status"] or "",
            f"{c['active_count']}/{c['connection_count']}",
            _provider_labels(c["providers"]),
            fmt_date(c["next_expiry"]) if c["next_expiry"] else "",
            _late_days(c["next_expiry"]),
            fmt_rupees(c["outstanding_paise"]) if c["outstanding_paise"] else "",
        ])
    return csv_response(
        "customers.csv",
        [
            "Customer",
            "Code",
            "Phone",
            "Area",
            "Location",
            "Railtel ID",
            "Hathway STB",
            "ANT IPTV",
            "SmartPlay OTT",
            "Status",
            "Connections",
            "Providers",
            "Next expiry",
            "Days late",
            "Outstanding",
        ],
        data,
    )


def prepaid_csv(rows, *, filename: str, provider: str) -> Response:
    label = provider_label(provider, default=provider)
    data = []
    for r in rows:
        data.append([
            r["customer_name"] or "",
            r["customer_code"] or "",
            r["phone"] or "",
            r["sub_area"] or r["area"] or "",
            r["upstream_id"] or "",
            r["package_name"] or r["upstream_plan_name"] or "",
            fmt_date(r["expiry_date"]) if r["expiry_date"] else "",
            _late_days(r["expiry_date"]),
            r["status"] or "",
        ])
    return csv_response(
        filename,
        [
            "Customer",
            "Code",
            "Phone",
            "Area",
            "Login / mobile",
            "Package",
            "Expiry",
            "Days late",
            "Status",
        ],
        data,
    )


def stbs_csv(rows) -> Response:
    data = []
    for r in rows:
        data.append([
            r["upstream_id"] or "",
            r["card_number"] or "",
            r["customer_name"] or "",
            r["customer_code"] or "",
            r["phone"] or "",
            r["sub_area"] or "",
            r["package_name"] or r["upstream_plan_name"] or "",
            r["status"] or "",
            fmt_date(r["expiry_date"]) if r["expiry_date"] else "",
            r.get("problem") or "",
        ])
    return csv_response(
        "hathway-stbs.csv",
        ["STB", "VC", "Customer", "Code", "Phone", "Area", "Plan", "Status", "Expiry", "Note"],
        data,
    )


def payments_csv(rows) -> Response:
    data = []
    for p in rows:
        data.append([
            p["receipt_no"] or "",
            p["customer_name"] or "",
            p["customer_code"] or "",
            p["paid_at"] or "",
            p["mode"] or "",
            p["collected_by"] or "",
            p["reference"] or "",
            fmt_rupees(p["amount_paise"]),
        ])
    return csv_response(
        "payments.csv",
        ["Receipt", "Customer", "Code", "When", "Mode", "By", "Reference", "Amount"],
        data,
    )


def followup_csv(rows) -> Response:
    data = []
    for b in rows:
        due = int(b["total_paise"] or 0) - int(b["paid_paise"] or 0)
        data.append([
            b["customer_name"] or "",
            b["customer_code"] or "",
            b["phone"] or "",
            b["upstream_id"] or "",
            provider_label(b["provider"] or "", default=b["provider"] or ""),
            b["followup_kind"] or "",
            b["package_name"] or "",
            b["created_at"] or "",
            fmt_rupees(due),
            b["notes"] or "",
        ])
    return csv_response(
        "payment-followup.csv",
        [
            "Customer",
            "Code",
            "Phone",
            "Connection",
            "Provider",
            "Kind",
            "Plan",
            "When",
            "Still due",
            "Notes",
        ],
        data,
    )


def bills_csv(rows) -> Response:
    data = []
    for b in rows:
        data.append([
            b["bill_no"] or "",
            b["customer_name"] or "",
            b["customer_code"] or "",
            b["phone"] or "",
            b["package_name"] or "",
            fmt_date(b["period_start"]) if b["period_start"] else "",
            fmt_date(b["period_end"]) if b["period_end"] else "",
            fmt_date(b["due_date"]) if b["due_date"] else "",
            fmt_rupees(b["total_paise"]),
            fmt_rupees(b["paid_paise"]),
            b["status"] or "",
            b["source"] or "",
        ])
    return csv_response(
        "bills.csv",
        [
            "Bill",
            "Customer",
            "Code",
            "Phone",
            "Plan",
            "Period start",
            "Period end",
            "Due",
            "Total",
            "Paid",
            "Status",
            "Source",
        ],
        data,
    )


def jobs_csv(rows) -> Response:
    data = []
    for j in rows:
        data.append([
            j["id"],
            j["provider"] or "",
            provider_label(j["provider"] or "", default=j["provider"] or ""),
            j["action"] or "",
            j["status"] or "",
            j["customer_name"] or "",
            j["customer_code"] or "",
            j["upstream_id"] or "",
            j["bill_no"] or "",
            repo.job_created_by_label(j),
            j["created_at"] or "",
            j["completed_at"] or "",
            j["error"] or "",
        ])
    return csv_response(
        "provider-jobs.csv",
        [
            "Job",
            "Provider",
            "Provider label",
            "Action",
            "Status",
            "Customer",
            "Code",
            "Connection",
            "Bill",
            "Created by",
            "Created",
            "Completed",
            "Error",
        ],
        data,
    )


def packages_csv(rows) -> Response:
    data = []
    for p in rows:
        data.append([
            provider_label(p["provider"] or "", default=p["provider"] or ""),
            p["name"] or "",
            fmt_rupees(p["price_paise"]),
            p["validity_days"] or "",
            p["gst_percentage"] or "",
            "yes" if p["active"] else "no",
            p["subscriber_count"] or 0,
            p["notes"] or "",
        ])
    return csv_response(
        "plans.csv",
        ["Provider", "Plan", "Price", "Validity days", "GST %", "Active", "Connections", "Notes"],
        data,
    )


def complaints_csv(rows) -> Response:
    data = []
    for c in rows:
        data.append([
            c["id"],
            c["customer_name"] or "",
            c["title"] or "",
            c["status"] or "",
            c["agent_name"] or "",
            c["created_at"] or "",
            c["updated_at"] or "",
            c["details"] or "",
        ])
    return csv_response(
        "complaints.csv",
        ["Id", "Customer", "Title", "Status", "Assigned", "Created", "Updated", "Details"],
        data,
    )


def activity_csv(rows, *, with_location: bool = False) -> Response:
    data = []
    for a in rows:
        line = [
            a["at"] or "",
            a["actor"] or "",
            a["kind"] or "",
            a["message"] or "",
            a["customer_name"] or "",
        ]
        if with_location:
            has_loc = a["lat"] is not None and a["lng"] is not None
            line += [
                f"{a['lat']:.6f}" if has_loc else "",
                f"{a['lng']:.6f}" if has_loc else "",
                a["loc_at"] or "",
                f"https://maps.google.com/?q={a['lat']},{a['lng']}" if has_loc else "",
            ]
        data.append(line)
    headers = ["When", "Who", "Kind", "Message", "Customer"]
    if with_location:
        headers += ["Latitude", "Longitude", "Location time", "Map"]
    return csv_response("activity.csv", headers, data)


def railtel_online_csv(rows) -> Response:
    data = []
    for r in rows:
        data.append([
            r.get("username") or "",
            r.get("customer_name") or "",
            r.get("customer_code") or "",
            r.get("start_at") or r.get("start_time") or "",
            r.get("total_time") or "",
            r.get("framed_ip") or "",
            r.get("mac") or "",
            r.get("upload_mb") or "",
            r.get("download_mb") or "",
            r.get("total_mb") or "",
        ])
    return csv_response(
        "railtel-online.csv",
        [
            "Username",
            "Customer",
            "Code",
            "Active since",
            "Duration",
            "IP",
            "MAC",
            "Upload MB",
            "Download MB",
            "Total MB",
        ],
        data,
    )
