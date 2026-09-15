"""End-to-end check of the money flow, against a throwaway database.

Walks the exact path the operator will use:

    customer + connection + plan
      -> record payment (renew requested)
      -> job sits in awaiting_confirm
      -> confirm
      -> worker renews on the provider (simulate mode)
      -> renewal bill created, expiry advanced, payment allocated, dues cleared

Run: python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Point at a scratch DB and force simulate mode before the app config loads.
_tmp = Path(tempfile.mkdtemp(prefix="vkp_smoke_"))
os.environ["VK_PLATFORM_DB"] = str(_tmp / "smoke.db")
os.environ["VK_PLATFORM_UPSTREAM_MODE"] = "simulate"
os.environ["VK_PLATFORM_WORKER_ENABLED"] = "0"

from app import auth, billing, bix_history, bix_sync, repo  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import connection as db_connection, init_db, transaction  # noqa: E402
from app.money import add_days, fmt_datetime_human, now_iso, to_paise, today  # noqa: E402
from app.plans import price_plan  # noqa: E402
from app.upstream import jobs as job_queue  # noqa: E402
from app.upstream.providers import link_state_from  # noqa: E402

# Belt and braces. The fixtures below use invented ids, so a live run would drive real
# Railtel/Hathway logins looking for boxes that do not exist.
if settings.is_live:
    raise SystemExit(
        "smoke_test refuses to run in live mode — it would contact the real provider "
        "portals with test data. Check that nothing sets VK_PLATFORM_UPSTREAM_MODE=live "
        "in the environment."
    )
if settings.db_path.name != "smoke.db":
    raise SystemExit(f"smoke_test refuses to run against {settings.db_path}")

PASS = "PASS"
FAIL = "FAIL"
_failures: list[str] = []

TODAY = today()


def day(offset: int) -> str:
    """A date relative to today, so the expectations never go stale."""
    return add_days(TODAY, offset).strftime("%Y-%m-%d")


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    if not ok:
        _failures.append(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  [{PASS if ok else FAIL}] {label}: {actual}")


def main() -> int:
    init_db()
    print(f"Scratch database: {os.environ['VK_PLATFORM_DB']}\n")

    # --- Plan pricing rules -------------------------------------------------
    print("House location parse")
    from app.routes.pages import parse_geo

    check("pair", parse_geo("13.26120, 76.47840"), (13.2612, 76.4784))
    check("maps at", parse_geo("https://www.google.com/maps/@13.26,76.48,17z")[0], 13.26)

    print("Railtel portal plan parse")
    from app.config import VK_AGENT_DIR

    if str(VK_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(VK_AGENT_DIR))
    from portal import parse_railtel_plan_name_from_text  # noqa: E402

    check(
        "plan name from subscriber details",
        parse_railtel_plan_name_from_text(
            "Username : ka.shashi\nPlan Name : FUP100Mbps-2Mbps 3.5TB\n"
            "Subscription Expiry : 2027-04-11 23:59:59"
        ),
        "FUP100Mbps-2Mbps 3.5TB",
    )
    check("empty details have no plan", parse_railtel_plan_name_from_text("Active since 09/09/26"), "")

    print("Railtel term plan pricing")
    monthly = price_plan("50Mbps Unlimited", "499")
    check("monthly price (paise)", monthly["price_paise"], 49900)
    check("monthly validity", monthly["validity_days"], 30)

    quarterly = price_plan("50Mbps Unlimited x3", "499")
    check("x3 price = 3 months", quarterly["price_paise"], 149700)
    check("x3 validity = 90 + 10 free", quarterly["validity_days"], 100)

    half = price_plan("FUP100Mbps-10Mbps 3000GB x6", "699")
    check("x6 price = 6 months", half["price_paise"], 419400)
    check("x6 validity = 180 + 30 free", half["validity_days"], 210)

    yearly = price_plan("Kaveri-100Mbps x10", "600")
    check("x10 price = 10 months", yearly["price_paise"], 600000)
    check("x10 validity = 300 + 60 free", yearly["validity_days"], 360)

    print("\nRailtel session timestamp")
    online = link_state_from("railtel", {
        "downtime": "Active since 09/09/26 12:10:08 PM",
        "session_days": 4,
        "is_online": True,
    })
    check("online state", online["state"], "online")
    check("online since stored", online["since"], "2026-09-09 12:10:08")
    check("online since shown", fmt_datetime_human(online["since"]), "09 Sep 2026, 12:10 pm")
    check("online days", online["days"], 4)
    down = link_state_from("railtel", {
        "downtime": "Down since 13/09/26 08:00:00 AM",
        "session_days": 0,
        "is_online": False,
    })
    check("offline state", down["state"], "offline")
    check("offline since stored", down["since"], "2026-09-13 08:00:00")
    hathway = link_state_from("hathway", {
        "downtime": "VC ID: /VM/JVM | STB NO: T403049507052",
        "is_online": True,
    })
    check("hathway has no session", hathway["state"], "")

    print("\nANT IPTV extras")
    from app.billing import billing_type_for
    from app.upstream.providers import id_problem, normalise_upstream_id

    check("iptv is prepaid", billing_type_for("iptv"), "prepaid")
    check("iptv phone ok", id_problem("iptv", "9876543210"), None)
    check("iptv strips +91", normalise_upstream_id("iptv", "+91 98765 43210"), "9876543210")
    check("iptv rejects Hathway STB", bool(id_problem("iptv", "N70100000001")), True)
    with db_connection() as conn:
        iptv_n = conn.execute(
            "SELECT COUNT(*) AS n FROM packages WHERE provider = 'iptv' AND active = 1"
        ).fetchone()["n"]
        iptv_stats = repo.iptv_stats(conn)
        billed = billing.apply_provider_billing_rules(conn)
    check("iptv catalog seeded", iptv_n >= 20, True)
    check("iptv dashboard starts empty", iptv_stats["total"], 0)
    check("iptv billing rule is a no-op on empty", billed["connections_iptv"], 0)

    print("\nSmartPlay OTT extras")
    check("ott is prepaid", billing_type_for("ott"), "prepaid")
    check("ott phone ok", id_problem("ott", "9876543210"), None)
    check("ott strips +91", normalise_upstream_id("ott", "+91 98765 43210"), "9876543210")
    check("ott rejects Hathway STB", bool(id_problem("ott", "N70100000001")), True)
    with db_connection() as conn:
        ott_n = conn.execute(
            "SELECT COUNT(*) AS n FROM packages WHERE provider = 'ott' AND active = 1"
        ).fetchone()["n"]
        ott_stats = repo.ott_stats(conn)
    check("ott catalog seeded", ott_n >= 5, True)
    check("ott dashboard starts empty", ott_stats["total"], 0)
    check("ott connection inferred as OTT bundle", billing.infer_plan_bundle([{"provider": "ott"}], None), "internet_iptv_ott")

    # --- Set up one customer with a Hathway STB -----------------------------
    print("\nSetting up a test customer")
    stamp = now_iso()
    with transaction() as conn:
        pkg = conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, active, created_at) "
            "VALUES('hathway', 'HW KA SILVER KANNADA HD 30d', ?, 30, 'postpaid', 0, 1, ?)",
            (to_paise("310"), stamp),
        ).lastrowid
        cust = conn.execute(
            "INSERT INTO customers(code, name, phone, area, status, created_at, updated_at) "
            "VALUES('TEST-1', 'Smoke Test Customer', '9999900000', 'Tiptur', 'active', ?, ?)",
            (stamp, stamp),
        ).lastrowid
        # Expired 12 days ago: renewal must start today, not back-date the lost days.
        cn = conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, card_number, package_id, "
            "status, billing_type, amount_paise, expiry_date, created_at, updated_at) "
            "VALUES(?, 'hathway', 'N70100000001', 'T400000000001', ?, 'active', 'postpaid', 0, "
            "?, ?, ?)",
            (cust, pkg, day(-12), stamp, stamp),
        ).lastrowid
        # An unpaid bill from a period that already ended.
        billing.create_bill(
            conn,
            customer_id=cust,
            connection_id=cn,
            package_id=pkg,
            package_name="HW KA SILVER KANNADA HD 30d",
            amount_paise=to_paise("310"),
            period_start=day(-42),
            period_end=day(-12),
            source="cycle",
        )
        billing.reconcile_customer(conn, cust)
        ledger = billing.customer_ledger(conn, cust)
    check("opening outstanding (paise)", ledger["outstanding_paise"], 31000)

    # --- Record a payment that also asks for a renewal ----------------------
    print("\nCollecting a payment of Rs 620 with a renewal requested")
    with transaction() as conn:
        payment_id = billing.record_payment(
            conn, customer_id=cust, connection_id=cn, amount_paise=to_paise("620"), mode="upi"
        )
        billing.reconcile_customer(conn, cust)
        job_id = job_queue.enqueue_job(
            conn, connection_id=cn, action="renew", payment_id=payment_id, needs_confirmation=True
        )
        ledger = billing.customer_ledger(conn, cust)
        job_status = conn.execute(
            "SELECT status FROM upstream_jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"]

    check("old bill cleared", ledger["outstanding_paise"], 0)
    check("remaining money held as advance", ledger["credit_paise"], 31000)
    check("job parked for approval", job_status, "awaiting_confirm")

    # --- Nothing should run before confirmation -----------------------------
    print("\nWorker must not touch the portal before approval")
    check("jobs run while unapproved", job_queue.drain_queue(), 0)

    # --- Confirm and let the worker run -------------------------------------
    print("\nConfirming the job and running the worker")
    with transaction() as conn:
        check("confirm accepted", job_queue.confirm_job(conn, job_id), True)
    check("jobs executed", job_queue.drain_queue(), 1)

    with transaction() as conn:
        job = conn.execute("SELECT * FROM upstream_jobs WHERE id = ?", (job_id,)).fetchone()
        connection_row = conn.execute("SELECT * FROM connections WHERE id = ?", (cn,)).fetchone()
        bills = conn.execute(
            "SELECT * FROM bills WHERE customer_id = ? ORDER BY id", (cust,)
        ).fetchall()
        ledger = billing.customer_ledger(conn, cust)

    check("job finished", job["status"], "done")
    check("job produced a bill", job["bill_id"] is not None, True)
    check("bill count", len(bills), 2)

    renewal = bills[-1]
    check("renewal bill source", renewal["source"], "renewal")
    check("renewal amount (paise)", renewal["total_paise"], 31000)
    check("expired connection restarts today", renewal["period_start"], day(0))
    check("period runs 30 days from today", renewal["period_end"], day(29))
    check("connection expiry advanced", connection_row["expiry_date"], day(29))
    check("renewal bill auto-paid from advance", renewal["status"], "paid")
    check("nothing left owing", ledger["net_due_paise"], 0)
    check("advance fully used", ledger["credit_paise"], 0)

    # --- Renewing early must not burn the days already paid for -------------
    print("\nRenewing a connection that still has 10 days left")
    with transaction() as conn:
        cn2 = conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, package_id, status, "
            "billing_type, amount_paise, expiry_date, created_at, updated_at) "
            "VALUES(?, 'hathway', 'N70100000002', ?, 'active', 'postpaid', 0, ?, ?, ?)",
            (cust, pkg, day(10), stamp, stamp),
        ).lastrowid
        early_job = job_queue.enqueue_job(
            conn, connection_id=cn2, action="renew", needs_confirmation=False
        )
    check("early renewal executed", job_queue.drain_queue(), 1)

    with transaction() as conn:
        early_bill = conn.execute(
            "SELECT * FROM bills WHERE job_id = ?", (early_job,)
        ).fetchone()
        cn2_row = conn.execute("SELECT * FROM connections WHERE id = ?", (cn2,)).fetchone()
    check("new period starts the day after old expiry", early_bill["period_start"], day(11))
    check("expiry extended, not reset", cn2_row["expiry_date"], day(40))

    print("\nHathway STB mapping")
    with db_connection() as conn:
        hw = repo.hathway_mapping_stats(conn)
        running = repo.list_hathway_stbs(conn, view="running")
        live = repo.search_customers(conn, view="hathway_live")
        unmapped = repo.list_hathway_stbs(conn, view="unmapped")
    check("two mapped Hathway STBs", hw["mapped_total"], 2)
    check("both listed as running", running["total"], 2)
    check("one live Hathway customer", live["total"], 1)
    check("no unmapped Hathway ids", unmapped["total"], 0)

    # --- Bill checker should now find nothing to do -------------------------
    print("\nBill checker on freshly renewed connections")
    with transaction() as conn:
        summary = billing.run_bill_checker(conn)
    check("no duplicate bills generated", summary["generated"], 0)
    check("both connections skipped as still paid",
          summary["skipped"].get("still_within_paid_period"), 2)

    print("\nRenew and collect later")
    stamp = now_iso()
    with transaction() as conn:
        cn3 = conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, package_id, "
            "status, billing_type, amount_paise, expiry_date, created_at, updated_at) "
            "VALUES(?, 'hathway', 'N70100000003', ?, 'active', 'postpaid', 0, ?, ?, ?)",
            (cust, pkg, day(-1), stamp, stamp),
        ).lastrowid
        job_queue.enqueue_job(
            conn, connection_id=cn3, action="renew", collect_later=True,
            needs_confirmation=False,
        )
    check("collect-later job ran", job_queue.drain_queue(), 1)
    with db_connection() as conn:
        follow = repo.list_collect_later(conn)
        follow_stats = repo.dashboard_stats(conn)["followups"]
    check("one follow-up bill", len(follow), 1)
    check("follow-up customer", int(follow[0]["customer_id"]), cust)
    check("follow-up kind is renew", follow[0]["followup_kind"], "renew")
    check("dashboard follow-up count", follow_stats["count"], 1)
    check("dashboard renew follow-up", follow_stats["renew"], 1)
    check("dashboard manual follow-up empty", follow_stats["manual"], 0)
    owed = int(follow[0]["total_paise"] - follow[0]["paid_paise"])
    with transaction() as conn:
        billing.record_payment(conn, customer_id=cust, amount_paise=owed, mode="cash")
        billing.reconcile_customer(conn, cust)
    with db_connection() as conn:
        check("follow-up cleared after cash", len(repo.list_collect_later(conn)), 0)

    print("\nManual payment follow-up")
    with transaction() as conn:
        follow_cust = conn.execute(
            "INSERT INTO customers(code, name, phone, area, status, created_at, updated_at) "
            "VALUES('TEST-FU', 'Follow-up Customer', '9999900099', 'Tiptur', 'active', ?, ?)",
            (stamp, stamp),
        ).lastrowid
        created = billing.add_manual_followup(
            conn, customer_id=follow_cust, amount_paise=2500, notes="Office chase"
        )
    check("manual follow-up created a bill", created["created"], True)
    with db_connection() as conn:
        manual = repo.list_collect_later(conn, kind="manual")
        renew_only = repo.list_collect_later(conn, kind="renew")
        follow_stats = repo.dashboard_stats(conn)["followups"]
    check("one manual follow-up", len(manual), 1)
    check("manual kind", manual[0]["followup_kind"], "manual")
    check("manual source", manual[0]["source"], "manual_followup")
    check("renew filter empty", len(renew_only), 0)
    check("dashboard manual count", follow_stats["manual"], 1)
    with transaction() as conn:
        dropped = billing.drop_followup(conn, int(manual[0]["id"]))
        billing.cancel_bill(conn, int(manual[0]["id"]))
        billing.reconcile_customer(conn, follow_cust)
    check("manual follow-up dropped", dropped, True)
    with db_connection() as conn:
        check("manual list empty after drop", len(repo.list_collect_later(conn)), 0)
    with transaction() as conn:
        existing = billing.create_bill(
            conn,
            customer_id=follow_cust,
            connection_id=None,
            package_id=None,
            package_name="Arrears",
            amount_paise=1800,
            period_start=today(),
            period_end=None,
            source="manual",
        )
        flagged = billing.add_manual_followup(conn, customer_id=follow_cust, notes="Chase arrears")
    check("existing bill flagged, no extra charge", flagged["created"], False)
    check("one bill flagged", flagged["flagged"], 1)
    with db_connection() as conn:
        flagged_row = repo.list_collect_later(conn, kind="manual")[0]
    check("flagged bill id", int(flagged_row["id"]), int(existing))
    check("flagged kind", flagged_row["followup_kind"], "manual")
    with transaction() as conn:
        billing.drop_followup(conn, int(existing))
        billing.cancel_bill(conn, int(existing))
        billing.reconcile_customer(conn, follow_cust)

    print("\nManual balance set")
    with transaction() as conn:
        changed = billing.set_customer_due(
            conn, cust, 5000, actor="admin", reason="Office correction"
        )
        ledger = billing.customer_ledger(conn, cust)
        stmt = repo.customer_statement(conn, cust)
    check("balance change recorded", changed["changed"], True)
    check("due is now 50", ledger["net_due_paise"], 5000)
    check(
        "statement shows the reason",
        any("Office correction" in (row["description"] or "") for row in stmt),
        True,
    )

    print("\nRailtel payment includes GST")
    from app.money import gst_on_exclusive
    gst, total = gst_on_exclusive(to_paise("499"), 18)
    check("499 + 18% GST (paise)", gst, 8982)
    check("499 + 18% total (paise)", total, 58882)
    with transaction() as conn:
        rpkg = conn.execute(
            "INSERT INTO packages(provider, name, price_paise, validity_days, billing_type, "
            "gst_percentage, active, created_at) "
            "VALUES('railtel', 'FUP GST 499', ?, 30, 'prepaid', 18, 1, ?)",
            (to_paise("499"), stamp),
        ).lastrowid
        rcust = conn.execute(
            "INSERT INTO customers(code, name, phone, area, status, created_at, updated_at) "
            "VALUES('TEST-GST', 'Railtel GST Customer', '9999900088', 'Tiptur', 'active', ?, ?)",
            (stamp, stamp),
        ).lastrowid
        rcn = conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, package_id, "
            "status, billing_type, amount_paise, expiry_date, created_at, updated_at) "
            "VALUES(?, 'railtel', 'ka.gst1', ?, 'active', 'prepaid', 0, ?, ?, ?)",
            (rcust, rpkg, day(-1), stamp, stamp),
        ).lastrowid
        crow = conn.execute("SELECT * FROM connections WHERE id = ?", (rcn,)).fetchone()
        prow = conn.execute("SELECT * FROM packages WHERE id = ?", (rpkg,)).fetchone()
        quote = billing.quoted_charge(crow, prow)
        bill_id, _, _ = billing.bill_for_renewal(
            conn, crow, prow, provider_expiry=None
        )
        rbill = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    check("quoted total includes GST", quote["total_paise"], 58882)
    check("default railtel term", billing.connection_validity_days(crow, prow), 30)
    with transaction() as conn:
        pkg_name = conn.execute("SELECT name FROM packages WHERE id = ?", (rpkg,)).fetchone()["name"]
        billing.bind_portal_plan(conn, rcn, pkg_name)
        bound = conn.execute(
            "SELECT package_id, upstream_plan_name FROM connections WHERE id = ?", (rcn,)
        ).fetchone()
        billing.bind_portal_plan(conn, rcn, "Unknown Portal Plan XYZ")
        unknown = conn.execute(
            "SELECT package_id, upstream_plan_name FROM connections WHERE id = ?", (rcn,)
        ).fetchone()
    check("portal plan stored", bound["upstream_plan_name"], pkg_name)
    check("portal plan attached catalog", int(bound["package_id"]), int(rpkg))
    check("unknown portal plan kept", unknown["upstream_plan_name"], "Unknown Portal Plan XYZ")
    check("unknown portal plan keeps catalog id", int(unknown["package_id"]), int(rpkg))
    check("railtel renewal base", rbill["amount_paise"], 49900)
    check("railtel renewal GST", rbill["gst_paise"], 8982)
    check("railtel renewal total", rbill["total_paise"], 58882)
    with transaction() as conn:
        billing.set_customer_collect_paise(conn, rcust, to_paise("600"))
        crow = conn.execute("SELECT * FROM connections WHERE id = ?", (rcn,)).fetchone()
        prow = conn.execute("SELECT * FROM packages WHERE id = ?", (rpkg,)).fetchone()
        custom_id, _, _ = billing.bill_for_renewal(conn, crow, prow, provider_expiry=None)
        custom_bill = conn.execute("SELECT * FROM bills WHERE id = ?", (custom_id,)).fetchone()
    check("custom collect bills 600", custom_bill["total_paise"], 60000)
    with transaction() as conn:
        conn.execute("UPDATE connections SET validity_days = 100 WHERE id = ?", (rcn,))
        crow = conn.execute("SELECT * FROM connections WHERE id = ?", (rcn,)).fetchone()
        prow = conn.execute("SELECT * FROM packages WHERE id = ?", (rpkg,)).fetchone()
        check("custom term 100 days", billing.connection_validity_days(crow, prow), 100)
        _bid, start, end = billing.bill_for_renewal(conn, crow, prow, provider_expiry=None)
    check("x3 term starts today", start.isoformat(), day(0))
    check("x3 term ends in 100 days", end.isoformat(), day(99))
    with transaction() as conn:
        for row in conn.execute(
            "SELECT id FROM bills WHERE customer_id = ? AND source = 'custom_plan'",
            (rcust,),
        ):
            billing.cancel_bill(conn, int(row["id"]))
        billing.set_customer_custom_plan(
            conn,
            rcust,
            name="Railtel + IPTV bundle",
            amount_paise=to_paise("3600"),
            validity_days=210,
            details="Railtel + ANT IPTV · 6 months + 1 month free",
        )
        crow = conn.execute("SELECT * FROM connections WHERE id = ?", (rcn,)).fetchone()
        prow = conn.execute("SELECT * FROM packages WHERE id = ?", (rpkg,)).fetchone()
        first, _, end210 = billing.bill_for_renewal(conn, crow, prow, provider_expiry=None)
        second, _, _ = billing.bill_for_renewal(conn, crow, prow, provider_expiry=None)
        bundle = conn.execute("SELECT * FROM bills WHERE id = ?", (first,)).fetchone()
    check("bundle bill name", bundle["package_name"], "Railtel + IPTV bundle")
    check("bundle bill amount", bundle["total_paise"], 360000)
    check("bundle not billed twice", first, second)
    check("bundle term 7 months", end210.isoformat(), day(209))
    check("bundle details stored", "1 month free" in (bundle["notes"] or ""), True)
    with transaction() as conn:
        billing.record_payment(
            conn,
            customer_id=rcust,
            connection_id=rcn,
            amount_paise=to_paise("3600"),
            mode="cash",
            paid_at=now_iso(),
        )
        cover = billing.customer_cover(conn, rcust)
        listed = repo.get_customer(conn, rcust)
        conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, "
            "billing_type, amount_paise, created_at, updated_at) "
            "VALUES(?, 'iptv', '9999900088', 'active', 'prepaid', 0, ?, ?)",
            (rcust, stamp, stamp),
        )
        billing.set_customer_custom_plan(
            conn,
            rcust,
            name="Railtel + IPTV bundle",
            amount_paise=to_paise("3600"),
            validity_days=210,
            details="Railtel + ANT IPTV · 6 months + 1 month free",
            bundle="internet_iptv_ott",
        )
        cover_ott = billing.customer_cover(conn, rcust)
    check("paid bundle expiry from payment", cover["expiry"], day(209))
    check("IPTV in plan name", cover["label"], "Internet + IPTV")
    check("list next expiry follows payment", listed["next_expiry"], day(209))
    check("saved OTT bundle label", cover_ott["label"], "Internet + IPTV + OTT")
    check("internet only inferred", billing.infer_plan_bundle([], {"name": "Broadband"}), "internet")
    check("iptv connection inferred", billing.infer_plan_bundle([{"provider": "iptv"}], None), "internet_iptv")
    check("ott in details inferred", billing.infer_plan_bundle(
        [{"provider": "railtel"}], {"details": "Internet + IPTV + OTT"}
    ), "internet_iptv_ott")

    print("\nRailtel online snapshot")
    with transaction() as conn:
        conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, "
            "billing_type, amount_paise, created_at, updated_at) "
            "VALUES(?, 'railtel', 'ka.demo1', 'active', 'prepaid', 0, ?, ?)",
            (cust, stamp, stamp),
        )
        online_job = job_queue.enqueue_provider_job(conn, provider="railtel", action="online")
    check("online job queued", bool(online_job), True)
    check("online job ran", job_queue.drain_queue(), 1)
    with transaction() as conn:
        snap = conn.execute(
            "SELECT * FROM railtel_online_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stored = conn.execute(
            "SELECT COUNT(*) AS n FROM railtel_online_rows WHERE snapshot_id = ?",
            (snap["id"],),
        ).fetchone()["n"]
        demo = conn.execute(
            "SELECT link_state, link_since FROM connections WHERE upstream_id = 'ka.demo1'"
        ).fetchone()
        listed = repo.railtel_online_rows(conn, int(snap["id"]))
        matched = sum(1 for row in listed if row.get("customer_id"))
    check("snapshot stored two sessions", stored, 2)
    check("matched ka.demo1 to the test customer", matched, 1)
    check("ka.demo1 marked online", demo["link_state"], "online")
    check("active-since written from the session start", bool(demo["link_since"]), True)

    print("\nAdmin login and collector permissions")
    with db_connection() as conn:
        owner = auth.authenticate(conn, "", settings.password)
        named = auth.authenticate(conn, "admin", settings.password)
        wrong = auth.authenticate(conn, "admin", "not-the-password")
    check("password-only login is admin", owner is not None and owner["username"] == "admin", True)
    check("named admin login works", named is not None and named["role"] == "admin", True)
    check("wrong password rejected", wrong is None, True)
    check("admin can manage agents", auth.can(owner, "agents"), True)

    stamp = now_iso()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO agents(name, username, password_hash, role, permissions, active, "
            "created_at, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
            (
                "Ramesh",
                "ramesh",
                auth.hash_password("field-secret"),
                "collector",
                json.dumps(auth.permissions_for_role("collector")),
                stamp,
                stamp,
            ),
        )
        collector = auth.authenticate(conn, "ramesh", "field-secret")
    check("collector can collect", auth.can(collector, "payments"), True)
    check("collector cannot edit customers", auth.can(collector, "customers_edit"), False)
    check("collector cannot run portal", auth.can(collector, "portal_actions"), False)
    check("collector cannot manage agents", auth.can(collector, "agents"), False)

    print("\nField agent tracking")
    from app import field as field_mod

    check("collector sees own roster", field_mod.can_see_roster(collector), True)
    check("collector cannot open another agent", field_mod.can_see_agent(collector, owner["id"]), False)
    check("admin sees every agent", field_mod.can_see_agent(owner, collector["id"]), True)
    with transaction() as conn:
        field_mod.record_location(
            conn,
            agent_id=collector["id"],
            lat=13.2612,
            lng=76.4784,
            accuracy=12,
            source="manual",
        )
        billing.record_payment(
            conn,
            customer_id=cust,
            amount_paise=10000,
            mode="cash",
            collected_by=collector["name"],
            collected_agent_id=collector["id"],
        )
        cid = conn.execute(
            "INSERT INTO complaints(customer_id, title, details, status, assigned_agent_id, "
            "assigned_to, created_by, created_at, updated_at) "
            "VALUES(?, 'No signal', '', 'in_progress', ?, ?, ?, ?, ?)",
            (cust, collector["id"], collector["name"], collector["name"], stamp, stamp),
        ).lastrowid
        conn.execute(
            "UPDATE complaints SET status = 'fixed', resolution = 'Retrack', "
            "resolved_by = ?, resolved_agent_id = ?, resolved_at = ?, updated_at = ? "
            "WHERE id = ?",
            (collector["name"], collector["id"], stamp, stamp, cid),
        )
        field_mod.record_visit(
            conn,
            agent_id=collector["id"],
            customer_id=cust,
            lat=13.26,
            lng=76.48,
            source="complaint",
        )
        sheet = field_mod.agent_summaries(conn, only_agent_id=collector["id"])[0]
        office = field_mod.office_summary(conn)
        detail = field_mod.agent_day(conn, collector["id"])
    check("collector collected 100 today", sheet["collected_paise"], 10000)
    check("collector GPS is today", sheet["seen_today"], True)
    check("one complaint fixed today", sheet["complaints_fixed"], 1)
    check("office roster includes the collection", office["payments"] >= 1, True)
    check("day sheet lists the payment", len(detail["payments"]), 1)
    check("day sheet lists the complaint", len(detail["complaints"]), 1)
    check("trail has points", len(detail["trail"]) >= 2, True)

    print("\nBix file sync")
    with transaction() as conn:
        bix_cust = conn.execute(
            "INSERT INTO customers(code, name, phone, area, status, created_at, updated_at) "
            "VALUES('AJ-1', 'Bix Household', '7777700000', 'Tiptur', 'active', ?, ?)",
            (stamp, stamp),
        ).lastrowid
    bix_path = _tmp / "bix.xls"
    bix_path.write_text(
        """<table>
        <tr><th>Customer ID</th><th>Customer Name</th><th>Phone</th>
            <th>STB Number</th><th>Due Amount</th><th>Area</th></tr>
        <tr><td>AJ-1</td><td>Bix Household</td><td>7777700000</td>
            <td>N70100000999</td><td>100</td><td>Tiptur</td></tr>
        <tr><td>NEW-9</td><td>New From Bix</td><td>8888800000</td>
            <td>N70100000099</td><td>250</td><td>KB Cross</td></tr>
        </table>""",
        encoding="utf-8",
    )
    items = bix_sync.parse_bix_customers(bix_path)
    check("parsed two Bix customers", len(items), 2)
    with transaction() as conn:
        preview = bix_sync.preview(conn, items)
        matched = next(row for row in preview if row["code"] == "AJ-1")
        missing = next(row for row in preview if row["code"] == "NEW-9")
        check("matched existing by code", matched["match"], "code")
        check("delta to Bix due of 100", matched["delta_paise"], 10000)
        check("unknown customer marked create", missing["action"], "create")
        summary = bix_sync.apply_preview(conn, preview, create_missing=True, actor="admin")
        ledger = billing.customer_ledger(conn, bix_cust)
        created = conn.execute("SELECT * FROM customers WHERE code = 'NEW-9'").fetchone()
        created_ledger = billing.customer_ledger(conn, int(created["id"]))
    check("one customer created", summary["created"], 1)
    check("existing due now matches Bix", ledger["net_due_paise"], 10000)
    check("new customer due matches Bix", created_ledger["net_due_paise"], 25000)

    print("\nBix Customer Export CSV")
    export_path = _tmp / "customer_export.csv"
    export_path.write_text(
        "id,Name,Locality,Mobile,Customer Code,Balance Amount,Settop Box Number,Products,Quantity,Active/Inactive\n"
        '202684276,Sree Photo Shudio,MST Road,9448014499,1292710160,18839.38,\'N70130694675,"HW KA BRONZE 30d\n","1.00\n",active\n'
        '99,"1.00",,,,,,,,\n',
        encoding="utf-8",
    )
    export_items = bix_sync.parse_bix_customers(export_path)
    check("export skips product-only rows", len(export_items), 1)
    photo = export_items[0]
    check("export due is Balance Amount", photo["due_paise"], 1883938)
    check("export STB parsed", photo["stbs"], ["N70130694675"])
    check("export phone parsed", photo["phone"], "9448014499")

    print("\nBix history archive")
    import sqlite3

    hist_path = _tmp / "vk_digital_history.db"
    hist = sqlite3.connect(hist_path)
    hist.executescript(
        """
        CREATE TABLE customers (customer_id TEXT, customer_name TEXT, phone TEXT, status TEXT);
        CREATE TABLE balance_history (
            id INTEGER PRIMARY KEY, customer_id TEXT, bill_name TEXT,
            date TEXT, txn_amount REAL, balance REAL
        );
        """
    )
    hist.execute(
        "INSERT INTO customers VALUES ('202684276', 'Bix Household', '7777700000', '')"
    )
    hist.execute(
        "INSERT INTO customers VALUES ('9', 'Ghost Shop', '1111111111', '')"
    )
    hist.executemany(
        "INSERT INTO balance_history(customer_id, bill_name, date, txn_amount, balance) VALUES(?,?,?,?,?)",
        [
            ("202684276", "Payment on 01-Jan-26", "2026-01-01 10:00:00", 500, 100),
            ("202684276", "Bill from 01-Jan-26 to 31-Jan-26", "2026-02-01 01:00:00", 270, 370),
            ("202684276", "Adjusted Balance from 370 to 70 ~Ub~", "2026-02-02 12:00:00", -300, 70),
            ("9", "Payment on 03-Jan-26", "2026-01-03 09:00:00", 200, 0),
        ],
    )
    hist.commit()
    hist.close()
    with transaction() as conn:
        due_before = billing.customer_ledger(conn, bix_cust)["net_due_paise"]
        summary = bix_history.import_archive(conn, hist_path, actor="admin")
        due_after = billing.customer_ledger(conn, bix_cust)["net_due_paise"]
        history = bix_history.customer_history(conn, bix_cust)
        pays = bix_history.list_archive_payments(conn)
    check("history imported four rows", summary["txns_seen"], 4)
    check("history matched one by phone", summary["matched"], 1)
    check("history left one unmatched", summary["unmatched"], 1)
    check("history does not change net due", due_after, due_before)
    check("customer sees three archive rows", len(history["rows"]), 3)
    check(
        "adjustment classified",
        next(r["kind"] for r in history["rows"] if "Adjusted" in r["label"]),
        "adjustment",
    )
    check("archive payments listed", pays["count"], 2)
    check(
        "payment amount in paise",
        next(r["amount_paise"] for r in history["rows"] if r["kind"] == "payment"),
        50000,
    )
    again = bix_history.import_archive
    with transaction() as conn:
        second = again(conn, hist_path, actor="admin")
    check("re-import adds no duplicate rows", second["txns_new"], 0)

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
