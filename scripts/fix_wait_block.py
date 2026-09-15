from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
t = p.read_text(encoding="utf-8")

broken = '''        ready = page.evaluate(
            r""") => {
                const body = document.body.innerText || '';
                if (/edit pdf/i.test(body)) return true;
                if (/file selected|add a caption/i.test(body)) return true;
                if (/no preview available/i.test(body) && /\\.pdf/i.test(body)) return true;
                if (document.querySelector('[data-testid="media-caption-input-container"]')) return true;
                return false;
            "
        )'''

fixed = '''        ready = page.evaluate(
            r"""() => {
                const body = document.body.innerText || '';
                if (/edit pdf/i.test(body)) return true;
                if (/file selected|add a caption/i.test(body)) return true;
                if (/no preview available/i.test(body) && /\\.pdf/i.test(body)) return true;
                if (document.querySelector('[data-testid="media-caption-input-container"]')) return true;
                return false;
            }"""
        )'''

if broken not in t:
    raise SystemExit("broken block not found")
p.write_text(t.replace(broken, fixed), encoding="utf-8")
print("fixed")
