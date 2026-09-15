"""Plan pricing rules.

Railtel/Railwire plan catalogs list a *monthly* amount and 30-day validity even for
term plans. A trailing ` xN` in the plan name means "pay N months up front", and the
term carries promotional free days on top:

    x3  -> pay 3 months,  100 days validity (90 + 10 free)
    x6  -> pay 6 months,  210 days validity (180 + 30 free)
    x10 -> pay 10 months, 360 days validity (300 + 60 free)

Hathway TV bouquets are plain 30-day monthly packs.

Mirrors cableway_automation/data_sync/railtel_plan_pricing.py; kept here so the
platform runs standalone.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .money import to_paise

DAYS_PER_MONTH = 30
MONTHLY_VALIDITY_DAYS = 30
TERM_BONUS_DAYS = {3: 10, 6: 30, 10: 60, 12: 0}

_TERM_SUFFIX = re.compile(r"\sx(\d+)\s*$", re.IGNORECASE)


def parse_term_months(plan_name: str) -> int | None:
    """Return N from a trailing ' xN' in the plan name, else None."""
    match = _TERM_SUFFIX.search(plan_name or "")
    if not match:
        return None
    try:
        months = int(match.group(1))
    except ValueError:
        return None
    return months if months > 1 else None


def term_validity_days(pay_months: int) -> int:
    return pay_months * DAYS_PER_MONTH + TERM_BONUS_DAYS.get(pay_months, 0)


def validity_from_name(plan_name: str) -> int:
    """Validity implied by the plan name alone, for catalogs that already hold term totals."""
    months = parse_term_months(plan_name)
    return term_validity_days(months) if months else MONTHLY_VALIDITY_DAYS


def billing_cycle_for(pay_months: int | None, plan_name: str = "") -> str:
    name = (plan_name or "").lower()
    if pay_months is None:
        if "yearly" in name or "x12" in name:
            return "yearly"
        return "monthly"
    if pay_months >= 10:
        return "yearly"
    if pay_months >= 6:
        return "half_yearly"
    if pay_months >= 3:
        return "quarterly"
    return "monthly"


def price_plan(plan_name: str, monthly_amount) -> dict:
    """Resolve a catalog row into what the customer actually pays for one term.

    Returns price_paise (full term amount), validity_days, billing_cycle,
    term_months and a human description.
    """
    monthly_paise = to_paise(monthly_amount)
    pay_months = parse_term_months(plan_name)

    if pay_months is None or monthly_paise <= 0:
        return {
            "price_paise": monthly_paise,
            "validity_days": MONTHLY_VALIDITY_DAYS,
            "billing_cycle": billing_cycle_for(None, plan_name),
            "term_months": 1,
            "description": "",
        }

    total_paise = monthly_paise * pay_months
    validity = term_validity_days(pay_months)
    free_days = TERM_BONUS_DAYS.get(pay_months, 0)
    monthly_rupees = (Decimal(monthly_paise) / 100).quantize(Decimal("0.01"))
    total_rupees = (Decimal(total_paise) / 100).quantize(Decimal("0.01"))
    description = (
        f"Pay {pay_months} months @ Rs {monthly_rupees}/mo = Rs {total_rupees}; "
        f"{validity} days validity"
    )
    if free_days:
        description += f" ({free_days} free days included)"

    return {
        "price_paise": total_paise,
        "validity_days": validity,
        "billing_cycle": billing_cycle_for(pay_months, plan_name),
        "term_months": pay_months,
        "description": description,
    }
