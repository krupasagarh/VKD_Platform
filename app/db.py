"""SQLite storage for VK Platform.

Deliberately plain sqlite3 (no ORM) so the schema stays readable and the file can
be inspected or backed up by copying one .db file.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .config import settings
from .money import now_iso

SCHEMA_VERSION = 40

# Logged-in agent for the current request, so every activity row knows who and where.
_current_agent_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "vk_current_agent_id", default=None
)
ACTIVITY_LOCATION_MAX_AGE = timedelta(minutes=20)


def set_current_agent(agent_id: int | None) -> None:
    _current_agent_id.set(int(agent_id) if agent_id else None)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT UNIQUE,
    name         TEXT NOT NULL,
    phone        TEXT,
    alt_phone    TEXT,
    email        TEXT,
    address      TEXT,
    area         TEXT,
    sub_area     TEXT,
    pincode      TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    notes        TEXT,
    collect_paise INTEGER NOT NULL DEFAULT 0,
    custom_plan_name TEXT NOT NULL DEFAULT '',
    custom_plan_amount_paise INTEGER NOT NULL DEFAULT 0,
    custom_plan_validity_days INTEGER NOT NULL DEFAULT 0,
    custom_plan_details TEXT NOT NULL DEFAULT '',
    custom_plan_bundle TEXT NOT NULL DEFAULT '',
    lat          REAL,
    lng          REAL,
    geo_accuracy REAL,
    geo_at       TEXT,
    geo_by       TEXT,
    whatsapp_status TEXT NOT NULL DEFAULT '',
    whatsapp_checked_at TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);
CREATE INDEX IF NOT EXISTS idx_customers_name  ON customers(name);

CREATE TABLE IF NOT EXISTS packages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    provider           TEXT NOT NULL,
    name               TEXT NOT NULL,
    price_paise        INTEGER NOT NULL DEFAULT 0,
    validity_days      INTEGER NOT NULL DEFAULT 30,
    billing_type       TEXT NOT NULL DEFAULT 'prepaid',
    gst_percentage     REAL NOT NULL DEFAULT 0,
    speed_mbps         TEXT,
    channel_count      INTEGER,
    upstream_plan_code TEXT,
    active             INTEGER NOT NULL DEFAULT 1,
    notes              TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE(provider, name)
);

CREATE TABLE IF NOT EXISTS connections (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id        INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    provider           TEXT NOT NULL,
    upstream_id        TEXT NOT NULL,
    card_number        TEXT,
    package_id         INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    label              TEXT,
    status             TEXT NOT NULL DEFAULT 'active',
    billing_type       TEXT NOT NULL DEFAULT 'prepaid',
    amount_paise       INTEGER NOT NULL DEFAULT 0,
    validity_days      INTEGER NOT NULL DEFAULT 0,
    expiry_date        TEXT,
    upstream_plan_name TEXT,
    -- Hathway PlanExpiry report bouquet (HSP/Basic). Staff-only; never billed.
    hathway_pack_name  TEXT NOT NULL DEFAULT '',
    -- The provider's live session, as of last_synced_at. Railtel reports when the line
    -- came up or went down, which is the first thing a customer asks about; Hathway does
    -- not report a session, so these stay empty for it.
    link_state         TEXT NOT NULL DEFAULT '',   -- 'online' | 'offline' | ''
    link_since         TEXT NOT NULL DEFAULT '',
    link_days          INTEGER,
    last_synced_at     TEXT,
    activated_on       TEXT,
    notes              TEXT,
    portal_account_id  TEXT NOT NULL DEFAULT '',
    -- Free STB: renewed but never charged. '' = normal; 'social' | 'node' | 'other'.
    free_reason        TEXT NOT NULL DEFAULT '',
    free_note          TEXT NOT NULL DEFAULT '',
    discount_paise     INTEGER NOT NULL DEFAULT 0,  -- off every month of the plan
    owner_reason       TEXT NOT NULL DEFAULT '',    -- '' | 'friend' | 'combo': owner collects, hidden from agents
    owner_note         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(provider, upstream_id)
);
CREATE INDEX IF NOT EXISTS idx_connections_customer ON connections(customer_id);
CREATE INDEX IF NOT EXISTS idx_connections_expiry   ON connections(expiry_date);

CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no       TEXT NOT NULL UNIQUE,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL,
    package_id    INTEGER REFERENCES packages(id) ON DELETE SET NULL,
    package_name  TEXT,
    period_start  TEXT,
    period_end    TEXT,
    amount_paise  INTEGER NOT NULL DEFAULT 0,
    gst_paise     INTEGER NOT NULL DEFAULT 0,
    total_paise   INTEGER NOT NULL DEFAULT 0,
    paid_paise    INTEGER NOT NULL DEFAULT 0,
    due_date      TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    source        TEXT NOT NULL DEFAULT 'manual',
    job_id        INTEGER,
    notes         TEXT,
    collect_later INTEGER NOT NULL DEFAULT 0,
    followup_kind TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bills_customer ON bills(customer_id);
CREATE INDEX IF NOT EXISTS idx_bills_status   ON bills(status);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no    TEXT NOT NULL UNIQUE,
    customer_id   INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL,
    amount_paise  INTEGER NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'cash',
    reference     TEXT,
    collected_by  TEXT,
    collected_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    paid_at       TEXT NOT NULL,
    notes         TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id);
CREATE INDEX IF NOT EXISTS idx_payments_paid_at  ON payments(paid_at);

CREATE TABLE IF NOT EXISTS bill_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    payment_id   INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bill_payments_bill    ON bill_payments(bill_id);
CREATE INDEX IF NOT EXISTS idx_bill_payments_payment ON bill_payments(payment_id);

-- connection_id / customer_id are nullable: dealer-account jobs (wallet balance,
-- STB counts) belong to a provider, not to any one customer.
CREATE TABLE IF NOT EXISTS upstream_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id   INTEGER REFERENCES connections(id) ON DELETE CASCADE,
    customer_id     INTEGER REFERENCES customers(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    action          TEXT NOT NULL,
    status          TEXT NOT NULL,
    -- Lower runs first. A scheduled sweep queues hundreds of jobs; a renewal for a
    -- customer standing in front of you must not wait behind them.
    priority        INTEGER NOT NULL DEFAULT 100,
    sweep_id        INTEGER REFERENCES sync_sweeps(id) ON DELETE SET NULL,
    payment_id      INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    bill_id         INTEGER REFERENCES bills(id) ON DELETE SET NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 2,
    requested_by    TEXT,
    scheduled_for   TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    result_json     TEXT,
    error           TEXT,
    screenshot_path TEXT,
    collect_later   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON upstream_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_connection ON upstream_jobs(connection_id);

CREATE TABLE IF NOT EXISTS activity_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    actor         TEXT,
    kind          TEXT NOT NULL,
    customer_id   INTEGER,
    connection_id INTEGER,
    message       TEXT NOT NULL,
    meta_json     TEXT,
    agent_id      INTEGER,
    lat           REAL,
    lng           REAL,
    loc_accuracy  REAL,
    loc_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(at DESC);

-- One row per bulk status sweep, so progress survives a restart and the operator can
-- see how far a four-hour run has got.
CREATE TABLE IF NOT EXISTS sync_sweeps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    providers    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    stale_days   INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    trigger      TEXT NOT NULL DEFAULT 'manual',
    requested_by TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS provider_status (
    provider       TEXT PRIMARY KEY,
    wallet_balance TEXT,
    active_count   TEXT,
    inactive_count TEXT,
    total_count    TEXT,
    operator_name  TEXT,
    checked_at     TEXT,
    error          TEXT
);

-- One snapshot of who is online on Railtel right now, read from dash.php after
-- clicking the Online subscribers tile on anpcntl. Rows live in railtel_online_rows.
CREATE TABLE IF NOT EXISTS railtel_online_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at    TEXT NOT NULL,
    job_id        INTEGER,
    online_count  INTEGER NOT NULL DEFAULT 0,
    row_count     INTEGER NOT NULL DEFAULT 0,
    upload_gb     TEXT,
    download_gb   TEXT,
    total_gb      TEXT,
    nas_json      TEXT,
    partial       INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS railtel_online_rows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id  INTEGER NOT NULL REFERENCES railtel_online_snapshots(id) ON DELETE CASCADE,
    session_id   TEXT,
    username     TEXT NOT NULL,
    mac          TEXT,
    framed_ip    TEXT,
    start_time   TEXT,
    start_at     TEXT,
    total_time   TEXT,
    upload_mb    TEXT,
    download_mb  TEXT,
    total_mb     TEXT
);
CREATE INDEX IF NOT EXISTS idx_online_snap ON railtel_online_rows(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_online_user ON railtel_online_rows(username);

-- Full My Subscribers list from anpcntl/mysubscribers (Renewal Date + red-row expiry).
CREATE TABLE IF NOT EXISTS railtel_subscriber_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at     TEXT NOT NULL,
    job_id         INTEGER,
    total_count    INTEGER NOT NULL DEFAULT 0,
    row_count      INTEGER NOT NULL DEFAULT 0,
    active_count   INTEGER NOT NULL DEFAULT 0,
    expired_count  INTEGER NOT NULL DEFAULT 0,
    late_1d        INTEGER NOT NULL DEFAULT 0,
    late_2d        INTEGER NOT NULL DEFAULT 0,
    expiring_7d    INTEGER NOT NULL DEFAULT 0,
    note           TEXT
);
CREATE TABLE IF NOT EXISTS railtel_subscriber_rows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    INTEGER NOT NULL REFERENCES railtel_subscriber_snapshots(id) ON DELETE CASCADE,
    subscriber_id  TEXT,
    username       TEXT NOT NULL,
    package_name   TEXT,
    renewal_date   TEXT,
    renewal_at     TEXT,
    mobile         TEXT,
    name           TEXT,
    status         TEXT,
    is_red         INTEGER NOT NULL DEFAULT 0,
    row_class      TEXT
);
CREATE INDEX IF NOT EXISTS idx_railtel_sub_snap ON railtel_subscriber_rows(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_railtel_sub_user ON railtel_subscriber_rows(username);

CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'collector',
    permissions   TEXT NOT NULL DEFAULT '[]',
    phone         TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT NOT NULL DEFAULT '',
    last_seen_at  TEXT NOT NULL DEFAULT '',
    field_duty_on INTEGER NOT NULL DEFAULT 0,
    field_duty_at TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Collector GPS pings and visits (payment / complaint / house pin).
CREATE TABLE IF NOT EXISTS agent_locations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    customer_id  INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    lat          REAL NOT NULL,
    lng          REAL NOT NULL,
    accuracy     REAL,
    source       TEXT NOT NULL DEFAULT 'ping',
    recorded_at  TEXT NOT NULL
);

-- Agent cash / UPI handover to the owner, plus expenses for that period.
CREATE TABLE IF NOT EXISTS agent_settlements (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id             INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    period_from          TEXT NOT NULL,
    period_to            TEXT NOT NULL,
    settled_on           TEXT NOT NULL,
    cable_cash_paise     INTEGER NOT NULL DEFAULT 0,
    cable_upi_agent_paise INTEGER NOT NULL DEFAULT 0,
    owner_upi_paise      INTEGER NOT NULL DEFAULT 0,
    owner_upi_vpa        TEXT NOT NULL DEFAULT '',
    owner_scanner_paise  INTEGER NOT NULL DEFAULT 0,
    notes                TEXT NOT NULL DEFAULT '',
    collection_json      TEXT NOT NULL DEFAULT '',
    created_by           TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_settlements_agent ON agent_settlements(agent_id, settled_on);
CREATE INDEX IF NOT EXISTS idx_agent_settlements_day ON agent_settlements(settled_on);

CREATE TABLE IF NOT EXISTS agent_settlement_lines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL REFERENCES agent_settlements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    line_date     TEXT NOT NULL DEFAULT '',
    customer_name TEXT NOT NULL DEFAULT '',
    comment       TEXT NOT NULL DEFAULT '',
    amount_paise  INTEGER NOT NULL DEFAULT 0,
    customer_id   INTEGER,
    provider      TEXT NOT NULL DEFAULT '',
    area          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_settlement_lines ON agent_settlement_lines(settlement_id);

CREATE TABLE IF NOT EXISTS agent_settlement_proofs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id  INTEGER NOT NULL REFERENCES agent_settlements(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL DEFAULT 'other',
    caption        TEXT NOT NULL DEFAULT '',
    stored_name    TEXT NOT NULL,
    original_name  TEXT NOT NULL DEFAULT '',
    content_type   TEXT NOT NULL DEFAULT 'image/jpeg',
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_settlement_proofs ON agent_settlement_proofs(settlement_id);

-- One uploaded Bix Customer_details file, kept so Apply can run after Preview.
CREATE TABLE IF NOT EXISTS bix_sync_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'preview',
    row_count    INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    summary_json TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    applied_at   TEXT
);

CREATE TABLE IF NOT EXISTS hathway_expiry_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'preview',
    row_count    INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    summary_json TEXT,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    applied_at   TEXT
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL UNIQUE,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT '',
    unit           TEXT NOT NULL DEFAULT 'pcs',
    provider_scope TEXT NOT NULL DEFAULT 'both',
    sort_order     INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory_receipts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES inventory_items(id),
    qty           REAL NOT NULL,
    amount_paise  INTEGER NOT NULL DEFAULT 0,
    received_on   TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_receipts_item ON inventory_receipts(item_id, received_on);

CREATE TABLE IF NOT EXISTS inventory_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES inventory_items(id),
    qty           REAL NOT NULL,
    kind          TEXT NOT NULL,
    customer_id   INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    note          TEXT NOT NULL DEFAULT '',
    used_on       TEXT NOT NULL,
    agent_id      INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    created_by    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_usage_item ON inventory_usage(item_id, used_on);
CREATE INDEX IF NOT EXISTS idx_inv_usage_customer ON inventory_usage(customer_id);

CREATE TABLE IF NOT EXISTS complaints (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    details           TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    assigned_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    assigned_to       TEXT,
    created_by        TEXT,
    last_note         TEXT,
    resolution        TEXT,
    resolved_by       TEXT,
    resolved_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
    resolved_at       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_complaints_status   ON complaints(status);
CREATE INDEX IF NOT EXISTS idx_complaints_agent    ON complaints(assigned_agent_id);
CREATE INDEX IF NOT EXISTS idx_complaints_customer ON complaints(customer_id);

CREATE TABLE IF NOT EXISTS bix_history_imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    imported_by     TEXT,
    imported_at     TEXT NOT NULL,
    customers_seen  INTEGER NOT NULL DEFAULT 0,
    txns_seen       INTEGER NOT NULL DEFAULT 0,
    txns_new        INTEGER NOT NULL DEFAULT 0,
    matched         INTEGER NOT NULL DEFAULT 0,
    unmatched       INTEGER NOT NULL DEFAULT 0
);

-- Copy of Bix Balance History. Never posted into bills / payments.
CREATE TABLE IF NOT EXISTS bix_history_customers (
    bix_customer_id      TEXT PRIMARY KEY,
    name                 TEXT NOT NULL DEFAULT '',
    phone                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT '',
    platform_customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    match_method         TEXT NOT NULL DEFAULT '',
    last_balance_paise   INTEGER,
    last_txn_at          TEXT,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bix_hist_cust_platform ON bix_history_customers(platform_customer_id);
CREATE INDEX IF NOT EXISTS idx_bix_hist_cust_phone ON bix_history_customers(phone);

CREATE TABLE IF NOT EXISTS bix_history_txns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    row_key         TEXT NOT NULL UNIQUE,
    bix_customer_id TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    label           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL,
    balance_paise   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bix_hist_txn_cust ON bix_history_txns(bix_customer_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_bix_hist_txn_kind ON bix_history_txns(kind, occurred_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Applied in order for databases created before the current SCHEMA_VERSION.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        # Drop NOT NULL from upstream_jobs.connection_id / customer_id. SQLite cannot
        # alter a column constraint, so the table is rebuilt and the rows copied.
        """
        CREATE TABLE upstream_jobs_v2 (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id   INTEGER REFERENCES connections(id) ON DELETE CASCADE,
            customer_id     INTEGER REFERENCES customers(id) ON DELETE CASCADE,
            provider        TEXT NOT NULL,
            action          TEXT NOT NULL,
            status          TEXT NOT NULL,
            payment_id      INTEGER REFERENCES payments(id) ON DELETE SET NULL,
            bill_id         INTEGER REFERENCES bills(id) ON DELETE SET NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            max_attempts    INTEGER NOT NULL DEFAULT 2,
            requested_by    TEXT,
            scheduled_for   TEXT,
            started_at      TEXT,
            completed_at    TEXT,
            result_json     TEXT,
            error           TEXT,
            screenshot_path TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """,
        """
        INSERT INTO upstream_jobs_v2 (
            id, connection_id, customer_id, provider, action, status, payment_id, bill_id,
            attempts, max_attempts, requested_by, scheduled_for, started_at, completed_at,
            result_json, error, screenshot_path, created_at, updated_at)
        SELECT id, connection_id, customer_id, provider, action, status, payment_id, bill_id,
               attempts, max_attempts, requested_by, scheduled_for, started_at, completed_at,
               result_json, error, screenshot_path, created_at, updated_at
        FROM upstream_jobs
        """,
        "DROP TABLE upstream_jobs",
        "ALTER TABLE upstream_jobs_v2 RENAME TO upstream_jobs",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status     ON upstream_jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_connection ON upstream_jobs(connection_id)",
    ),
    3: (
        "ALTER TABLE upstream_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 100",
        "ALTER TABLE upstream_jobs ADD COLUMN sweep_id INTEGER",
        """
        CREATE TABLE IF NOT EXISTS sync_sweeps (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            providers    TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'running',
            stale_days   INTEGER NOT NULL DEFAULT 0,
            total        INTEGER NOT NULL DEFAULT 0,
            trigger      TEXT NOT NULL DEFAULT 'manual',
            requested_by TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            finished_at  TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_jobs_sweep ON upstream_jobs(sweep_id)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_ready ON upstream_jobs(status, priority, id)",
    ),
    4: (
        "ALTER TABLE connections ADD COLUMN link_state TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE connections ADD COLUMN link_since TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE connections ADD COLUMN link_days INTEGER",
    ),
    5: (
        """
        CREATE TABLE IF NOT EXISTS railtel_online_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at    TEXT NOT NULL,
            job_id        INTEGER,
            online_count  INTEGER NOT NULL DEFAULT 0,
            row_count     INTEGER NOT NULL DEFAULT 0,
            upload_gb     TEXT,
            download_gb   TEXT,
            total_gb      TEXT,
            nas_json      TEXT,
            partial       INTEGER NOT NULL DEFAULT 0,
            note          TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS railtel_online_rows (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id  INTEGER NOT NULL REFERENCES railtel_online_snapshots(id) ON DELETE CASCADE,
            session_id   TEXT,
            username     TEXT NOT NULL,
            mac          TEXT,
            framed_ip    TEXT,
            start_time   TEXT,
            start_at     TEXT,
            total_time   TEXT,
            upload_mb    TEXT,
            download_mb  TEXT,
            total_mb     TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_online_snap ON railtel_online_rows(snapshot_id)",
        "CREATE INDEX IF NOT EXISTS idx_online_user ON railtel_online_rows(username)",
    ),
    6: (
        """
        CREATE TABLE IF NOT EXISTS agents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'collector',
            permissions   TEXT NOT NULL DEFAULT '[]',
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bix_sync_batches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            filename     TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'preview',
            row_count    INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            summary_json TEXT,
            created_by   TEXT,
            created_at   TEXT NOT NULL,
            applied_at   TEXT
        )
        """,
    ),
    7: (
        """
        CREATE TABLE IF NOT EXISTS complaints (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            title             TEXT NOT NULL,
            details           TEXT,
            status            TEXT NOT NULL DEFAULT 'open',
            assigned_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL,
            assigned_to       TEXT,
            created_by        TEXT,
            last_note         TEXT,
            resolution        TEXT,
            resolved_by       TEXT,
            resolved_at       TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_complaints_status   ON complaints(status)",
        "CREATE INDEX IF NOT EXISTS idx_complaints_agent    ON complaints(assigned_agent_id)",
        "CREATE INDEX IF NOT EXISTS idx_complaints_customer ON complaints(customer_id)",
    ),
    8: (
        """
        CREATE TABLE IF NOT EXISTS bix_history_imports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            imported_by     TEXT,
            imported_at     TEXT NOT NULL,
            customers_seen  INTEGER NOT NULL DEFAULT 0,
            txns_seen       INTEGER NOT NULL DEFAULT 0,
            txns_new        INTEGER NOT NULL DEFAULT 0,
            matched         INTEGER NOT NULL DEFAULT 0,
            unmatched       INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS bix_history_customers (
            bix_customer_id      TEXT PRIMARY KEY,
            name                 TEXT NOT NULL DEFAULT '',
            phone                TEXT NOT NULL DEFAULT '',
            status               TEXT NOT NULL DEFAULT '',
            platform_customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
            match_method         TEXT NOT NULL DEFAULT '',
            last_balance_paise   INTEGER,
            last_txn_at          TEXT,
            updated_at           TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bix_hist_cust_platform ON bix_history_customers(platform_customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_bix_hist_cust_phone ON bix_history_customers(phone)",
        """
        CREATE TABLE IF NOT EXISTS bix_history_txns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            row_key         TEXT NOT NULL UNIQUE,
            bix_customer_id TEXT NOT NULL,
            occurred_at     TEXT NOT NULL,
            label           TEXT NOT NULL,
            kind            TEXT NOT NULL,
            amount_paise    INTEGER NOT NULL,
            balance_paise   INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bix_hist_txn_cust ON bix_history_txns(bix_customer_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_bix_hist_txn_kind ON bix_history_txns(kind, occurred_at)",
    ),
    9: (
        "ALTER TABLE bills ADD COLUMN collect_later INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE upstream_jobs ADD COLUMN collect_later INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_bills_followup ON bills(collect_later, status)",
    ),
    10: (
        "ALTER TABLE customers ADD COLUMN lat REAL",
        "ALTER TABLE customers ADD COLUMN lng REAL",
        "ALTER TABLE customers ADD COLUMN geo_accuracy REAL",
        "ALTER TABLE customers ADD COLUMN geo_at TEXT",
        "ALTER TABLE customers ADD COLUMN geo_by TEXT",
    ),
    11: (
        "ALTER TABLE payments ADD COLUMN collected_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL",
        "ALTER TABLE complaints ADD COLUMN resolved_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL",
        """
        CREATE TABLE IF NOT EXISTS agent_locations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id     INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            customer_id  INTEGER REFERENCES customers(id) ON DELETE SET NULL,
            lat          REAL NOT NULL,
            lng          REAL NOT NULL,
            accuracy     REAL,
            source       TEXT NOT NULL DEFAULT 'ping',
            recorded_at  TEXT NOT NULL
        )
        """,
    ),
    12: (
        "ALTER TABLE bills ADD COLUMN followup_kind TEXT NOT NULL DEFAULT ''",
        "UPDATE bills SET followup_kind = 'renew' WHERE collect_later = 1 AND followup_kind = ''",
    ),
    13: (
        "ALTER TABLE customers ADD COLUMN collect_paise INTEGER NOT NULL DEFAULT 0",
    ),
    14: (
        "ALTER TABLE connections ADD COLUMN validity_days INTEGER NOT NULL DEFAULT 0",
    ),
    15: (
        "ALTER TABLE customers ADD COLUMN custom_plan_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE customers ADD COLUMN custom_plan_amount_paise INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE customers ADD COLUMN custom_plan_validity_days INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE customers ADD COLUMN custom_plan_details TEXT NOT NULL DEFAULT ''",
        "UPDATE customers SET custom_plan_amount_paise = collect_paise "
        "WHERE collect_paise > 0 AND custom_plan_amount_paise = 0",
    ),
    16: (
        "ALTER TABLE customers ADD COLUMN custom_plan_bundle TEXT NOT NULL DEFAULT ''",
    ),
    17: (
        """
        CREATE TABLE IF NOT EXISTS railtel_subscriber_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at     TEXT NOT NULL,
            job_id         INTEGER,
            total_count    INTEGER NOT NULL DEFAULT 0,
            row_count      INTEGER NOT NULL DEFAULT 0,
            active_count   INTEGER NOT NULL DEFAULT 0,
            expired_count  INTEGER NOT NULL DEFAULT 0,
            late_1d        INTEGER NOT NULL DEFAULT 0,
            late_2d        INTEGER NOT NULL DEFAULT 0,
            expiring_7d    INTEGER NOT NULL DEFAULT 0,
            note           TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS railtel_subscriber_rows (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id    INTEGER NOT NULL REFERENCES railtel_subscriber_snapshots(id) ON DELETE CASCADE,
            subscriber_id  TEXT,
            username       TEXT NOT NULL,
            package_name   TEXT,
            renewal_date   TEXT,
            renewal_at     TEXT,
            mobile         TEXT,
            name           TEXT,
            status         TEXT,
            is_red         INTEGER NOT NULL DEFAULT 0,
            row_class      TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_railtel_sub_snap ON railtel_subscriber_rows(snapshot_id)",
        "CREATE INDEX IF NOT EXISTS idx_railtel_sub_user ON railtel_subscriber_rows(username)",
    ),
    18: (
        """
        CREATE TABLE IF NOT EXISTS railtel_invoices (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id       INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            connection_id     INTEGER REFERENCES connections(id) ON DELETE SET NULL,
            job_id            INTEGER REFERENCES upstream_jobs(id) ON DELETE SET NULL,
            upstream_id       TEXT NOT NULL,
            invoice_no        TEXT NOT NULL DEFAULT '',
            receipt_date      TEXT NOT NULL DEFAULT '',
            gross_amount      TEXT NOT NULL DEFAULT '',
            file_path         TEXT NOT NULL,
            file_name         TEXT NOT NULL,
            created_at        TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_railtel_inv_customer ON railtel_invoices(customer_id)",
        "CREATE INDEX IF NOT EXISTS idx_railtel_inv_conn ON railtel_invoices(connection_id)",
    ),
    19: (
        "ALTER TABLE railtel_invoices ADD COLUMN whatsapp_sent_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE railtel_invoices ADD COLUMN whatsapp_error TEXT NOT NULL DEFAULT ''",
    ),
    20: (
        "ALTER TABLE upstream_jobs ADD COLUMN batch_json TEXT",
    ),
    21: (
        "ALTER TABLE agents ADD COLUMN phone TEXT NOT NULL DEFAULT ''",
    ),
    22: (
        "ALTER TABLE agents ADD COLUMN provider_scope TEXT NOT NULL DEFAULT 'both'",
    ),
    23: (
        "ALTER TABLE customers ADD COLUMN whatsapp_status TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE customers ADD COLUMN whatsapp_checked_at TEXT NOT NULL DEFAULT ''",
    ),
    24: (
        "ALTER TABLE connections ADD COLUMN portal_account_id TEXT NOT NULL DEFAULT ''",
    ),
    25: (
        """
        CREATE TABLE IF NOT EXISTS agent_areas (
            agent_id  INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            sub_area  TEXT NOT NULL,
            PRIMARY KEY (agent_id, sub_area)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_areas_sub ON agent_areas(sub_area)",
        "ALTER TABLE customers ADD COLUMN assigned_agent_id INTEGER REFERENCES agents(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS idx_customers_assigned_agent ON customers(assigned_agent_id)",
    ),
    26: (
        """
        CREATE TABLE IF NOT EXISTS pay_intents (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token           TEXT NOT NULL UNIQUE,
            customer_id     INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            connection_id   INTEGER REFERENCES connections(id) ON DELETE SET NULL,
            amount_paise    INTEGER NOT NULL,
            reference       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            payment_id      INTEGER REFERENCES payments(id) ON DELETE SET NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            customer_marked_at TEXT,
            confirmed_at    TEXT,
            confirmed_by    TEXT,
            ip_hint         TEXT,
            notes           TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_pay_intents_status ON pay_intents(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_pay_intents_customer ON pay_intents(customer_id)",
    ),
    27: (
        "ALTER TABLE connections ADD COLUMN subscription_expiry TEXT",
    ),
    28: (
        """
        CREATE TABLE IF NOT EXISTS agent_settlements (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id             INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            period_from          TEXT NOT NULL,
            period_to            TEXT NOT NULL,
            settled_on           TEXT NOT NULL,
            cable_cash_paise     INTEGER NOT NULL DEFAULT 0,
            cable_upi_agent_paise INTEGER NOT NULL DEFAULT 0,
            owner_upi_paise      INTEGER NOT NULL DEFAULT 0,
            owner_upi_vpa        TEXT NOT NULL DEFAULT '',
            owner_scanner_paise  INTEGER NOT NULL DEFAULT 0,
            notes                TEXT NOT NULL DEFAULT '',
            created_by           TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_settlements_agent ON agent_settlements(agent_id, settled_on)",
        "CREATE INDEX IF NOT EXISTS idx_agent_settlements_day ON agent_settlements(settled_on)",
        """
        CREATE TABLE IF NOT EXISTS agent_settlement_lines (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            settlement_id INTEGER NOT NULL REFERENCES agent_settlements(id) ON DELETE CASCADE,
            kind          TEXT NOT NULL,
            line_date     TEXT NOT NULL DEFAULT '',
            customer_name TEXT NOT NULL DEFAULT '',
            comment       TEXT NOT NULL DEFAULT '',
            amount_paise  INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_settlement_lines ON agent_settlement_lines(settlement_id)",
    ),
    29: (
        """
        CREATE TABLE IF NOT EXISTS agent_settlement_proofs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            settlement_id  INTEGER NOT NULL REFERENCES agent_settlements(id) ON DELETE CASCADE,
            kind           TEXT NOT NULL DEFAULT 'other',
            caption        TEXT NOT NULL DEFAULT '',
            stored_name    TEXT NOT NULL,
            original_name  TEXT NOT NULL DEFAULT '',
            content_type   TEXT NOT NULL DEFAULT 'image/jpeg',
            size_bytes     INTEGER NOT NULL DEFAULT 0,
            created_by     TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_agent_settlement_proofs ON agent_settlement_proofs(settlement_id)",
    ),
    30: (
        "ALTER TABLE agent_settlements ADD COLUMN collection_json TEXT NOT NULL DEFAULT ''",
    ),
    31: (
        "ALTER TABLE agent_settlement_lines ADD COLUMN customer_id INTEGER",
        "ALTER TABLE agent_settlement_lines ADD COLUMN provider TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE agent_settlement_lines ADD COLUMN area TEXT NOT NULL DEFAULT ''",
    ),
    32: (
        "ALTER TABLE connections ADD COLUMN hathway_pack_name TEXT NOT NULL DEFAULT ''",
        """
        CREATE TABLE IF NOT EXISTS hathway_expiry_batches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            filename     TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'preview',
            row_count    INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            summary_json TEXT,
            created_by   TEXT,
            created_at   TEXT NOT NULL,
            applied_at   TEXT
        )
        """,
    ),
    33: (
        """
        CREATE TABLE IF NOT EXISTS inventory_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            code           TEXT NOT NULL UNIQUE,
            name           TEXT NOT NULL,
            category       TEXT NOT NULL DEFAULT '',
            unit           TEXT NOT NULL DEFAULT 'pcs',
            provider_scope TEXT NOT NULL DEFAULT 'both',
            sort_order     INTEGER NOT NULL DEFAULT 0,
            active         INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inventory_receipts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id       INTEGER NOT NULL REFERENCES inventory_items(id),
            qty           REAL NOT NULL,
            amount_paise  INTEGER NOT NULL DEFAULT 0,
            received_on   TEXT NOT NULL,
            note          TEXT NOT NULL DEFAULT '',
            created_by    TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_inv_receipts_item ON inventory_receipts(item_id, received_on)",
        """
        CREATE TABLE IF NOT EXISTS inventory_usage (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id       INTEGER NOT NULL REFERENCES inventory_items(id),
            qty           REAL NOT NULL,
            kind          TEXT NOT NULL,
            customer_id   INTEGER REFERENCES customers(id) ON DELETE SET NULL,
            note          TEXT NOT NULL DEFAULT '',
            used_on       TEXT NOT NULL,
            agent_id      INTEGER REFERENCES agents(id) ON DELETE SET NULL,
            created_by    TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_inv_usage_item ON inventory_usage(item_id, used_on)",
        "CREATE INDEX IF NOT EXISTS idx_inv_usage_customer ON inventory_usage(customer_id)",
    ),
    34: (
        "ALTER TABLE agents ADD COLUMN last_login_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE agents ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''",
    ),
    35: (
        "ALTER TABLE agents ADD COLUMN field_duty_on INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN field_duty_at TEXT NOT NULL DEFAULT ''",
    ),
    36: (
        "ALTER TABLE connections ADD COLUMN free_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE connections ADD COLUMN free_note TEXT NOT NULL DEFAULT ''",
    ),
    37: (
        "ALTER TABLE connections ADD COLUMN discount_paise INTEGER NOT NULL DEFAULT 0",
    ),
    38: (
        "ALTER TABLE connections ADD COLUMN owner_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE connections ADD COLUMN owner_note TEXT NOT NULL DEFAULT ''",
    ),
    39: (
        "ALTER TABLE activity_log ADD COLUMN agent_id INTEGER",
        "ALTER TABLE activity_log ADD COLUMN lat REAL",
        "ALTER TABLE activity_log ADD COLUMN lng REAL",
        "ALTER TABLE activity_log ADD COLUMN loc_accuracy REAL",
        "ALTER TABLE activity_log ADD COLUMN loc_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_activity_agent ON activity_log(agent_id, at)",
        # Old rows only have the name; match it to the agent so filters cover history.
        "UPDATE activity_log SET agent_id = (SELECT a.id FROM agents a "
        " WHERE lower(a.name) = lower(activity_log.actor) OR lower(a.username) = lower(activity_log.actor) LIMIT 1) "
        "WHERE agent_id IS NULL",
    ),
    40: (
        # Dues are whole rupees: round each open bill's unpaid part up (₹588.82 -> ₹589).
        "UPDATE bills SET "
        " total_paise = paid_paise + ((total_paise - paid_paise + 99) / 100) * 100, "
        " amount_paise = paid_paise + ((total_paise - paid_paise + 99) / 100) * 100 - gst_paise "
        "WHERE status IN ('pending', 'partial') AND total_paise > paid_paise "
        " AND (total_paise - paid_paise) % 100 != 0",
    ),
}

_write_lock = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Read/write connection. Callers manage their own transactions via `transaction`."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction — the worker thread and web requests share one file."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        conn = _connect(settings.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _migration_error_ok(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "duplicate column" in msg or "already exists" in msg


def _apply_migrations(conn: sqlite3.Connection, *, from_version: int, ignore_duplicates: bool) -> int:
    current = from_version
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[version]:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if ignore_duplicates and _migration_error_ok(exc):
                        continue
                    raise
            set_setting(conn, "schema_version", str(version))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def _bootstrap_data(conn: sqlite3.Connection) -> None:
    from .auth import ensure_admin_agent, grant_collector_complaints, grant_collector_inventory
    from .billing import ensure_railtel_exclusive_gst
    from .field import backfill_agent_ids
    from .inventory import ensure_catalog
    from .iptv_plans import ensure_iptv_packages, ensure_iptv_subscriptions
    from .ott_plans import ensure_ott_packages

    ensure_admin_agent(conn)
    grant_collector_complaints(conn)
    grant_collector_inventory(conn)
    ensure_catalog(conn)
    ensure_iptv_packages(conn)
    ensure_iptv_subscriptions(conn)
    ensure_ott_packages(conn)
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bills_followup ON bills(collect_later, status)"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_loc_agent "
            "ON agent_locations(agent_id, recorded_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_collector "
            "ON payments(collected_agent_id)"
        )
    except sqlite3.OperationalError:
        pass
    backfill_agent_ids(conn)
    ensure_railtel_exclusive_gst(conn)


def init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
    settings.railtel_invoice_dir.mkdir(parents=True, exist_ok=True)
    settings.whatsapp_web_session_dir.mkdir(parents=True, exist_ok=True)
    (settings.screenshot_dir.parent / "settlement_proofs").mkdir(parents=True, exist_ok=True)
    fresh = not settings.db_path.exists() or settings.db_path.stat().st_size == 0

    with connection() as conn:
        conn.executescript(SCHEMA)
        if fresh:
            set_setting(conn, "schema_version", "1")
            _apply_migrations(conn, from_version=1, ignore_duplicates=True)
        else:
            try:
                current = int(get_setting(conn, "schema_version", "1"))
            except ValueError:
                current = 1
            _apply_migrations(conn, from_version=current, ignore_duplicates=False)
        _bootstrap_data(conn)


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log_activity(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    actor: str | None = None,
    customer_id: int | None = None,
    connection_id: int | None = None,
    meta_json: str | None = None,
    agent_id: int | None = None,
) -> None:
    actor = actor or settings.operator
    live_agent = _current_agent_id.get()
    agent_id = agent_id or live_agent
    if not agent_id and actor:
        row = conn.execute(
            "SELECT id FROM agents WHERE lower(name) = lower(?) OR lower(username) = lower(?) LIMIT 1",
            (actor, actor),
        ).fetchone()
        agent_id = int(row[0]) if row else None
    loc = _activity_location(conn, agent_id, live=bool(live_agent and live_agent == agent_id), meta_json=meta_json)
    conn.execute(
        "INSERT INTO activity_log(at, actor, kind, customer_id, connection_id, message, meta_json, "
        "agent_id, lat, lng, loc_accuracy, loc_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), actor, kind, customer_id, connection_id, message, meta_json,
         agent_id, loc[0], loc[1], loc[2], loc[3]),
    )


def _activity_location(
    conn: sqlite3.Connection, agent_id: int | None, *, live: bool, meta_json: str | None
) -> tuple:
    """Where the agent was: latest GPS fix during a request, or the job's request point."""
    none = (None, None, None, None)
    if not agent_id:
        return none
    try:
        if live:
            cutoff = (datetime.now() - ACTIVITY_LOCATION_MAX_AGE).strftime("%Y-%m-%d %H:%M:%S")
            row = conn.execute(
                "SELECT lat, lng, accuracy, recorded_at FROM agent_locations "
                "WHERE agent_id = ? AND recorded_at >= ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (agent_id, cutoff),
            ).fetchone()
            return tuple(row) if row else none
        job_id = (json.loads(meta_json) or {}).get("job_id") if meta_json else None
        if job_id:
            row = conn.execute(
                "SELECT lat, lng, loc_accuracy, loc_at FROM activity_log "
                "WHERE kind = 'job_created' AND lat IS NOT NULL AND meta_json LIKE ? ORDER BY id DESC LIMIT 1",
                (f'%"job_id": {int(job_id)}%',),
            ).fetchone()
            return tuple(row) if row else none
    except (sqlite3.OperationalError, ValueError, TypeError, AttributeError):
        return none
    return none
