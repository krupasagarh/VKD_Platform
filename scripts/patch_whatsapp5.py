from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
text = p.read_text(encoding="utf-8")

preview_js = r"""() => {
                const body = document.body.innerText || '';
                if (/edit pdf/i.test(body)) return true;
                if (/file selected|add a caption/i.test(body)) return true;
                if (/no preview available/i.test(body) && /\.pdf/i.test(body)) return true;
                if (document.querySelector('[data-testid="media-caption-input-container"]')) return true;
                return false;
            }"""

old_wait = """        ready = page.evaluate(
            r\"\"\"() => {
                const body = document.body.innerText || '';
                const hasFileBar = /file selected|add a caption/i.test(body);
                const hasEditPdf = [...document.querySelectorAll('button, [role="button"], span, div')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));
                return !!(hasFileBar || hasEditPdf);
            }\"\"\"
        )"""

new_wait = f"""        ready = page.evaluate(
            r\"\"\"{preview_js[1:-1]}\"
        )"""

old_fast = """                const body = document.body.innerText || '';
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
                return { mode: send ? 'send' : 'wait' };"""

new_fast = """                const body = document.body.innerText || '';
                const editPdf = /edit pdf/i.test(body);
                const compact = /file selected|add a caption|no preview available/i.test(body)
                    && /\\.pdf/i.test(body);
                if (editPdf) return { mode: 'full' };
                if (!compact) return { mode: 'wait' };
                const send = [...document.querySelectorAll('span[data-icon]')].find(s => {
                    const icon = (s.getAttribute('data-icon') || '').toLowerCase();
                    if (!icon.includes('send') || /mic|ptt|voice/.test(icon)) return false;
                    const r = s.getBoundingClientRect();
                    return r.top > innerHeight * 0.75 && r.width > 0;
                });
                return { mode: send ? 'send' : 'wait' };"""

old_closed = """                const body = document.body.innerText || '';
                if (/file selected|add a caption/i.test(body)) return true;
                return [...document.querySelectorAll('button, [role="button"]')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));"""

new_closed = """                const body = document.body.innerText || '';
                if (/file selected|add a caption|no preview available/i.test(body)) return true;
                if (document.querySelector('[data-testid="media-caption-input-container"]')) return true;
                return [...document.querySelectorAll('button, [role="button"]')]
                    .some(el => /edit pdf/i.test(el.textContent || el.getAttribute('aria-label') || ''));"""

old_click_min = """    vh = (page.viewport_size or {}).get("height") or 800
    min_y = vh * 0.55"""

new_click_min = """    vh = (page.viewport_size or {}).get("height") or 800
    min_y = vh * 0.75"""

for label, old, new in [
    ("wait", old_wait, new_wait),
    ("fast", old_fast, new_fast),
    ("closed", old_closed, new_closed),
    ("click", old_click_min, new_click_min),
]:
    if old not in text:
        raise SystemExit(f"{label} not found")
    text = text.replace(old, new)

p.write_text(text, encoding="utf-8")
print("patched5 ok")
