#!/usr/bin/env python3
"""One-off probe: scrape My Subscribers and print expiry summary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # vk_digital_hub
sys.path.insert(0, str(ROOT / "railtel_debugger" / "vk_agent"))
sys.path.insert(0, str(ROOT / "railtel_debugger"))
sys.path.insert(0, str(ROOT / "vk_platform"))

from app.money import parse_date, today  # noqa: E402
from app.railtel_sync import summarize_subscriber_rows  # noqa: E402
from vk_agent.portal import check_railtel_sync_subscribers  # noqa: E402


def main() -> int:
    result = check_railtel_sync_subscribers()
    rows = result.get("subscribers") or []
    summary = summarize_subscriber_rows(rows, on_date=today())
    red_sample = [
        {
            "username": r.get("username"),
            "renewal_date": r.get("renewal_date"),
            "status": r.get("status"),
        }
        for r in rows
        if r.get("is_red")
    ][:8]
    out = {
        "success": result.get("success"),
        "error": result.get("error"),
        "message": result.get("message"),
        "row_count": len(rows),
        "summary": summary,
        "red_sample": red_sample,
    }
    print(json.dumps(out, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
