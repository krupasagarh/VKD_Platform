"""Run VK Platform on the isolated demo database (port 8801 only).

Your live app stays on port 8800 with vk_platform.db.

  python scripts/seed_demo.py --reset   # once
  python run_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEMO_DB = PROJECT_DIR / "data" / "vk_platform_demo.db"
DEMO_PORT = 8801
LIVE_PORT = 8800

os.environ["VK_PLATFORM_DB"] = str(DEMO_DB)
os.environ["VK_PLATFORM_PORT"] = str(DEMO_PORT)
os.environ["VK_PLATFORM_UPSTREAM_MODE"] = "simulate"
os.environ["VK_PLATFORM_WORKER_ENABLED"] = "0"
os.environ["WHATSAPP_WEB_AUTO_SEND"] = "0"
os.environ.setdefault("VK_PLATFORM_OPERATOR", "Demo Operator")

if not DEMO_DB.is_file():
    print("Demo database missing. Run first:")
    print("  python scripts/seed_demo.py --reset")
    sys.exit(1)

if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import argparse

import uvicorn

from app.config import is_demo_instance, settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run VK Platform demo instance")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--port",
        type=int,
        default=DEMO_PORT,
        help=f"Demo port (default {DEMO_PORT}; do not use {LIVE_PORT})",
    )
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.port == LIVE_PORT:
        print(f"Error: port {LIVE_PORT} is for your LIVE app (run.py). Demo uses port {DEMO_PORT}.")
        print(f"  python run_demo.py          # starts on {DEMO_PORT}")
        sys.exit(1)

    if not is_demo_instance():
        print(f"Error: expected demo database {DEMO_DB.name}, got {settings.db_path.name}")
        sys.exit(1)

    host_show = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print("=" * 56)
    print("VK Platform DEMO (isolated from your live data)")
    print(f"  Port    : {args.port}  (live app uses {LIVE_PORT})")
    print(f"  Desktop : http://{host_show}:{args.port}/")
    print(f"  Mobile  : http://{host_show}:{args.port}/v2/")
    print("  Login   : demo / demo123")
    print(f"  Database: {DEMO_DB}")
    print(f"  Mode    : {settings.upstream_mode}")
    print("=" * 56)
    if args.host == "0.0.0.0":
        print(f"On your phone (same Wi‑Fi): http://YOUR-LAN-IP:{args.port}/v2/")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
