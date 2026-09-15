"""Send WhatsApp messages with document attachments via WhatsApp Web (Playwright).

All Playwright work runs on one dedicated thread so the browser session can be
reused safely (sync Playwright cannot be shared across threads).

Optional: attach to your own Chrome via WHATSAPP_WEB_CDP_URL (see .env.example).
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .messaging import normalize_wa_phone

log = logging.getLogger("vk_platform.whatsapp")

_wa_cmd_queue: queue.Queue = queue.Queue()
_wa_worker: threading.Thread | None = None
_wa_worker_lock = threading.Lock()


@dataclass
class _WaBrowser:
    playwright: object | None = None
    context: object | None = None
    page: object | None = None
    logged_in: bool = False
    mode: str = ""  # persistent | cdp
    worker_thread_id: int | None = None


_session = _WaBrowser()


class _WaCmdResult:
    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value = None
        self.error: BaseException | None = None


def _wa_headless() -> bool:
    return (os.getenv("WHATSAPP_WEB_HEADLESS") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _wa_reuse_browser() -> bool:
    return (os.getenv("WHATSAPP_WEB_REUSE_BROWSER") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _wa_cdp_url() -> str:
    return (os.getenv("WHATSAPP_WEB_CDP_URL") or "").strip()


def _playwright_installed() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def _is_thread_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "cannot switch to a different thread" in msg or "greenlet" in msg


def _discard_stale_session() -> None:
    global _session
    _session = _WaBrowser()


def _ensure_wa_worker() -> None:
    global _wa_worker
    with _wa_worker_lock:
        if _wa_worker and _wa_worker.is_alive():
            return
        if _wa_worker and not _wa_worker.is_alive():
            _discard_stale_session()
            _wa_worker = None
        _wa_worker = threading.Thread(
            target=_wa_worker_loop, name="whatsapp-sender", daemon=True
        )
        _wa_worker.start()


def _dispatch(fn, /, *args, timeout: float = 600.0, **kwargs):
    _ensure_wa_worker()
    result = _WaCmdResult()
    _wa_cmd_queue.put((fn, args, kwargs, result))
    if not result.event.wait(timeout=timeout):
        raise TimeoutError(f"WhatsApp operation timed out after {int(timeout)}s")
    if result.error is not None:
        raise result.error
    return result.value


def _wa_worker_loop() -> None:
    global _session
    owner = threading.get_ident()
    if _session.worker_thread_id not in (None, owner):
        _discard_stale_session()
    _session.worker_thread_id = owner

    while True:
        item = _wa_cmd_queue.get()
        if item is None:
            _close_whatsapp_session_impl()
            break
        fn, args, kwargs, result = item
        try:
            result.value = fn(*args, **kwargs)
        except BaseException as exc:
            if _is_thread_error(exc):
                log.warning("WhatsApp Playwright thread mismatch - resetting session")
                _close_whatsapp_session_impl()
            result.error = exc
        finally:
            result.event.set()


def warm_whatsapp_session() -> None:
    if not settings.whatsapp_web_auto_send:
        return
    _dispatch(_warm_whatsapp_session_impl, timeout=180.0)


def close_whatsapp_session() -> None:
    global _wa_worker
    with _wa_worker_lock:
        worker = _wa_worker
    if worker and worker.is_alive():
        _wa_cmd_queue.put(None)
        worker.join(timeout=15.0)
    with _wa_worker_lock:
        _wa_worker = None


def send_whatsapp_document(
    phone: str | None,
    file_path: str | Path,
    caption: str = "",
) -> dict:
    """Attach a file in WhatsApp Web and press Send."""
    if not settings.whatsapp_web_auto_send:
        return {"ok": False, "error": "WhatsApp auto-send is disabled (WHATSAPP_WEB_AUTO_SEND=0)."}

    wa_phone = normalize_wa_phone(phone)
    if not wa_phone:
        return {"ok": False, "error": "No valid WhatsApp phone number."}

    path = Path(file_path).resolve()
    if not path.is_file():
        return {"ok": False, "error": f"PDF not found: {path}"}

    if not _playwright_installed():
        return {"ok": False, "error": "Playwright is not installed."}

    settings.whatsapp_web_session_dir.mkdir(parents=True, exist_ok=True)
    caption = (caption or "").strip()

    try:
        _dispatch(_send_whatsapp_document_impl, wa_phone, path, caption, timeout=90.0)
    except Exception as exc:
        log.exception("WhatsApp document send failed")
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "message": f"WhatsApp document sent to {wa_phone[-10:]}",
        "phone": wa_phone,
    }


def _warm_whatsapp_session_impl() -> None:
    page = _ensure_page()
    if _session.logged_in:
        return
    print("   [whatsapp] Warming session...", flush=True)
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=90000)
    _wait_whatsapp_ready(page, full_wait=True)
    _session.logged_in = True
    print("   [whatsapp] Session ready.", flush=True)


def _close_whatsapp_session_impl() -> None:
    global _session
    ctx = _session.context
    pw = _session.playwright
    mode = _session.mode
    owner = _session.worker_thread_id
    _session = _WaBrowser(worker_thread_id=threading.get_ident())
    if ctx and mode != "cdp":
        try:
            ctx.close()
        except Exception:
            pass
    if pw:
        try:
            pw.stop()
        except Exception:
            pass
    if owner is not None and owner != threading.get_ident():
        _session.worker_thread_id = threading.get_ident()


def _send_whatsapp_document_impl(wa_phone: str, path: Path, caption: str) -> None:
    for attempt in (1, 2):
        page = _ensure_page()
        try:
            _send_document_on_page(page, wa_phone, path, caption)
            _session.logged_in = True
            return
        except Exception as exc:
            if attempt == 1 and _is_thread_error(exc):
                _close_whatsapp_session_impl()
                continue
            try:
                _debug_screenshot(page, "whatsapp_send_error")
            except Exception:
                pass
            if not _wa_reuse_browser():
                _close_whatsapp_session_impl()
            raise


def _ensure_page():
    global _session

    owner = threading.get_ident()
    if _session.worker_thread_id not in (None, owner):
        _close_whatsapp_session_impl()

    if _wa_reuse_browser() and _session.page is not None and _session.context is not None:
        try:
            if not _session.page.is_closed():
                _trim_extra_tabs(_session.context, _session.page)
                return _session.page
        except Exception:
            _close_whatsapp_session_impl()

    if _session.context and not _wa_reuse_browser():
        _close_whatsapp_session_impl()

    from playwright.sync_api import sync_playwright

    cdp = _wa_cdp_url()
    if cdp:
        if _session.playwright is None:
            _session.playwright = sync_playwright().start()
        browser = _session.playwright.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = _pick_whatsapp_page(context) or context.new_page()
        _session.context = context
        _session.page = page
        _session.mode = "cdp"
        _session.worker_thread_id = threading.get_ident()
        _trim_extra_tabs(context, page)
        _set_viewport(page)
        return page

    if _session.playwright is None:
        _session.playwright = sync_playwright().start()
    _session.context = _session.playwright.chromium.launch_persistent_context(
        user_data_dir=str(settings.whatsapp_web_session_dir),
        headless=_wa_headless(),
        slow_mo=int(os.getenv("WHATSAPP_WEB_SLOW_MO", "0")),
        args=["--disable-blink-features=AutomationControlled"],
    )
    _session.mode = "persistent"
    _session.page = (
        _session.context.pages[0] if _session.context.pages else _session.context.new_page()
    )
    _session.worker_thread_id = threading.get_ident()
    _trim_extra_tabs(_session.context, _session.page)
    _set_viewport(_session.page)
    return _session.page


def _set_viewport(page) -> None:
    try:
        page.set_viewport_size({"width": 1280, "height": 900})
    except Exception:
        pass


def _pick_whatsapp_page(context):
    for page in context.pages:
        try:
            if "web.whatsapp.com" in (page.url or ""):
                return page
        except Exception:
            continue
    if context.pages:
        page = context.pages[0]
        if "web.whatsapp.com" not in (page.url or ""):
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=90000)
        return page
    return None


def _trim_extra_tabs(context, keep_page) -> None:
    for page in list(context.pages):
        if page is keep_page or page.is_closed():
            continue
        try:
            page.close()
        except Exception:
            pass


def _debug_screenshot(page, name: str) -> None:
    try:
        out = settings.screenshot_dir / f"{name}.png"
        page.screenshot(path=str(out), full_page=True)
        print(f"   [whatsapp] Debug screenshot -> {out}", flush=True)
    except Exception:
        pass


def _dismiss_continue_to_chat(page) -> None:
    for sel in (
        'a:has-text("Continue to chat")',
        'button:has-text("Continue to chat")',
        'div[role="button"]:has-text("Continue to chat")',
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=4000)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


def _wait_whatsapp_ready(page, *, full_wait: bool) -> None:
    if not full_wait and _session.logged_in:
        try:
            page.locator("#pane-side, footer").first.wait_for(state="visible", timeout=6000)
            return
        except Exception:
            pass

    login_wait = max(30, int(os.getenv("WHATSAPP_WEB_LOGIN_WAIT_SEC", "120")))
    deadline = time.monotonic() + login_wait
    while time.monotonic() < deadline:
        for sel in ('[data-testid="chat-list"]', "#pane-side", "footer"):
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=2500)
                return
            except Exception:
                continue
        page.wait_for_timeout(800)
    raise RuntimeError("WhatsApp Web did not load - scan QR to log in.")


_SEARCH_BOX_XPATH = '//*[@id="_r_d_"]'
_FIRST_CHAT_XPATH = '//*[@id="pane-side"]/div[1]/div/div/div[2]'
_ATTACH_MENU_XPATH = '//*[@id="main"]/footer/div[2]/div/div'
_SEND_BTN_XPATH = (
    '//*[@id="app"]/div/div/div[3]/div/div[2]/div[2]/div/span/div/div/div/div[2]'
    '/div/div[2]/div[2]/span/div/div/span'
)


def _voice_calling_screen(page) -> bool:
    return bool(
        page.evaluate(
            "() => /voice and video calling is now available/i.test(document.body.innerText || '')"
        )
    )


def _chat_compose_visible(page) -> bool:
    """True when chat footer with attach (+) is usable — not stuck in PDF viewer."""
    if _voice_calling_screen(page):
        return False
    return bool(
        page.evaluate(
            r"""() => {
                if (/edit pdf/i.test(document.body.innerText || '')) return false;
                const attach = document.querySelector(
                    '#main footer span[data-icon="ic-attach-file"], #main footer span[data-icon="plus"]'
                );
                return !!(attach && attach.getBoundingClientRect().width > 4);
            }"""
        )
    )


def _attach_preview_ready(page, path: Path) -> bool:
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
    )

def _upload_overlay_open(page) -> bool:
    """New file being attached (not the same as viewing an old message PDF)."""
    return bool(
        page.evaluate(
            r"""() => {
                if (document.querySelector('[data-testid="media-caption-input-container"]'))
                    return true;
                const t = document.body.innerText || '';
                return /no preview available|file selected/i.test(t);
            }"""
        )
    )


def _click_close_buttons(page) -> None:
    """WhatsApp PDF viewer close X is top-LEFT; also try top-right and Escape."""
    page.evaluate(
        r"""() => {
            const vw = window.innerWidth;
            const icons = [...document.querySelectorAll('span[data-icon="x"], span[data-icon="close"]')];
            const score = (r) => {
                if (r.top > 120 || r.width < 1) return -1;
                if (r.left < vw * 0.35) return 100 - r.left;
                if (r.left > vw * 0.55) return 50 - r.top;
                return -1;
            };
            let best = null, bestScore = -1;
            for (const x of icons) {
                const r = x.getBoundingClientRect();
                const s = score(r);
                if (s > bestScore) { bestScore = s; best = x; }
            }
            if (best) (best.closest('button,[role=button],div[tabindex="0"]') || best).click();
        }"""
    )
    for sel in ('#main header span[data-icon="x"]', 'span[data-icon="x"]', 'span[data-icon="back"]'):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                loc.click(timeout=2000, force=True)
                break
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _close_blocking_pdf_viewer(page, path: Path | None = None) -> bool:
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

    return _chat_compose_visible(page)

def _fresh_whatsapp_home(page) -> None:
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=45000)
    _wait_whatsapp_ready(page, full_wait=False)
    page.locator("#pane-side").first.wait_for(state="visible", timeout=20000)


def _ensure_chat_compose_ready(page, path: Path | None = None) -> None:
    """Chat footer (+) visible — not stuck in old PDF viewer."""
    if _close_blocking_pdf_viewer(page, path):
        return
    _debug_screenshot(page, "whatsapp_stuck_in_viewer")
    raise RuntimeError("Stuck in PDF viewer — could not return to chat compose.")


def _click_xpath(page, xpath: str, *, timeout: float = 10000) -> bool:
    try:
        loc = page.locator(f"xpath={xpath}").first
        if loc.count() and loc.is_visible(timeout=8000):
            loc.click(timeout=timeout)
            return True
    except Exception:
        pass
    return False


def _focus_whatsapp_search(page):
    """Focus the sidebar search box (user-provided xpath + fallbacks)."""
    candidates = (
        f'xpath={_SEARCH_BOX_XPATH}',
        f'xpath={_SEARCH_BOX_XPATH}//div[@contenteditable="true"]',
        '#side div[contenteditable="true"]',
        '[data-testid="chat-list-search"]',
        'div[title="Search input textbox"]',
    )
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=4000):
                loc.click(timeout=8000)
                return loc
        except Exception:
            continue
    return None


def _type_in_search(page, loc, text: str) -> None:
    page.wait_for_timeout(300)
    try:
        loc.fill(text)
        return
    except Exception:
        pass
    try:
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")
    except Exception:
        pass
    page.keyboard.type(text, delay=50)


def _open_chat_panel(page, last10: str) -> bool:
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
    return _chat_compose_visible(page)

def _open_chat_by_search(page, wa_phone: str) -> None:
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
            raise RuntimeError("WhatsApp search box not found.")

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
    )

def _send_document_on_page(page, wa_phone: str, path: Path, caption: str) -> None:
    size_kb = path.stat().st_size // 1024
    print(
        f"   [whatsapp] Send {path.name} ({size_kb} KB) -> {wa_phone[-10:]}...",
        flush=True,
    )

    _open_chat_by_search(page, wa_phone)
    _ensure_chat_compose_ready(page, path)

    print(f"   [whatsapp] Attach -> Document ({path.name})...", flush=True)
    _attach_pdf(page, path)
    _wait_attachment_overlay(page, path)

    attach_wait = max(5000, int(os.getenv("WHATSAPP_WEB_AFTER_ATTACH_MS", "5000")))
    print(f"   [whatsapp] Waiting {attach_wait // 1000}s for preview...", flush=True)
    page.wait_for_timeout(attach_wait)

    skip = (os.getenv("WHATSAPP_WEB_SKIP_CAPTION") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    if caption and not skip:
        _fill_caption(page, caption)

    print("   [whatsapp] Clicking send...", flush=True)
    sent_click = False
    for attempt in range(1, 4):
        if _click_overlay_send(page):
            sent_click = True
            page.wait_for_timeout(1200)
            if _wait_until_sent(page, path, timeout_sec=8):
                break
        print(f"   [whatsapp] Send click attempt {attempt}...", flush=True)
        page.wait_for_timeout(800)
    if not sent_click:
        _debug_screenshot(page, "whatsapp_send_error")
        raise RuntimeError("Send button not found on attachment preview.")

    if not _wait_until_sent(page, path, timeout_sec=15):
        _click_overlay_send(page)
        page.wait_for_timeout(1500)
        if not _wait_until_sent(page, path, timeout_sec=10):
            _debug_screenshot(page, "whatsapp_send_not_confirmed")
            raise RuntimeError(
                f"PDF not visible in chat after send: {path.name} "
                f"(check {wa_phone[-10:]} on WhatsApp Web)"
            )

    print(f"   [whatsapp] Sent {path.name}", flush=True)


def _click_attach_button(page) -> None:
    """Click attach in chat footer to open Document/Photo menu."""
    if _click_xpath(page, _ATTACH_MENU_XPATH, timeout=10000):
        return

    main_footer = page.locator("#main footer").first
    for sel in (
        'span[data-icon="ic-attach-file"]',
        'span[data-icon="plus"]',
        'span[data-icon="clip"]',
        '[aria-label="Attach"]',
    ):
        loc = main_footer.locator(sel).last
        try:
            if loc.is_visible(timeout=2000):
                loc.click(timeout=8000)
                return
        except Exception:
            continue
    raise RuntimeError("Attach (+) button not found in chat footer.")


def _attach_pdf(page, path: Path) -> None:
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

    raise RuntimeError(f"Could not attach {path.name} - file picker did not open.")

def _attachment_overlay_open(page) -> bool:
    return _upload_overlay_open(page)


def _overlay_shows_file(page, path: Path) -> bool:
    """Match filename in preview header (supports truncated Adobe viewer titles)."""
    stem = path.stem.lower()
    parts = [stem[:24], stem[:16], path.name.lower()]
    return bool(
        page.evaluate(
            r"""(parts) => {
                const headers = [...document.querySelectorAll(
                    '#main header span, #main header div, #main [role="banner"] *'
                )];
                const ht = headers.map(e => (e.textContent || '')).join(' ').toLowerCase();
                return parts.some(p => p && ht.includes(p));
            }""",
            parts,
        )
    )


def _wait_attachment_overlay(page, path: Path) -> None:
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
    raise RuntimeError(f"Attachment preview did not open for {path.name}")

def _fill_caption(page, caption: str) -> None:
    try:
        box = page.locator(
            '[data-testid="media-caption-input-container"] [contenteditable="true"]'
        ).first
        if box.is_visible(timeout=2000):
            box.fill(caption[:1024])
    except Exception:
        pass


def _click_overlay_send(page) -> bool:
    """Click the green Send on the attach preview (bottom-right, never the mic)."""
    if _click_xpath(page, _SEND_BTN_XPATH, timeout=5000):
        return True

    target = page.evaluate(
        r"""() => {
            const vh = innerHeight;
            let best = null;
            for (const icon of document.querySelectorAll('span[data-icon]')) {
                const n = (icon.getAttribute('data-icon') || '').toLowerCase();
                if (!n.includes('send') || /mic|ptt|voice/.test(n)) continue;
                const r = icon.getBoundingClientRect();
                if (r.top < vh * 0.68 || r.width < 8) continue;
                const score = r.top * 10 + r.left;
                if (!best || score > best.score)
                    best = { x: r.x + r.width / 2, y: r.y + r.height / 2, score };
            }
            return best;
        }"""
    )
    if target:
        page.mouse.click(float(target["x"]), float(target["y"]))
        return True

    vh = (page.viewport_size or {}).get("height") or 900
    for sel in ('span[data-icon="wds-ic-send-filled"]', 'span[data-icon="send"]'):
        icons = page.locator(sel)
        for idx in range(icons.count() - 1, -1, -1):
            icon = icons.nth(idx)
            try:
                if not icon.is_visible(timeout=600):
                    continue
                box = icon.bounding_box()
                if not box or box["y"] < vh * 0.68:
                    continue
                btn = icon.locator("xpath=ancestor::button[1]")
                if btn.count():
                    btn.first.click(timeout=5000, force=True)
                else:
                    icon.click(timeout=5000, force=True)
                return True
            except Exception:
                continue
    return False


def _document_in_chat(page, path: Path) -> bool:
    try:
        msgs = page.locator("div.message-out")
        count = msgs.count()
        for idx in range(count - 1, max(count - 5, -1), -1):
            msg = msgs.nth(idx)
            if msg.locator(
                'span[data-icon*="document"], [data-testid="document-thumb"]'
            ).count() == 0:
                continue
            text = (msg.inner_text(timeout=1500) or "").lower()
            if path.name.lower() in text or path.stem.lower()[:15] in text:
                return True
            return True
    except Exception:
        pass
    return False


def _wait_until_sent(page, path: Path, *, timeout_sec: float = 20) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _document_in_chat(page, path):
            return True
        if not _attach_preview_ready(page, path) and not _upload_overlay_open(page):
            page.wait_for_timeout(1000)
            return _document_in_chat(page, path)
        page.wait_for_timeout(500)
    return _document_in_chat(page, path)
