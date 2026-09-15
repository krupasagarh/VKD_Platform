"""Fix WhatsApp PDF attach + preview detection."""
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")


def replace_func(name: str, new_body: str) -> None:
    global text
    start = text.index(f"def {name}")
    next_def = text.index("\ndef ", start + 1)
    text = text[:start] + new_body.rstrip() + "\n\n" + text[next_def + 1 :]


replace_func(
    "_attach_preview_ready",
    '''def _attach_preview_ready(page, path: Path) -> bool:
    """Upload preview open — green Send in bottom bar (not main-chat mic)."""
    stem = path.stem.lower()
    name = path.name.lower()
    return bool(
        page.evaluate(
            r"""(stem, name) => {
                if (/edit pdf/i.test(document.body.innerText || '')) return false;
                if (document.querySelector('[data-testid="media-caption-input-container"]'))
                    return true;
                const header = (document.querySelector('#main header')?.innerText || '').toLowerCase();
                const vh = innerHeight;
                let sendBottom = false;
                for (const icon of document.querySelectorAll('span[data-icon]')) {
                    const n = (icon.getAttribute('data-icon') || '').toLowerCase();
                    if (!n.includes('send') || /mic|ptt|voice/.test(n)) continue;
                    const r = icon.getBoundingClientRect();
                    if (r.top > vh * 0.65 && r.width > 8) sendBottom = true;
                }
                if (!sendBottom) return false;
                if (/no preview available|file selected|adding a caption/i.test(document.body.innerText || ''))
                    return true;
                if (header.includes(stem.slice(0, 10)) || header.includes(name.slice(0, 12)))
                    return true;
                // PDF preview canvas/iframe in main panel + send = attached
                const main = document.querySelector('#main');
                if (main && (main.querySelector('canvas') || main.querySelector('embed[type="application/pdf"]')))
                    return true;
                return sendBottom;
            }""",
            stem[:32],
            name[:48],
        )
    )''',
)

replace_func(
    "_close_blocking_pdf_viewer",
    '''def _close_blocking_pdf_viewer(page, path: Path | None = None) -> bool:
    """Close old PDF viewer / Calls promo so chat compose (+) is reachable."""
    if path and _upload_overlay_open(page) and _attach_preview_ready(page, path):
        return True
    if _chat_compose_visible(page):
        return True

    if _voice_calling_screen(page):
        print("   [whatsapp] Dismissing Calls screen...", flush=True)
        for sel in ('span[data-icon="back"]', '#main header span[data-icon="x"]'):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=800):
                    loc.click(timeout=2000)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue

    if page.evaluate("() => /edit pdf/i.test(document.body.innerText || '')"):
        print("   [whatsapp] Closing PDF viewer...", flush=True)
        for _ in range(8):
            if _chat_compose_visible(page):
                return True
            _click_close_buttons(page)
            page.wait_for_timeout(500)

    return _chat_compose_visible(page)''',
)

replace_func(
    "_open_chat_panel",
    '''def _open_chat_panel(page, last10: str) -> bool:
    """Open the conversation — right panel must show compose footer, not Calls promo."""
    print("   [whatsapp] Opening chat...", flush=True)

    # Enter often opens the first search hit
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        if _chat_compose_visible(page):
            return True
    except Exception:
        pass

    for sel in (
        f'#pane-side [data-testid="cell-frame-container"]:has-text("{last10}")',
        f'#pane-side span[title*="{last10}"]',
        f'xpath={_FIRST_CHAT_XPATH}',
        '#pane-side [data-testid="cell-frame-container"]',
        '#pane-side [role="listitem"]',
    ):
        try:
            row = page.locator(sel).first
            if not row.is_visible(timeout=2500):
                continue
            row.click(timeout=8000)
            page.wait_for_timeout(1200)
            if _chat_compose_visible(page):
                return True
            _click_close_buttons(page)
            page.wait_for_timeout(600)
            if _chat_compose_visible(page):
                return True
            row.dblclick(timeout=5000)
            page.wait_for_timeout(1200)
            if _chat_compose_visible(page):
                return True
        except Exception:
            continue
    return _chat_compose_visible(page)''',
)

