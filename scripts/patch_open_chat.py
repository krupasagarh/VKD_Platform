from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")

start = text.index("def _search_and_open_chat(page, last10: str)")
end = text.index("\n\ndef _send_document_on_page(page, wa_phone: str")

new_block = '''def _open_chat_panel(page, last10: str) -> bool:
    """Open the conversation — right panel must show compose footer, not Calls promo."""
    print("   [whatsapp] Opening chat...", flush=True)
    for sel in (
        f'#pane-side [data-testid="cell-frame-container"]:has-text("{last10}")',
        f'#pane-side span[title*="{last10}"]',
        f'xpath={_FIRST_CHAT_XPATH}',
        '#pane-side [data-testid="cell-frame-container"]',
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
    return _chat_compose_visible(page)


def _open_chat_by_search(page, wa_phone: str) -> None:
    """Search sidebar for the number and open that chat."""
    last10 = wa_phone[-10:]
    print(f"   [whatsapp] Searching chat for {last10}...", flush=True)

    for attempt in (1, 2):
        _fresh_whatsapp_home(page)

        search = _focus_whatsapp_search(page)
        if search is None:
            _debug_screenshot(page, "whatsapp_search_box_not_found")
            raise RuntimeError('WhatsApp search box not found (xpath //*[@id="_r_d_"]').')

        _type_in_search(page, search, last10)
        page.wait_for_timeout(2000)

        if _open_chat_panel(page, last10):
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
            _click_close_buttons(page)
            if _chat_compose_visible(page):
                print("   [whatsapp] Chat open (via send link).", flush=True)
                return

        print(f"   [whatsapp] Chat not open yet (attempt {attempt})...", flush=True)

    _debug_screenshot(page, "whatsapp_stuck_in_viewer")
    raise RuntimeError(
        f"Could not open WhatsApp chat for {last10}. "
        "Right panel should show message box, not Calls screen."
    )

'''

text = text[:start] + new_block + text[end:]
p.write_text(text, encoding="utf-8")
print("patched open chat")
