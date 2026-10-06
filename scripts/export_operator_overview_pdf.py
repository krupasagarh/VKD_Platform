"""Export VK_Platform_Operator_Overview.md to PDF."""
from __future__ import annotations

import os
import re
from pathlib import Path

from fpdf import FPDF

PROJECT = Path(__file__).resolve().parent.parent
MD_PATH = PROJECT / "docs" / "VK_Platform_Operator_Overview.md"
PDF_PATH = PROJECT / "docs" / "VK_Platform_Operator_Overview.pdf"
FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def parse_sections(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    sections: list[tuple[str, list[str]]] = []
    title = ""
    current_heading = ""
    bullets: list[str] = []

    for line in text.splitlines():
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if current_heading:
                sections.append((current_heading, bullets))
            current_heading = line[3:].strip()
            bullets = []
            continue
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif re.match(r"^\d+\.\s", line):
            bullets.append(line.strip())

    if current_heading:
        sections.append((current_heading, bullets))
    return title, sections


class OverviewPDF(FPDF):
    def __init__(self) -> None:
        super().__init__()
        self.set_auto_page_break(auto=True, margin=18)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("ArialUni", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def build_pdf() -> None:
    text = MD_PATH.read_text(encoding="utf-8")
    doc_title, sections = parse_sections(text)

    pdf = OverviewPDF()
    pdf.add_font("ArialUni", "", str(FONT_DIR / "arial.ttf"))
    pdf.add_font("ArialUni", "B", str(FONT_DIR / "arialbd.ttf"))
    pdf.add_font("ArialUni", "I", str(FONT_DIR / "ariali.ttf"))
    pdf.set_margins(18, 18, 18)
    pdf.add_page()

    pdf.set_font("ArialUni", "B", 18)
    pdf.set_text_color(20, 40, 90)
    pdf.multi_cell(0, 10, doc_title)
    pdf.ln(2)

    pdf.set_font("ArialUni", "I", 10)
    pdf.set_text_color(60, 60, 60)
    intro = (
        "Billing, CRM, and provider automation for Indian LCOs running Hathway, Railtel, "
        "IPTV (ANT), and OTT (SmartPlay) from one office."
    )
    pdf.multi_cell(0, 5.5, intro)
    pdf.ln(4)

    for heading, bullets in sections:
        if heading.startswith("---") or not bullets and heading == "---":
            continue
        pdf.set_font("ArialUni", "B", 12)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 7, heading)
        pdf.ln(1)

        pdf.set_font("ArialUni", "", 10)
        pdf.set_text_color(40, 40, 40)
        for item in bullets:
            pdf.set_x(pdf.l_margin + 2)
            pdf.multi_cell(0, 5.2, f"\u2022  {item}")
        pdf.ln(3)

    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(PDF_PATH))
    print(f"Wrote {PDF_PATH} ({PDF_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    build_pdf()
