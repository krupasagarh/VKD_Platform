from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")

old_wait = '''def _wait_pdf_preview(page) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        ready = page.evaluate(
            """() => {
                const body = document.body.innerText || '';
                const hasFileBar = /file selected|add a caption|\\.pdf/i.test(body);
                const hasEditPdf = [...document.querySelectorAll('button, [role="button"], span, div')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));
                const sendIcon = [...document.querySelectorAll('span[data-icon]')].find(s => {
                    const icon = (s.getAttribute('data-icon') || '').toLowerCase();
                    if (!icon.includes('send') || /mic|ptt|voice/.test(icon)) return false;
                    const r = s.getBoundingClientRect();
                    return r.top > innerHeight * 0.65 && r.width > 0;
                });
                return !!((hasFileBar || hasEditPdf) && sendIcon);
            }"""
        )
        if ready:
            page.wait_for_timeout(500)
            return
        page.wait_for_timeout(400)
    _debug_screenshot(page, "whatsapp_preview_timeout")
    raise RuntimeError("PDF upload overlay did not appear after attach.")'''

new_wait = '''def _wait_pdf_preview(page) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        ready = page.evaluate(
            r"""() => {
                const body = document.body.innerText || '';
                const hasFileBar = /file selected|add a caption/i.test(body);
                const hasEditPdf = [...document.querySelectorAll('button, [role="button"], span, div')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));
                return !!(hasFileBar || hasEditPdf);
            }"""
        )
        if ready:
            page.wait_for_timeout(800)
            return
        page.wait_for_timeout(400)
    _debug_screenshot(page, "whatsapp_preview_timeout")
    raise RuntimeError("PDF upload overlay did not appear after attach.")'''

old_click = '''def _click_pdf_send(page) -> bool:
    """Click send inside the PDF overlay - never the main-chat microphone."""
    vh = (page.viewport_size or {}).get("height") or 800
    min_y = vh * 0.65
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
    return False'''

new_click = '''def _click_pdf_send(page) -> bool:
    """Click send on the document preview — never the main-chat microphone."""
    full_preview = False
    try:
        full_preview = page.locator(
            'button:has-text("Edit PDF"), [aria-label="Edit PDF"]'
        ).first.is_visible(timeout=1500)
    except Exception:
        pass

    vh = (page.viewport_size or {}).get("height") or 800
    min_y = vh * 0.55
    for sel in ('span[data-icon="wds-ic-send-filled"]', 'span[data-icon="send"]'):
        icons = page.locator(sel)
        for idx in range(icons.count() - 1, -1, -1):
            icon = icons.nth(idx)
            try:
                if not icon.is_visible(timeout=600):
                    continue
                box = icon.bounding_box()
                if not box:
                    continue
                if not full_preview and box["y"] < min_y:
                    continue
                btn = icon.locator("xpath=ancestor::button[1]")
                if btn.count():
                    btn.first.click(timeout=5000)
                else:
                    icon.click(timeout=5000)
                return True
            except Exception:
                continue

    if full_preview:
        try:
            page.keyboard.press("Enter")
            return True
        except Exception:
            pass
    return False'''

old_send_page = '''    page.locator("footer").first.wait_for(state="visible", timeout=20000)

    print(f"   [whatsapp] Attaching {path.name}...", flush=True)'''

new_send_page = '''    page.locator("footer").first.wait_for(state="visible", timeout=20000)

    try:
        if page.locator('button:has-text("Edit PDF"), [aria-label="Edit PDF"]').first.is_visible(
            timeout=800
        ):
            for sel in ('span[data-icon="x"]', 'span[data-icon="back"]', '[aria-label="Close"]'):
                try:
                    page.locator(sel).first.click(timeout=2000)
                    page.wait_for_timeout(500)
                    break
                except Exception:
                    continue
    except Exception:
        pass

    print(f"   [whatsapp] Attaching {path.name}...", flush=True)'''

for label, old, new in (
    ("wait", old_wait, new_wait),
    ("click", old_click, new_click),
    ("send_page", old_send_page, new_send_page),
):
    if old not in text:
        raise SystemExit(f"{label} not found")
    text = text.replace(old, new)

p.write_text(text, encoding="utf-8")
print("patched3 ok")
