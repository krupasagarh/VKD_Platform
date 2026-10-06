"""Railtel dealer account ids (multi-login) for filters and UI tags."""
from __future__ import annotations

RAILTEL_DEALERS: tuple[tuple[str, str], ...] = (
    ("default", "VK Digital"),
    ("kalpataru", "Kalpataru"),
    ("speedfirst", "Speedfirst"),
)

RAILTEL_DEALER_LABELS: dict[str, str] = dict(RAILTEL_DEALERS)

RAILTEL_DEALER_SHORT: dict[str, str] = {
    "default": "VK",
    "kalpataru": "KFN",
    "speedfirst": "SFB",
}


def normalize_railtel_account_id(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return ""
    if value in ("default", "vk_digital", "vk digital"):
        return "default"
    return value


def railtel_account_filter_sql(
    portal_account_col: str = "cn.portal_account_id",
) -> tuple[str, list]:
    """Return (SQL fragment, params) for filtering connections by dealer account."""
    return f"COALESCE({portal_account_col}, '') IN ('', 'default')", []


def railtel_account_filter_sql_for(account_id: str, *, portal_account_col: str = "cn.portal_account_id") -> tuple[str, list]:
    acc = normalize_railtel_account_id(account_id)
    if acc == "default":
        return railtel_account_filter_sql(portal_account_col)
    return f"lower({portal_account_col}) = ?", [acc]
