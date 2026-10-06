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
DATE_DISPLAY_FMT = "%d-%m-%y"
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


def round_up_rupee(paise: int | None) -> int:
    """Paise rounded up to the next whole rupee: 58882 -> 58900 (₹588.82 -> ₹589)."""
    value = int(paise or 0)
    return -((-value) // 100) * 100


def whole_rupees(paise: int | None) -> int:
    """Rupees as a whole number, rounded up the same way bills are."""
    return round_up_rupee(paise) // 100


def fmt_rupees(paise: int | None) -> str:
    """Indian-grouped whole-rupee string without the symbol, e.g. 1,23,456."""
    amount = whole_rupees(paise)
    negative = amount < 0
    whole = str(abs(amount))
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if negative else ''}{whole}"


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


_ONES = (
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
    "Eighteen", "Nineteen",
)
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _words_below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def rupees_in_words(paise) -> str:
    """Whole rupees in Indian-system words: "Rupees Twelve Thousand Five Hundred Only"."""
    n = whole_rupees(paise)
    if n <= 0:
        return "Rupees Zero Only"
    parts = []
    for size, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if n >= size:
            parts.append(f"{_words_below_1000(n // size) if n // size < 1000 else rupees_in_words((n // size) * 100)[7:-5]} {label}")
            n %= size
    if n:
        parts.append(_words_below_1000(n))
    return f"Rupees {' '.join(parts)} Only"


def fmt_receipt_datetime(value) -> str:
    """A timestamp as "04 Oct 2026, 2:08 PM" for printed documents."""
    parsed = parse_datetime(value)
    if not parsed:
        return str(value or "")
    return parsed.strftime("%d %b %Y, ") + parsed.strftime("%I:%M %p").lstrip("0")


def fmt_datetime_human(value) -> str:
    """A timestamp as "29 Sep, 12:03" (year added when not this year)."""
    parsed = parse_datetime(value)
    if not parsed:
        return ""
    clock = parsed.strftime("%H:%M")
    if parsed.year == date.today().year:
        return f"{parsed.day} {parsed.strftime('%b')}, {clock}"
    return f"{parsed.day} {parsed.strftime('%b %Y')}, {clock}"


def fmt_relative(value) -> str:
    """Relative time such as 'just now', '12 min ago', '3 days ago'."""
    parsed = parse_datetime(value)
    if not parsed:
        return ""
    secs = int((datetime.now() - parsed).total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 45:
        return "in a moment" if future else "just now"
    if secs < 3600:
        n = max(1, secs // 60)
        unit = "min"
        text = f"{n} {unit}"
    elif secs < 86400:
        n = secs // 3600
        unit = "hour" if n == 1 else "hours"
        text = f"{n} {unit}"
    else:
        n = secs // 86400
        if n >= 30:
            return fmt_datetime_human(parsed)
        unit = "day" if n == 1 else "days"
        text = f"{n} {unit}"
    return f"in {text}" if future else f"{text} ago"


def fmt_date(value: date | str | None) -> str:
    parsed = parse_date(value) if not isinstance(value, date) else value
    return parsed.strftime(DATE_FMT) if parsed else ""


def fmt_date_display(value: date | str | None) -> str:
    """User-facing calendar date: 9 Apr 2025."""
    parsed = parse_date(value) if not isinstance(value, date) else value
    if not parsed:
        return "—"
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def fmt_date_input(value: date | str | None) -> str:
    """Value for expiry/date text fields (empty when unknown)."""
    parsed = parse_date(value) if not isinstance(value, date) else value
    return parsed.strftime(DATE_DISPLAY_FMT) if parsed else ""


def normalise_expiry_input(value: str) -> tuple[str, str | None]:
    """Parse a form expiry field; store ISO in DB. Returns (stored, error)."""
    raw = (value or "").strip()
    if not raw:
        return "", None
    parsed = parse_date(raw)
    if not parsed:
        return raw, f"Expiry must be DD-MM-YY (you entered {raw!r})."
    return fmt_date(parsed), None


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
