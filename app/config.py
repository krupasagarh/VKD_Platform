"""Runtime configuration for VK Platform."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
WORKSPACE_DIR = PROJECT_DIR.parent
VK_AGENT_DIR = WORKSPACE_DIR / "railtel_debugger" / "vk_agent"
CABLEWAY_DATA_DIR = WORKSPACE_DIR / "cableway_automation" / "data"

# Workspace .env holds provider credentials, project .env holds platform settings and
# wins where they overlap. Neither may override a variable already in the environment:
# scripts set VK_PLATFORM_UPSTREAM_MODE=simulate to stay off the real portals, and a
# .env that clobbered that would drive live Railtel/Hathway logins from a test run.
for _key, _value in {
    **dotenv_values(WORKSPACE_DIR / ".env"),
    **dotenv_values(PROJECT_DIR / ".env"),
}.items():
    if _value is not None and _key not in os.environ:
        os.environ[_key] = _value


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip())
    except ValueError:
        return default


def _iptv_phone() -> str:
    """WhatsApp number ANT sends the partner OTP to. Never hardcode this."""
    for key in ("ANT_IPTV_PHONE", "VK_PLATFORM_IPTV_PHONE"):
        digits = re.sub(r"\D", "", os.getenv(key) or "")
        if digits.startswith("91") and len(digits) >= 12:
            digits = digits[-10:]
        if len(digits) == 10:
            return digits
    return ""


def _invoice_whatsapp_test_phone() -> str:
    digits = re.sub(r"\D", "", (os.getenv("RAILTEL_INVOICE_WHATSAPP_TEST") or "").strip())
    return digits[-10:] if len(digits) >= 10 else ""


def _float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    password: str
    secret: str
    operator: str
    db_path: Path
    screenshot_dir: Path
    railtel_invoice_dir: Path
    public_base_url: str
    railtel_invoice_whatsapp_test: str
    railtel_invoice_whatsapp_after_download: bool
    whatsapp_web_auto_send: bool
    whatsapp_web_session_dir: Path
    upstream_mode: str
    worker_enabled: bool
    job_poll_seconds: int
    job_retry_minutes: int
    railtel_account: str
    hathway_account: str
    iptv_account: str
    iptv_phone: str
    gst_percentage: float
    due_days: int
    expiring_soon_days: int
    bix_history_db: Path

    @property
    def is_live(self) -> bool:
        return self.upstream_mode == "live"


def load_settings() -> Settings:
    data_dir = PROJECT_DIR / "data"
    db_raw = (os.getenv("VK_PLATFORM_DB") or "").strip()
    db_path = Path(db_raw) if db_raw else data_dir / "vk_platform.db"

    mode = (os.getenv("VK_PLATFORM_UPSTREAM_MODE") or "simulate").strip().lower()
    if mode not in {"simulate", "live"}:
        mode = "simulate"

    hist_raw = (os.getenv("VK_BIX_HISTORY_DB") or "").strip()
    bix_history_db = (
        Path(hist_raw) if hist_raw else WORKSPACE_DIR / "bix42_export" / "vk_digital_history.db"
    )

    return Settings(
        host=(os.getenv("VK_PLATFORM_HOST") or "127.0.0.1").strip(),
        port=_int("VK_PLATFORM_PORT", 8800),
        password=(os.getenv("VK_PLATFORM_PASSWORD") or "vkdigital").strip(),
        secret=(os.getenv("VK_PLATFORM_SECRET") or "vk-platform-dev-secret").strip(),
        operator=(os.getenv("VK_PLATFORM_OPERATOR") or "operator").strip(),
        db_path=db_path,
        screenshot_dir=data_dir / "screenshots",
        railtel_invoice_dir=data_dir / "railtel_invoices",
        public_base_url=(os.getenv("VK_PLATFORM_PUBLIC_URL") or "").strip().rstrip("/"),
        railtel_invoice_whatsapp_test=_invoice_whatsapp_test_phone(),
        railtel_invoice_whatsapp_after_download=_flag(
            "RAILTEL_INVOICE_WHATSAPP_AFTER_DOWNLOAD", False
        ),
        whatsapp_web_auto_send=_flag("WHATSAPP_WEB_AUTO_SEND", True),
        whatsapp_web_session_dir=data_dir / "whatsapp_web",
        upstream_mode=mode,
        worker_enabled=_flag("VK_PLATFORM_WORKER_ENABLED", True),
        job_poll_seconds=max(1, _int("VK_PLATFORM_JOB_POLL_SECONDS", 5)),
        job_retry_minutes=max(1, _int("VK_PLATFORM_JOB_RETRY_MINUTES", 10)),
        railtel_account=(os.getenv("VK_PLATFORM_RAILTEL_ACCOUNT") or "").strip(),
        hathway_account=(os.getenv("VK_PLATFORM_HATHWAY_ACCOUNT") or "").strip(),
        iptv_account=(os.getenv("VK_PLATFORM_IPTV_ACCOUNT") or "").strip(),
        iptv_phone=_iptv_phone(),
        gst_percentage=_float("VK_PLATFORM_GST_PERCENTAGE", 0.0),
        due_days=_int("VK_PLATFORM_DUE_DAYS", 7),
        expiring_soon_days=_int("VK_PLATFORM_EXPIRING_SOON_DAYS", 7),
        bix_history_db=bix_history_db,
    )


settings = load_settings()
