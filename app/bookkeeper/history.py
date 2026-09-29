"""Category suggestions learned from one company's own posted history.

A vendor maps to an account only when that mapping repeats inside the
transactions just read for that realm. Nothing here is shared across companies.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class HistorySuggestion:
    account_id: str
    account_name: str
    count: int
    total: int
    confidence: str

    @property
    def reason(self) -> str:
        return (
            f"{self.count} previous transactions from this payee in this company "
            f"were classified as {self.account_name} ({self.count} of {self.total})."
        )


def _payee(txn: dict) -> str:
    return (
        (txn.get("EntityRef") or {}).get("name")
        or (txn.get("CustomerRef") or {}).get("name")
        or (txn.get("VendorRef") or {}).get("name")
        or ""
    ).strip()


def _line_account(txn: dict) -> tuple[str, str]:
    lines = txn.get("Line") or []
    if isinstance(lines, dict):
        lines = [lines]
    for line in lines:
        if not isinstance(line, dict):
            continue
        for field in ("AccountBasedExpenseLineDetail", "DepositLineDetail", "JournalEntryLineDetail"):
            detail = line.get(field)
            if isinstance(detail, dict):
                ref = detail.get("AccountRef") or {}
                if ref.get("value"):
                    return str(ref["value"]), ref.get("name") or ""
        ref = line.get("AccountRef") or {}
        if isinstance(ref, dict) and ref.get("value"):
            return str(ref["value"]), ref.get("name") or ""
    return "", ""


def build_payee_history(transactions: list[tuple[str, dict]], is_uncategorized) -> dict:
    """Count categorized accounts per payee. is_uncategorized(name) excludes placeholders."""
    history: dict[str, dict[str, dict]] = {}
    for _txn_type, txn in transactions:
        if not isinstance(txn, dict):
            continue
        payee = _payee(txn).lower()
        account_id, account_name = _line_account(txn)
        if not payee or not account_id:
            continue
        if is_uncategorized(account_name):
            continue
        bucket = history.setdefault(payee, {})
        cell = bucket.setdefault(account_id, {"name": account_name, "count": 0})
        cell["count"] += 1
        if account_name:
            cell["name"] = account_name
    return history


def suggest_from_history(
    history: dict,
    payee: str,
    *,
    min_count: int = 3,
    min_share: float = 0.75,
) -> HistorySuggestion | None:
    """Propose the dominant account when the pattern is repeated and consistent."""
    bucket = history.get((payee or "").strip().lower())
    if not bucket:
        return None
    total = sum(int(cell["count"]) for cell in bucket.values())
    if total <= 0:
        return None
    account_id, best = max(bucket.items(), key=lambda item: item[1]["count"])
    count = int(best["count"])
    share = count / total
    if count < min_count or share < min_share:
        return None
    confidence = "high" if count >= 8 and share >= 0.9 else "medium"
    return HistorySuggestion(
        account_id=account_id,
        account_name=best.get("name") or account_id,
        count=count,
        total=total,
        confidence=confidence,
    )
