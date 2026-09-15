"""Pull SmartPlay OTT subscribers into the live VK Platform database.

Hits portal.smartplaytv.in with SMARTPLAY_USER / SMARTPLAY_PASS from .env,
maps each 10-digit mobile onto an existing customer (extra `ott` connection;
never overwrites ANT IPTV), and stores the dealer wallet snapshot.

  python scripts/sync_smartplay.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
AGENT = PROJECT.parent / "railtel_debugger" / "vk_agent"
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(AGENT))

os.environ.setdefault("VK_PLATFORM_UPSTREAM_MODE", os.environ.get("VK_PLATFORM_UPSTREAM_MODE") or "simulate")

from app.db import init_db, transaction  # noqa: E402
from app.money import now_iso  # noqa: E402
from app.ott_plans import sync_smartplay_subscribers  # noqa: E402


def main() -> int:
    init_db()
    from smartplay_portal import SmartPlayError, fetch_smartplay_snapshot

    print("Logging into SmartPlay and reading customers…")
    try:
        snap = fetch_smartplay_snapshot()
    except SmartPlayError as exc:
        print(f"SmartPlay login failed: {exc}")
        return 1

    rows = list(snap.get("subscribers") or [])
    print(f"Portal: {snap.get('wallet_balance') or 'no wallet'} · "
          f"{snap.get('total') or '?'} total / {snap.get('active') or '?'} active / "
          f"{snap.get('expired') or '?'} expired · {len(rows)} view pages")

    stamp = now_iso()
    with transaction() as conn:
        mapped = sync_smartplay_subscribers(conn, rows)
        conn.execute(
            "INSERT INTO provider_status(provider, wallet_balance, active_count, "
            "inactive_count, total_count, operator_name, checked_at, error) "
            "VALUES('ott', ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "wallet_balance = excluded.wallet_balance, active_count = excluded.active_count, "
            "inactive_count = excluded.inactive_count, total_count = excluded.total_count, "
            "operator_name = excluded.operator_name, checked_at = excluded.checked_at, error = NULL",
            (
                snap.get("wallet_balance") or "",
                snap.get("active") or "",
                snap.get("expired") or "",
                snap.get("total") or str(len(rows)),
                snap.get("operator") or "",
                stamp,
            ),
        )
        local = conn.execute(
            "SELECT COUNT(*) AS n FROM connections WHERE provider = 'ott'"
        ).fetchone()["n"]

    print(
        f"Mapped {mapped['total']}: {mapped['linked']} linked, "
        f"{mapped['created']} new customers, {mapped['skipped']} skipped. "
        f"{local} ott connections on file."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
