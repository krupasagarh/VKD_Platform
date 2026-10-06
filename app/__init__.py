"""VK Platform — a small billing/CRM for a cable TV and broadband operator."""
from __future__ import annotations

import sys

__version__ = "0.1.0"


def _windows_utf8_stdio() -> None:
    """Stop portal emoji prints (⚠️ 🖱️) crashing Hathway/Railtel jobs on Windows cp1252."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_windows_utf8_stdio()
