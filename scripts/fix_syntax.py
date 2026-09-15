from pathlib import Path

p = Path(__file__).resolve().parents[1] / "app" / "whatsapp_send.py"
lines = p.read_text(encoding="utf-8").splitlines()
for i, line in enumerate(lines):
    if "WhatsApp search box not found" in line:
        lines[i] = '            raise RuntimeError("WhatsApp search box not found.")'
        print("fixed line", i + 1, "was:", line)
p.write_text("\n".join(lines) + "\n", encoding="utf-8")
