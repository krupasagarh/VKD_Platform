from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")

# viewport in _ensure_page
old_ensure = """    _session.page = _session.context.pages[0] if _session.context.pages else _session.context.new_page()
    _session.worker_thread_id = threading.get_ident()
    _trim_extra_tabs(_session.context, _session.page)
    return _session.page"""


new_ensure = """    _session.page = _session.context.pages[0] if _session.context.pages else _session.context.new_page()
    _session.worker_thread_id = threading.get_ident()
    _trim_extra_tabs(_session.context, _session.page)
    try:
        _session.page.set_viewport_size({"width": 1280, "height": 900})
    except Exception:
        pass
    return _session.page"""

old_send_doc = """def _send_document_on_page(page, wa_phone: str, path: Path, caption: str) -> None:
    print(f"   [whatsapp] Send {path.name} -> {wa_phone[-10:]}...", flush=True)
    _dismiss_whatsapp_modals(page)

    page.goto(
        f"https://web.whatsapp.com/send?phone={wa_phone}",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    _wait_whatsapp_ready(page, full_wait=False)
    _dismiss_continue_to_chat(page)
    _dismiss_whatsapp_modals(page)

    try:
        if page.get_by_text("Phone number shared via url is invalid", exact=False).is_visible(
            timeout=1500
        ):
            raise RuntimeError("WhatsApp says this phone number is invalid.")
    except RuntimeError:
        raise
    except Exception:
        pass

    page.locator("footer").first.wait_for(state="visible", timeout=20000)

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

    print(f"   [whatsapp] Attaching {path.name}...", flush=True)
    _attach_pdf(page, path)
    _wait_pdf_preview(page)
    print("   [whatsapp] Preview ready - sending...", flush=True)

    skip_caption = (os.getenv("WHATSAPP_WEB_SKIP_CAPTION") or "1").strip().lower() not in {
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
        raise RuntimeError(f"Send not confirmed for {path.name}")
    print(f"   [whatsapp] Sent {path.name}", flush=True)"""

new_send_doc = """def _reset_chat_for_send(page, wa_phone: str) -> None:
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=45000)
    _wait_whatsapp_ready(page, full_wait=False)
    for _ in range(4):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(200)
    page.goto(
        f"https://web.whatsapp.com/send?phone={wa_phone}",
        wait_until="domcontentloaded",
        timeout=45000,
    )
    _wait_whatsapp_ready(page, full_wait=False)
    _dismiss_continue_to_chat(page)
    _dismiss_whatsapp_modals(page)
    page.locator("footer").first.wait_for(state="visible", timeout=20000)


def _send_document_on_page(page, wa_phone: str, path: Path, caption: str) -> None:
    print(f"   [whatsapp] Send {path.name} -> {wa_phone[-10:]}...", flush=True)
    _reset_chat_for_send(page, wa_phone)

    try:
        if page.get_by_text("Phone number shared via url is invalid", exact=False).is_visible(
            timeout=1500
        ):
            raise RuntimeError("WhatsApp says this phone number is invalid.")
    except RuntimeError:
        raise
    except Exception:
        pass

    print(f"   [whatsapp] Attaching {path.name}...", flush=True)
    _attach_pdf(page, path)

    skip_caption = (os.getenv("WHATSAPP_WEB_SKIP_CAPTION") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    if caption and not skip_caption:
        _wait_pdf_preview(page)
        _fill_caption(page, caption)
    elif not _try_fast_compact_send(page):
        _wait_pdf_preview(page)

    print("   [whatsapp] Sending...", flush=True)
    if not _click_pdf_send(page):
        _debug_screenshot(page, "whatsapp_send_error")
        raise RuntimeError("Could not click send on PDF preview.")

    page.wait_for_timeout(2000)
    if not _document_sent(page) and not _preview_closed(page):
        _debug_screenshot(page, "whatsapp_send_not_confirmed")
        raise RuntimeError(f"Send not confirmed for {path.name}")
    print(f"   [whatsapp] Sent {path.name}", flush=True)"""

old_attach = """    attach = footer.locator(
        '[data-testid="attach-menu-plus"], span[data-icon="plus"], span[data-icon="clip"], '
        '[aria-label="Attach"], [title="Attach"]'
    ).last"""

new_attach = """    attach = footer.locator(
        'span[data-icon="ic-attach-file"], [data-testid="attach-menu-plus"], '
        'span[data-icon="plus"], span[data-icon="clip"], '
        '[aria-label="Attach"], [title="Attach"]'
    ).last"""

insert_after_wait = """def _try_fast_compact_send(page) -> bool:
    \"\"\"Send from the compact '1 file selected' bar before full PDF viewer opens.\"\"\"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = page.evaluate(
            r\"\"\"() => {
                const body = document.body.innerText || '';
                const compact = /file selected|add a caption/i.test(body);
                const editPdf = /edit pdf/i.test(body);
                if (editPdf) return { mode: 'full' };
                if (!compact) return { mode: 'wait' };
                const send = [...document.querySelectorAll('span[data-icon]')].find(s => {
                    const icon = (s.getAttribute('data-icon') || '').toLowerCase();
                    if (!icon.includes('send') || /mic|ptt|voice/.test(icon)) return false;
                    const r = s.getBoundingClientRect();
                    return r.top > innerHeight * 0.8 && r.width > 0;
                });
                return { mode: send ? 'send' : 'wait' };
            }\"\"\"
        )
        if state.get("mode") == "full":
            return False
        if state.get("mode") == "send":
            return _click_pdf_send(page)
        page.wait_for_timeout(250)
    return False


"""

# insert _try_fast_compact_send before _wait_pdf_preview
marker = "def _wait_pdf_preview(page) -> None:"
if insert_after_wait.strip() not in text and marker in text:
    text = text.replace(marker, insert_after_wait + marker)

replacements = [
    ("ensure", old_ensure, new_ensure),
    ("send_doc", old_send_doc, new_send_doc),
    ("attach", old_attach, new_attach),
]

for label, old, new in replacements:
    if old not in text:
        raise SystemExit(f"{label} not found")
    text = text.replace(old, new)

p.write_text(text, encoding="utf-8")
print("patched4 ok", len(text.splitlines()), "lines")
