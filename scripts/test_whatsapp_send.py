"""One-off: send an existing invoice PDF to RAILTEL_INVOICE_WHATSAPP_TEST."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.whatsapp_send import send_whatsapp_document

PDF = ROOT / "data" / "railtel_invoices" / "Megha K Sep2026_RWKA09-26-013625.pdf"
PHONE = os.getenv("RAILTEL_INVOICE_WHATSAPP_TEST", "7259316656")

if __name__ == "__main__":
    print("Sending", PDF.name, "to", PHONE)
    print(send_whatsapp_document(PHONE, PDF, caption=""))
