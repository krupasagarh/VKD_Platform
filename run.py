"""Start VK Platform.

    python run.py                 # listen on the configured host/port
    python run.py --reload        # auto-reload while editing code
    python run.py --host 0.0.0.0  # reachable from your phone on the same wifi
"""
from __future__ import annotations

import argparse

import uvicorn

from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the VK Platform web app")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    args = parser.parse_args()

    print(f"VK Platform on http://{args.host}:{args.port}  (upstream mode: {settings.upstream_mode})")
    print(f"Mobile UI:  http://{args.host}:{args.port}/v2/  (sign in first)")
    if args.host in {"127.0.0.1", "localhost"}:
        print("Tip: use --host 0.0.0.0 so phones on wifi can open http://YOUR-LAN-IP:8800/v2/")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
