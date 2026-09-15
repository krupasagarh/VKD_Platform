from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")

old1 = """    skip_caption = (os.getenv("WHATSAPP_WEB_SKIP_CAPTION") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if caption and not skip_caption:
        _fill_caption(page, caption)

    if not _click_pdf_send(page):
        _debug_screenshot(page, "whatsapp_send_error")
        raise RuntimeError("Could not click send on PDF preview.")

    page.wait_for_timeout(1200)
    if not _preview_closed(page):
        _debug_screenshot(page, "whatsapp_send_not_confirmed")
        raise RuntimeError(f"Send not confirmed for {path.name}")"""

new1 = """    skip_caption = (os.getenv("WHATSAPP_WEB_SKIP_CAPTION") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    if caption and not skip_caption:
        _fill_caption(page, caption)

    if not _click_pdf_send(page):
        _debug_screenshot(page, "whatsapp_send_error")
        raise RuntimeError("Could not click send on PDF preview.")

    page.wait_for_timeout(1500)
    if not _document_sent(page) and not _preview_closed(page):
        _debug_screenshot(page, "whatsapp_send_not_confirmed")
        raise RuntimeError(f"Send not confirmed for {path.name}")"""

old2 = '''def _click_pdf_send(page) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const icons = [...document.querySelectorAll('span[data-icon]')];
                for (const icon of icons.reverse()) {
                    const name = (icon.getAttribute('data-icon') || '').toLowerCase();
                    if (!name.includes('send')) continue;
                    if (/mic|ptt|voice/.test(name)) continue;
                    const r = icon.getBoundingClientRect();
                    if (r.width < 2 || r.top < innerHeight * 0.45) continue;
                    const btn = icon.closest('button, [role="button"], div[tabindex="0"]') || icon;
                    btn.click();
                    return true;
                }
                return false;
            }"""
        )
    )'''

new2 = '''def _click_pdf_send(page) -> bool:
    """Click send inside the PDF overlay — never the main-chat microphone."""
    vh = (page.viewport_size or {}).get("height") or 800
    min_y = vh * 0.45
    for sel in ('span[data-icon="wds-ic-send-filled"]', 'span[data-icon="send"]'):
        icons = page.locator(sel)
        for idx in range(icons.count() - 1, -1, -1):
            icon = icons.nth(idx)
            try:
                if not icon.is_visible(timeout=600):
                    continue
                box = icon.bounding_box()
                if not box or box["y"] < min_y:
                    continue
                btn = icon.locator("xpath=ancestor::button[1]")
                if btn.count():
                    btn.first.click(timeout=5000)
                else:
                    icon.click(timeout=5000)
                return True
            except Exception:
                continue
    return False


def _document_sent(page) -> bool:
    try:
        last = page.locator("div.message-out").last
        if not last.is_visible(timeout=2000):
            return False
        return last.locator(
            'span[data-icon="document"], span[data-icon="document-PDF-icon"], '
            '[data-testid="document-thumb"]'
        ).count() > 0
    except Exception:
        return False'''

for label, old, new in (("old1", old1, new1), ("old2", old2, new2)):
    if old not in text:
        raise SystemExit(f"{label} not found")
    text = text.replace(old, new)

p.write_text(text, encoding="utf-8")
print("patched ok", len(text.splitlines()), "lines")
