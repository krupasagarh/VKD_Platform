"""Hit every page and API endpoint of a running server and report status + size.

Run the app first, then:  python scripts/check_pages.py --port 8801
"""
from __future__ import annotations

import argparse
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

PAGES = [
    "/",
    "/customers",
    "/customers?view=due",
    "/customers?view=expiring",
    "/customers?view=expired",
    "/customers?view=hathway_live",
    "/customers?view=hathway",
    "/customers?view=iptv_live",
    "/customers?view=iptv",
    "/customers?provider=iptv",
    "/customers?view=ott_live",
    "/customers?view=ott",
    "/customers?provider=ott",
    "/stbs",
    "/stbs?view=all",
    "/stbs?view=unmapped",
    "/iptv",
    "/iptv?view=active",
    "/iptv?view=expiring",
    "/iptv?view=expired",
    "/ott",
    "/ott?view=active",
    "/ott?view=expiring",
    "/ott?view=expired",
    "/customers?q=Manoj",
    "/customers?provider=railtel",
    "/customers/new",
    "/jobs",
    "/jobs?status=awaiting_confirm",
    "/jobs?status=awaiting_otp",
    "/jobs?status=failed",
    "/providers",
    "/providers/online",
    "/bills",
    "/bills?status=",
    "/payments",
    "/payments/follow-up",
    "/payments/follow-up?kind=manual",
    "/payments/follow-up?kind=renew",
    "/field",
    "/customers?view=followup",
    "/payments?from=2026-09-01&to=2026-09-14",
    "/payments/bix",
    "/settings/bix-history",
    "/complaints",
    "/packages",
    "/packages?provider=railtel",
    "/packages?provider=hathway",
    "/packages?provider=iptv",
    "/packages?provider=ott",
    "/activity",
    "/settings/agents",
    "/settings/bix",
    "/healthz",
    "/api/stats",
    "/api/customers?q=Manoj",
    "/api/lookup?q=N70152400365",
    "/api/jobs",
    "/api/bills",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--password", default="vkdigital")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    body = urllib.parse.urlencode(
        {"username": "admin", "password": args.password, "next": "/"}
    ).encode()
    with opener.open(f"{base}/login", data=body) as response:
        print(f"login -> {response.status}")
    if not any(cookie.name == "vkp_session" for cookie in jar):
        print("FAIL: no session cookie issued")
        return 1

    # Discover a real customer and connection id to exercise the detail pages.
    extra: list[str] = []
    try:
        import json

        with opener.open(f"{base}/api/customers?view=due") as response:
            data = json.load(response)
        if data["customers"]:
            cid = data["customers"][0]["id"]
            extra.append(f"/customers/{cid}")
            with opener.open(f"{base}/api/customers/{cid}") as response:
                detail = json.load(response)
            extra.append(f"/api/customers/{cid}")
            if detail["payments"]:
                extra.append(f"/payments/{detail['payments'][0]['id']}/receipt")
    except Exception as exc:  # noqa: BLE001
        print(f"note: could not discover sample ids ({exc})")

    failures = 0
    for path in PAGES + extra:
        try:
            with opener.open(f"{base}{path}") as response:
                payload = response.read()
            marker = ""
            if b"Internal Server Error" in payload or b"Traceback" in payload:
                marker = "  <-- error in body"
                failures += 1
            print(f"  {response.status}  {len(payload):>7,} B  {path}{marker}")
        except urllib.error.HTTPError as exc:
            print(f"  {exc.code}  FAILED       {path}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ---  FAILED       {path}  ({exc})")
            failures += 1

    print()
    if failures:
        print(f"{failures} endpoint(s) failed")
        return 1
    print(f"All {len(PAGES) + len(extra)} endpoints OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
