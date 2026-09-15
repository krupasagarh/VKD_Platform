from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.whatsapp_send import _dispatch, _ensure_page

PDF = ROOT / "data" / "railtel_invoices" / "Megha K Sep2026_RWKA09-26-013625.pdf"
PHONE = os.getenv("RAILTEL_INVOICE_WHATSAPP_TEST", "7259316656")


def _run():
    page = _ensure_page()
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    page.locator("#pane-side").first.wait_for(state="visible", timeout=30000)
    for _ in range(5):
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    page.goto(f"https://web.whatsapp.com/send?phone={PHONE}", wait_until="domcontentloaded")
    page.locator("footer").first.wait_for(state="visible", timeout=30000)
    page.wait_for_timeout(1500)
    footer_icons = page.evaluate(
        """() => [...document.querySelectorAll('footer span[data-icon]')]
            .map(s => s.getAttribute('data-icon'))"""
    )
    print("footer icons:", footer_icons)
    page.screenshot(path=str(ROOT / "data/screenshots/debug_before_attach.png"))

    page.locator(
        "footer span[data-icon='plus'], footer span[data-icon='clip'], footer [aria-label='Attach']"
    ).last.click(timeout=10000)
    page.wait_for_timeout(500)
    page.locator('[data-testid="mi-document-item"], li:has-text("Document")').first.click()
    page.wait_for_timeout(400)
    page.locator('input[type="file"]').last.set_input_files(str(PDF))
    page.wait_for_timeout(3000)

    info = page.evaluate(
        r"""() => {
        const sends = [...document.querySelectorAll('span[data-icon]')]
            .filter(s => /send/i.test(s.getAttribute('data-icon')||'') && !/mic|ptt|voice/i.test(s.getAttribute('data-icon')||''))
            .map(s => {
                const r = s.getBoundingClientRect();
                return {icon: s.getAttribute('data-icon'), x: r.x, y: r.y, w: r.width, h: r.height, vis: r.width>0&&r.height>0};
            });
        const editPdf = [...document.querySelectorAll('*')].some(el => /edit pdf/i.test(el.textContent||''));
        const title = document.title;
        const header = (document.querySelector('header')?.innerText || '').slice(0,120);
        return {sends, editPdf, title, header, h: innerHeight, bodyTail: document.body.innerText.slice(-200)};
    }"""
    )
    print(info)
    page.screenshot(path=str(ROOT / "data/screenshots/debug_send_btn.png"), full_page=True)


if __name__ == "__main__":
    _dispatch(_run, timeout=120.0)
