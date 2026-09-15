"""Money and date helpers.

Amounts are stored as integer paise everywhere in the database so that repeated
billing arithmetic never drifts the way float rupees do.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

_MONEY_CLEAN = re.compile(r"[^0-9.\-]")

DATE_FMT = "%Y-%m-%d"
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    # Hathway reports expiry as "11-OCT-26". Two-digit years come last so a full year
    # always wins; %y reads 00-68 as 2000-2068, which covers every date we bill for.
    "%d-%b-%y",
    "%d-%B-%y",
    "%d %b %y",
    "%d-%m-%y",
    "%d/%m/%y",
    # Railtel reports expiry as "23/09/26 11:59:59 PM" — end of the last paid day.
    "%d/%m/%y %I:%M:%S %p",
    "%d/%m/%Y %I:%M:%S %p",
    "%d-%m-%y %I:%M:%S %p",
    "%d-%m-%Y %I:%M:%S %p",
    "%d/%m/%y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
)


def to_paise(value) -> int:
    """Accept rupees as str/int/float/Decimal and return integer paise."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value * 100
    if isinstance(value, Decimal):
        dec = value
    else:
        cleaned = _MONEY_CLEAN.sub("", str(value)).strip()
        if cleaned in {"", "-", ".", "-."}:
            return 0
        try:
            dec = Decimal(cleaned)
        except Exception:
            return 0
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_paise(paise: int | None) -> Decimal:
    return (Decimal(int(paise or 0)) / 100).quantize(Decimal("0.01"))


def fmt_rupees(paise: int | None) -> str:
    """Indian-grouped rupee string without the symbol, e.g. 1,23,456.00."""
    amount = from_paise(paise)
    negative = amount < 0
    whole, _, frac = f"{abs(amount):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if negative else ''}{whole}.{frac}"


def gst_split(total_paise: int, gst_percentage: float) -> tuple[int, int]:
    """Split an inclusive amount into (base, gst). Zero percent means no GST."""
    if gst_percentage <= 0:
        return int(total_paise), 0
    rate = Decimal(str(gst_percentage)) / Decimal("100")
    base = (Decimal(total_paise) / (Decimal("1") + rate)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(base), int(total_paise) - int(base)


def gst_on_exclusive(base_paise: int, gst_percentage: float) -> tuple[int, int]:
    """Add GST on a catalog (exclusive) amount. Returns (gst, total)."""
    base = int(base_paise or 0)
    if gst_percentage <= 0:
        return 0, base
    rate = Decimal(str(gst_percentage)) / Decimal("100")
    gst = (Decimal(base) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(gst), base + int(gst)


def parse_date(value) -> date | None:
    """Parse the many date shapes the provider portals return."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # A day-month-year date embedded in a longer sentence, with either year width.
    match = re.search(r"(\d{1,2})[-/ ]([A-Za-z]{3,})[-/ ](\d{2}|\d{4})", text)
    if match:
        fmts = ("%d-%b-%Y", "%d-%B-%Y") if len(match.group(3)) == 4 else ("%d-%b-%y",)
        for fmt in fmts:
            try:
                return datetime.strptime(
                    f"{match.group(1)}-{match.group(2)[:3]}-{match.group(3)}", fmt
                ).date()
            except ValueError:
                continue
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    # A numeric day/month/year anywhere in the text, e.g. "Active since 09/09/26 12:10 PM".
    # Both providers are Indian, so the day always comes first. The lookarounds stop this
    # from chopping a longer number such as an ISO date apart; those are handled above.
    match = re.search(r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](\d{2}|\d{4})(?!\d)", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        if year < 100:
            year += 2000 if year <= 68 else 1900
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def parse_datetime(value) -> datetime | None:
    """Parse a timestamp, keeping the time of day.

    Railtel reports a session start as "09/09/26 12:10:08 PM". `parse_date` throws the
    clock away, which is fine for an expiry but loses the useful half of "active since".
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = re.sub(r"\s+", " ", str(value).strip())
    if not text:
        return None

    for fmt in _DATE_FORMATS:
        if "%H" not in fmt and "%I" not in fmt:
            continue
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    # The same shapes wrapped in a sentence, e.g. "Active since 09/09/26 12:10:08 PM".
    match = re.search(
        r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](\d{2}|\d{4})[ T]"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?",
        text,
    )
    if match:
        day, month, year = (int(match.group(i)) for i in (1, 2, 3))
        hour, minute = int(match.group(4)), int(match.group(5))
        second = int(match.group(6) or 0)
        meridiem = (match.group(7) or "").lower()
        if year < 100:
            year += 2000 if year <= 68 else 1900
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return None

    # No clock in the text, but a date on its own is still a valid answer at midnight.
    parsed = parse_date(text)
    return datetime(parsed.year, parsed.month, parsed.day) if parsed else None


def fmt_datetime(value) -> str:
    """A timestamp as "2026-09-09 12:10:08", the form we store, or ""."""
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S") if parsed else ""


def fmt_datetime_human(value) -> str:
    """A timestamp as "09 Sep 2026, 12:10 pm" for display, or ""."""
    parsed = parse_datetime(value)
    if not parsed:
        return ""
    return parsed.strftime("%d %b %Y, %I:%M %p").replace(" 0", " ", 1).replace("AM", "am").replace("PM", "pm")


def fmt_date(value: date | str | None) -> str:
    parsed = parse_date(value) if not isinstance(value, date) else value
    return parsed.strftime(DATE_FMT) if parsed else ""


def fmt_date_display(value: date | str | None) -> str:
    parsed = parse_date(value) if not isinstance(value, date) else value
    return parsed.strftime("%d %b %Y") if parsed else "—"


def today() -> date:
    return date.today()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_days(start: date, days: int) -> date:
    return start + timedelta(days=days)


def days_until(value: date | str | None) -> int | None:
    parsed = parse_date(value)
    if not parsed:
        return None
    return (parsed - today()).days
