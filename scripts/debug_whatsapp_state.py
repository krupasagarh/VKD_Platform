"""Inspect WhatsApp Web DOM after attaching a PDF (no send)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.whatsapp_send import _dispatch, _ensure_page, _wait_whatsapp_ready, _dismiss_continue_to_chat

PDF = ROOT / "data" / "railtel_invoices" / "Megha K Sep2026_RWKA09-26-013625.pdf"
PHONE = os.getenv("RAILTEL_INVOICE_WHATSAPP_TEST", "7259316656")


def _probe(page) -> None:
    state = page.evaluate(
        """() => {
        const body = document.body.innerText.slice(0, 800);
        const icons = [...document.querySelectorAll('span[data-icon]')].map(s => ({
            icon: s.getAttribute('data-icon'),
            y: Math.round(s.getBoundingClientRect().top),
            x: Math.round(s.getBoundingClientRect().left),
        })).filter(i => i.icon);
        return { body, icons: icons.slice(-20), h: innerHeight };
    }"""
    )
    print("STATE:", state)
    page.screenshot(path=str(ROOT / "data" / "screenshots" / "debug_whatsapp_probe.png"), full_page=True)


def _run():
    page = _ensure_page()
    page.goto(f"https://web.whatsapp.com/send?phone={PHONE}", wait_until="domcontentloaded", timeout=45000)
    _wait_whatsapp_ready(page, full_wait=False)
    _dismiss_continue_to_chat(page)
    page.locator("footer").first.wait_for(state="visible", timeout=20000)

    footer = page.locator("footer").first
    footer.locator('span[data-icon="plus"]').last.click(timeout=8000)
    page.wait_for_timeout(500)
    page.locator('[data-testid="mi-document-item"], li:has-text("Document")').first.click(timeout=5000)
    page.wait_for_timeout(500)
    page.locator('input[type="file"]').last.set_input_files(str(PDF), timeout=10000)
    page.wait_for_timeout(2000)
    _probe(page)


if __name__ == "__main__":
    _dispatch(_run, timeout=120.0)
