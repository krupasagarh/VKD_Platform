from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")

old_attach = '''def _attach_pdf(page, path: Path) -> None:
    footer = page.locator("footer").first
    attach = footer.locator(
        '[data-testid="attach-menu-plus"], span[data-icon="plus"], span[data-icon="clip"], '
        '[aria-label="Attach"], [title="Attach"]'
    ).last
    attach.click(timeout=8000)
    page.wait_for_timeout(400)

    inputs = page.locator('input[type="file"]')
    for i in range(min(inputs.count(), 6)):
        try:
            inputs.nth(i).set_input_files(str(path), timeout=8000)
            return
        except Exception:
            continue

    for sel in (
        '[data-testid="mi-document-item"]',
        '[data-testid="mi-document"]',
        'li[role="button"]:has-text("Document")',
        'div[role="button"]:has-text("Document")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                loc.click(timeout=3000)
                page.wait_for_timeout(300)
                break
        except Exception:
            continue

    page.locator('input[type="file"]').last.set_input_files(str(path), timeout=10000)'''

new_attach = '''def _attach_pdf(page, path: Path) -> None:
    footer = page.locator("footer").first
    attach = footer.locator(
        '[data-testid="attach-menu-plus"], span[data-icon="plus"], span[data-icon="clip"], '
        '[aria-label="Attach"], [title="Attach"]'
    ).last
    attach.click(timeout=8000)
    page.wait_for_timeout(400)

    # Document menu item first — sets the correct accept filter for PDFs.
    for sel in (
        '[data-testid="mi-document-item"]',
        '[data-testid="mi-document"]',
        'li[role="button"]:has-text("Document")',
        'div[role="button"]:has-text("Document")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=3000)
                page.wait_for_timeout(400)
                break
        except Exception:
            continue

    inputs = page.locator('input[type="file"]')
    for i in range(min(inputs.count(), 6)):
        try:
            inputs.nth(i).set_input_files(str(path), timeout=8000)
            page.wait_for_timeout(800)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return
        except Exception:
            continue

    page.locator('input[type="file"]').last.set_input_files(str(path), timeout=10000)
    page.wait_for_timeout(800)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass'''

old_wait = '''def _wait_pdf_preview(page) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ready = page.evaluate(
            """() => {
                const hasEditPdf = [...document.querySelectorAll('button, [role="button"], span, div')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));
                const sendIcon = [...document.querySelectorAll('span[data-icon]')].find(s => {
                    const icon = (s.getAttribute('data-icon') || '').toLowerCase();
                    if (!icon.includes('send') || /mic|ptt|voice/.test(icon)) return false;
                    return s.getBoundingClientRect().top > innerHeight * 0.45;
                });
                return !!(hasEditPdf && sendIcon);
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

old_closed = '''def _preview_closed(page, *, timeout_ms: int = 8000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        still_open = page.evaluate(
            """() => [...document.querySelectorAll('button, [role="button"]')]
                .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''))"""
        )
        if not still_open:
            return True
        page.wait_for_timeout(400)
    return False'''

new_closed = '''def _preview_closed(page, *, timeout_ms: int = 8000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        still_open = page.evaluate(
            """() => {
                const body = document.body.innerText || '';
                if (/file selected|add a caption/i.test(body)) return true;
                return [...document.querySelectorAll('button, [role="button"]')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));
            }"""
        )
        if not still_open:
            return True
        page.wait_for_timeout(400)
    return False'''

for label, old, new in (
    ("attach", old_attach, new_attach),
    ("wait", old_wait, new_wait),
    ("closed", old_closed, new_closed),
):
    if old not in text:
        raise SystemExit(f"{label} not found")
    text = text.replace(old, new)

p.write_text(text, encoding="utf-8")
print("patched2 ok")
