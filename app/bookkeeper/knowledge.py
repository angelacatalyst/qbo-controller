"""What a business type asks the bookkeeper to look at.

These are review focuses, not account mappings. Restaurant A and a retailer
can both buy from the same vendor and post it to different accounts.
That decision lives on the company's own rules and history.
"""

BUSINESS_TYPES = (
    "restaurant",
    "professional_services",
    "consulting",
    "agency",
    "nonprofit",
    "construction",
    "retail",
    "other",
)

_FOCUS = {
    "restaurant": (
        "POS deposits and card batches",
        "tips",
        "sales tax collected",
        "food and beverage cost",
        "payroll",
    ),
    "professional_services": (
        "open invoices and unapplied payments",
        "retainers",
        "contractor payments",
        "accounts receivable aging",
    ),
    "consulting": (
        "open invoices and unapplied payments",
        "project retainers",
        "contractor payments",
    ),
    "agency": (
        "client invoices",
        "media and vendor bills",
        "unapplied payments",
    ),
    "nonprofit": (
        "restricted versus unrestricted cash",
        "grants and programs",
        "classes and projects",
        "donor restrictions",
    ),
    "construction": (
        "job costs",
        "subcontractor bills",
        "retainage",
        "customer draws",
    ),
    "retail": (
        "POS and merchant deposits",
        "inventory and cost of goods",
        "sales tax",
        "card batches",
    ),
    "other": (
        "uncategorized transactions",
        "bank and credit card balances",
        "open invoices and bills",
        "financial statement anomalies",
    ),
}

_INDUSTRY_ALIASES = {
    "restaurant": "restaurant",
    "food": "restaurant",
    "hospitality": "restaurant",
    "professional services": "professional_services",
    "professional_services": "professional_services",
    "legal": "professional_services",
    "consulting": "consulting",
    "consultant": "consulting",
    "agency": "agency",
    "marketing": "agency",
    "nonprofit": "nonprofit",
    "non-profit": "nonprofit",
    "not for profit": "nonprofit",
    "construction": "construction",
    "contractor": "construction",
    "retail": "retail",
    "store": "retail",
}


def normalize_business_type(industry: str | None) -> str:
    text = (industry or "").strip().lower()
    if not text:
        return "other"
    if text in _INDUSTRY_ALIASES:
        return _INDUSTRY_ALIASES[text]
    for key, value in _INDUSTRY_ALIASES.items():
        if key in text:
            return value
    return "other"


def focus_for(industry: str | None) -> tuple[str, tuple[str, ...]]:
    kind = normalize_business_type(industry)
    return kind, _FOCUS[kind]