replace_func(
    "_open_chat_by_search",
    '''def _open_chat_by_search(page, wa_phone: str) -> None:
    """Search sidebar for the number and open that chat."""
    last10 = wa_phone[-10:]
    print(f"   [whatsapp] Searching chat for {last10}...", flush=True)

    for attempt in (1, 2):
        _fresh_whatsapp_home(page)

        search = _focus_whatsapp_search(page)
        if search is None:
            for sel in ('span[data-icon="search"]', '[aria-label="Search"]'):
                try:
                    page.locator(sel).first.click(timeout=3000)
                    page.wait_for_timeout(400)
                    break
                except Exception:
                    continue
            search = _focus_whatsapp_search(page)
        if search is None:
            _debug_screenshot(page, "whatsapp_search_box_not_found")
            raise RuntimeError('WhatsApp search box not found (xpath //*[@id="_r_d_"]').')

        _type_in_search(page, search, last10)
        page.wait_for_timeout(2000)

        if _open_chat_panel(page, last10):
            _click_close_buttons(page)
            page.wait_for_timeout(400)
            if _chat_compose_visible(page):
                print("   [whatsapp] Chat open.", flush=True)
                return

        if attempt == 1:
            print("   [whatsapp] Trying send?phone= fallback...", flush=True)
            page.goto(
                f"https://web.whatsapp.com/send?phone=91{last10}",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            page.wait_for_timeout(2000)
            _dismiss_continue_to_chat(page)
            _click_close_buttons(page)
            page.wait_for_timeout(600)
            if _chat_compose_visible(page):
                print("   [whatsapp] Chat open (via send link).", flush=True)
                return

        print(f"   [whatsapp] Chat not open yet (attempt {attempt})...", flush=True)

    _debug_screenshot(page, "whatsapp_stuck_in_viewer")
    raise RuntimeError(
        f"Could not open WhatsApp chat for {last10}. "
        "Right panel should show message box, not Calls screen."
    )''',
)

replace_func(
    "_attach_pdf",
    '''def _attach_pdf(page, path: Path) -> None:
    _click_attach_button(page)
    page.wait_for_timeout(600)

    doc_selectors = (
        '[data-testid="mi-document-item"]',
        '[data-testid="mi-document"]',
        'li[role="button"]:has-text("Document")',
        'div[role="button"]:has-text("Document")',
        'span:has-text("Document")',
    )

    # Prefer file-chooser hook when Document is clicked
    for sel in doc_selectors:
        try:
            item = page.locator(sel).first
            if not item.is_visible(timeout=2000):
                continue
            with page.expect_file_chooser(timeout=8000) as fc_info:
                item.click(timeout=5000)
            fc_info.value.set_files(str(path))
            page.wait_for_timeout(800)
            return
        except Exception:
            continue

    # Fallback: click Document then set hidden inputs
    for sel in doc_selectors:
        try:
            item = page.locator(sel).first
            if item.is_visible(timeout=1500):
                item.click(timeout=5000)
                page.wait_for_timeout(500)
                break
        except Exception:
            continue

    inputs = page.locator('input[type="file"]')
    n = inputs.count()
    for i in range(n - 1, -1, -1):
        try:
            inputs.nth(i).set_input_files(str(path), timeout=10000)
            page.wait_for_timeout(800)
            return
        except Exception:
            continue

    raise RuntimeError(f"Could not attach {path.name} - file picker did not open.")''',
)

replace_func(
    "_wait_attachment_overlay",
    '''def _wait_attachment_overlay(page, path: Path) -> None:
    """Wait until the newly attached PDF preview is ready."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _attach_preview_ready(page, path):
            print(f"   [whatsapp] Preview ready: {path.name}", flush=True)
            return
        page.wait_for_timeout(500)

    # Last chance: send button visible in bottom bar — proceed anyway
    if page.evaluate(
        r"""() => {
            const vh = innerHeight;
            for (const icon of document.querySelectorAll('span[data-icon]')) {
                const n = (icon.getAttribute('data-icon') || '').toLowerCase();
                if (!n.includes('send') || /mic|ptt|voice/.test(n)) continue;
                const r = icon.getBoundingClientRect();
                if (r.top > vh * 0.65 && r.width > 8) return true;
            }
            return false;
        }"""
    ):
        print(f"   [whatsapp] Preview assumed ready (send button visible)", flush=True)
        return

    _debug_screenshot(page, "whatsapp_preview_timeout")
    raise RuntimeError(f"Attachment preview did not open for {path.name}")''',
)

p.write_text(text, encoding="utf-8")
print("patched whatsapp attach flow")
