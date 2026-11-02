"""A Hathway-only installation, on a throwaway database in simulate mode.

Covers what VK_PLATFORM_PROVIDERS must guarantee for a copy sold to a cable-only
operator: Railtel / ANT / SmartPlay never reach the job queue or a portal robot,
their pages refuse to open, and menus and dashboards do not mention them.

Run: python scripts/test_provider_modules.py
"""
import http.cookiejar
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

_tmp = Path(tempfile.mkdtemp(prefix="vkp_modules_"))
os.environ["VK_PLATFORM_DB"] = str(_tmp / "modules.db")
os.environ["VK_PLATFORM_UPSTREAM_MODE"] = "simulate"
os.environ["VK_PLATFORM_WORKER_ENABLED"] = "0"
os.environ["WHATSAPP_WEB_AUTO_SEND"] = "0"
os.environ["VK_PLATFORM_PASSWORD"] = "modules-test"
os.environ["VK_PLATFORM_PROVIDERS"] = "hathway"

from app import config  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, transaction  # noqa: E402
from app.money import now_iso  # noqa: E402
from app.upstream import jobs as q  # noqa: E402
from app.upstream.providers import UpstreamUnsupported, run_action  # noqa: E402

assert not settings.is_live, "must not run live"

failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{': ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(label)


def parsed(raw):
    old = os.environ.get("VK_PLATFORM_PROVIDERS")
    os.environ["VK_PLATFORM_PROVIDERS"] = raw
    try:
        return config._enabled_providers()
    finally:
        os.environ["VK_PLATFORM_PROVIDERS"] = old


print("Reading VK_PLATFORM_PROVIDERS")
check("blank keeps all four", parsed("") == config.KNOWN_PROVIDERS, parsed(""))
check("hathway only", parsed("hathway") == ("hathway",))
check("aliases and spacing", parsed(" Cable , railwire ") == ("railtel", "hathway"), parsed(" Cable , railwire "))
try:
    parsed("hathawy")
    check("a typo refuses to start", False)
except ValueError as exc:
    check("a typo refuses to start", "hathawy" in str(exc), exc)
check("this run is hathway only", settings.enabled_providers == ("hathway",), settings.enabled_providers)

init_db()
stamp = now_iso()

print("Startup data")
with transaction() as conn:
    seeded = {r["provider"] for r in conn.execute("SELECT DISTINCT provider FROM packages")}
    onts = conn.execute("SELECT COUNT(*) FROM inventory_items WHERE category = 'Railtel ONT'").fetchone()[0]
    cable_stock = conn.execute("SELECT COUNT(*) FROM inventory_items WHERE code = 'stb_hathway'").fetchone()[0]
check("no ANT / SmartPlay packs seeded", not seeded & {"iptv", "ott"}, seeded)
check("no Railtel ONTs in stock list", onts == 0, onts)
check("cable stock items still seeded", cable_stock == 1, cable_stock)

from app.billing import enabled_plan_bundles  # noqa: E402

check("household bundles are Hathway only", [k for k, _ in enabled_plan_bundles()] == ["hathway"],
      enabled_plan_bundles())

with transaction() as conn:
    ids = {}
    for name, provider, uid in [
        ("Cable home", "hathway", "N70100000011"),
        ("Leftover broadband", "railtel", "ka.leftover"),
        ("Leftover IPTV", "iptv", "9876500001"),
    ]:
        cid = conn.execute(
            "INSERT INTO customers(name, status, created_at, updated_at) VALUES(?, 'active', ?, ?)",
            (name, stamp, stamp),
        ).lastrowid
        ids[provider] = conn.execute(
            "INSERT INTO connections(customer_id, provider, upstream_id, status, billing_type, "
            "amount_paise, expiry_date, created_at, updated_at) "
            "VALUES(?, ?, ?, 'active', 'prepaid', 30000, '', ?, ?)",
            (cid, provider, uid, stamp, stamp),
        ).lastrowid

print("Job queue")
with transaction() as conn:
    job_id = q.enqueue_job(conn, connection_id=ids["hathway"], action="status", needs_confirmation=False)
check("hathway jobs still queue", job_id > 0, job_id)
for provider in ("railtel", "iptv"):
    try:
        with transaction() as conn:
            q.enqueue_job(conn, connection_id=ids[provider], action="status")
        check(f"{provider} job refused", False)
    except q.ProviderNotEnabled as exc:
        check(f"{provider} job refused", True, exc)
check("a refusal flashes like a refused renew", issubclass(q.ProviderNotEnabled, q.RenewNotAllowed))
try:
    with transaction() as conn:
        q.enqueue_provider_job(conn, provider="railtel", action="wallet")
    check("railtel dealer job refused", False)
except q.ProviderNotEnabled:
    check("railtel dealer job refused", True)
with transaction() as conn:
    swept = {r["provider"] for r in q.sweep_candidates(conn, providers="both", stale_days=0)}
    railtel_only = q.sweep_candidates(conn, providers="railtel", stale_days=0)
    batch = q.enqueue_status_batch(conn, provider="railtel", connection_ids=[ids["railtel"]], requested_by="test")
check("'both' sweep covers only hathway", swept <= {"hathway"}, swept)
check("railtel sweep finds nothing", railtel_only == [], len(railtel_only))
check("railtel status batch not queued", batch is None, batch)
try:
    run_action("railtel", "status", "ka.leftover")
    check("portal robot refuses railtel", False)
except UpstreamUnsupported as exc:
    check("portal robot refuses railtel", "not enabled" in str(exc), exc)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


print("Pages, on a running server")
port = free_port()
server = subprocess.Popen(
    [sys.executable, "run.py", "--port", str(port)],
    cwd=PROJECT,
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.STDOUT,
)
base = f"http://127.0.0.1:{port}"
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    raw = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
    body = urllib.parse.urlencode({"username": "admin", "password": "modules-test", "next": "/"}).encode()
    opener.open(f"{base}/login", data=body).read()
    check("owner signs in", any(c.name == "vkp_session" for c in jar))

    def page(path):
        with opener.open(f"{base}{path}") as response:
            return response.status, response.read().decode("utf-8", "replace")

    for path in ("/", "/v2/", "/v2/menu", "/customers", "/v2/customers", "/providers", "/stbs", "/settings/agents"):
        status, html = page(path)
        leaks = [w for w in ('href="/iptv"', 'href="/ott"', 'href="/railtel-check"', 'href="/v2/railtel-check"',
                             'href="/v2/railtel-online"', 'href="/providers/online"', "Railtel only")
                 if w in html]
        check(f"{path} opens without other providers", status == 200 and not leaks, leaks or status)
    status, html = page("/")
    check("dashboard still links Hathway STBs", 'href="/stbs"' in html)

    for path in ("/iptv", "/ott", "/railtel-check", "/v2/railtel-online", "/providers/online"):
        try:
            raw.open(f"{base}{path}")
            check(f"{path} refused", False, "opened")
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("location") or ""
            check(f"{path} refused", exc.code == 303 and "not+enabled" in location, f"{exc.code} {location}")
    try:
        opener.open(f"{base}/api/railtel-online")
        check("/api/railtel-online refused", False, "opened")
    except urllib.error.HTTPError as exc:
        check("/api/railtel-online refused", exc.code == 404, exc.code)
finally:
    server.terminate()
    server.wait(timeout=10)

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("Provider module checks passed.")
