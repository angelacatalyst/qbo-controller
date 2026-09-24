"""
Bookkeeping Engine
──────────────────
Three core engines, all strictly scoped to one realm_id:

  1. CATEGORIZATION  — rules-based AI categorizer for uncategorized / Ask My
                       Accountant transactions pulled from QBO.
  2. AR MATCHING     — auto-match unapplied customer payments to open invoices.
  3. BANK REC        — reconciliation worksheet: match QBO transactions to bank
                       statement imports and flag discrepancies.

Every proposed change enters the ProposedCategorization / ProposedARMatch table
and requires approval before anything is written back to QBO.
"""
from __future__ import annotations

import re
from datetime import datetime, date, timedelta
from typing import Optional
from sqlalchemy.orm import Session

import logging

from app.database import (
    Company, CompanyProfile,
    ProposedCategorization, ProposedARMatch,
    ProposedJournalEntry, ChangeLog, WorkItem, CompanyRule, _now,
)

logger = logging.getLogger(__name__)
from app.qbo_client import QBOClient, classify_qbo_error


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — TRANSACTION CATEGORIZATION ENGINE
# ═══════════════════════════════════════════════════════════════

# ── Category Rule Library ────────────────────────────────────
# Each rule: (pattern_in_payee_or_memo, account_keywords, confidence)
# Pattern is matched case-insensitively against payee name + memo.
# account_keywords is matched against COA account names.

_CATEGORIZATION_RULES: list[tuple[str, str, str]] = [
    # Banking / Financial
    (r"bank fee|service charge|monthly fee|wire fee|overdraft",      "bank charges|bank fees|service charge",    "high"),
    (r"interest (charge|expense|payment)",                            "interest expense",                         "high"),
    # Payroll
    (r"gusto|adp|paychex|rippling|bamboohr|payroll|paylocity",       "payroll expense|wages|salaries",           "high"),
    # Insurance
    (r"insurance|allstate|state farm|progressive|geico|travelers|hiscox|next insurance", "insurance expense",    "high"),
    # Utilities
    (r"electric|gas|water|utility|pg&e|con ed|xcel|centerpoint|atmos", "utilities|electric|gas",                "high"),
    (r"internet|comcast|att|verizon|spectrum|at&t|tmobile|t-mobile",  "internet|telephone|utilities|communication", "high"),
    # Office / Supplies
    (r"amazon|staples|office depot|officemax|costco|sams club|sam's", "office supplies|supplies",                "medium"),
    (r"usps|fedex|ups|dhl|stamps\.com",                               "postage|shipping",                        "high"),
    # Software / Subscriptions
    (r"quickbooks|intuit",                                            "accounting|software subscription",         "high"),
    (r"google|microsoft|adobe|dropbox|zoom|slack|hubspot|salesforce|notion|asana|monday\.com",
                                                                      "software|subscription|cloud services|saas", "high"),
    (r"netflix|spotify|hulu|apple\.com/bill",                         "entertainment|subscriptions",              "medium"),
    # Advertising
    (r"google ads|facebook|meta |instagram|linkedin|twitter|tiktok|yelp|bing ads",
                                                                      "advertising|marketing",                    "high"),
    # Travel / Auto
    (r"uber|lyft|airbnb|hotel|marriott|hilton|hyatt|delta|american airlines|southwest|jetblue|hertz|enterprise|avis",
                                                                      "travel|airfare|hotel|auto",                "medium"),
    (r"chevron|shell|exxon|bp |marathon|speedway|quiktrip|wawa|gasoline|fuel",
                                                                      "auto|fuel|gas",                            "high"),
    # Meals
    (r"doordash|grubhub|uber eats|postmates|seamless",                "meals|food delivery",                     "medium"),
    # Professional services
    (r"attorney|lawyer|law firm|legal",                               "legal|professional services",              "high"),
    (r"cpa|accountant|bookkeeper|accounting firm",                    "accounting|professional services",         "high"),
    (r"consultant|consulting",                                        "consulting|professional services",         "medium"),
    # Rent
    (r"rent|lease|landlord|property management",                      "rent|lease expense",                       "high"),
    # Cleaning / Maintenance
    (r"cleaning|janitorial|maintenance|repair",                       "repairs|maintenance|cleaning",             "medium"),
    # Restaurant-specific COGS
    (r"sysco|us foods|performance food|restaurant depot|gordon food|gfs|cheney brothers|chef'?s warehouse",
                                                                      "food cost|cost of goods|food purchases",   "high"),
    (r"beverage|liquor|wine|beer|spirits|southern wine|breakthru|glazer",
                                                                      "beverage cost|liquor|cost of goods",       "high"),
    # Taxes & Licenses
    (r"irs|internal revenue|state tax|city tax|county tax|sales tax payment|tax payment",
                                                                      "income tax|sales tax payable|taxes",       "high"),
    (r"license|permit|registration|secretary of state",               "licenses|permits",                        "high"),
]


def _match_rule(payee: str, memo: str) -> tuple[str | None, str, str]:
    """Return (account_keyword, confidence, rule_text) for the first matching rule, or (None, '', '')."""
    combined = f"{payee} {memo}".lower()
    for pat, acct_kw, conf in _CATEGORIZATION_RULES:
        if re.search(pat, combined, re.IGNORECASE):
            return acct_kw, conf, pat
    return None, "", ""


def _find_coa_account(coa: list, keywords: str) -> tuple[str | None, str | None, str | None]:
    """Match COA account by keyword, return (id, name, type) or (None, None, None)."""
    kws = [k.strip().lower() for k in keywords.split("|")]
    for kw in kws:
        for a in coa:
            if kw in a.get("name", "").lower():
                return a.get("id"), a.get("name"), a.get("type")
    return None, None, None


def _is_uncategorized(account_name: str) -> bool:
    """Return True if this account is a placeholder that needs categorization."""
    n = (account_name or "").lower()
    return any(kw in n for kw in [
        "uncategorized", "ask my accountant", "miscellaneous",
        "other expense", "other income",
    ])


def run_categorization(
    db: Session,
    company: Company,
    profile: CompanyProfile,
    lookback_days: int = 90,
) -> list[ProposedCategorization]:
    """
    Fetch uncategorized / Ask My Accountant transactions from QBO for the
    past `lookback_days`, apply rules, and create ProposedCategorization
    records for review.

    Returns the list of newly created proposals (does NOT commit — caller
    must commit after reviewing or the route handler will commit).
    """
    if not profile or not profile.data_as_of:
        return []

    coa = profile.chart_of_accounts or []
    conventions: dict = profile.accounting_conventions or {}

    client = QBOClient(company)
    start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # IDs already proposed (avoid duplicates)
    existing_txn_ids = {
        r.qbo_txn_id
        for r in db.query(ProposedCategorization.qbo_txn_id)
                   .filter_by(realm_id=company.realm_id)
                   .filter(ProposedCategorization.status.in_(["pending", "approved"]))
                   .all()
    }

    proposals: list[ProposedCategorization] = []
    base_count = db.query(ProposedCategorization).filter_by(realm_id=company.realm_id).count()

    def _new_proposal(**kwargs) -> ProposedCategorization:
        p = ProposedCategorization(
            company_id=company.id,
            realm_id=company.realm_id,
            **kwargs,
        )
        db.add(p)
        proposals.append(p)
        return p

    # ── Pull Purchases / Expenses ──────────────────────────────
    try:
        purchases = client.get_expenses(db, start_date=start_date)
    except Exception:
        purchases = []

    for txn in purchases:
        txn_id = txn.get("Id", "")
        if txn_id in existing_txn_ids:
            continue

        lines = txn.get("Line", [])
        for line in lines:
            line_acct = (line.get("AccountBasedExpenseLineDetail", {})
                            .get("AccountRef", {})
                            .get("name", ""))
            if not _is_uncategorized(line_acct):
                continue

            payee = (txn.get("EntityRef", {}).get("name", "")
                     or txn.get("PaymentType", ""))
            memo = txn.get("PrivateNote", "") or line.get("Description", "")
            amount = float(line.get("Amount", 0))
            txn_date_str = txn.get("TxnDate", "")

            acct_kw, conf, rule = _match_rule(payee, memo)

            # Check company-specific conventions first
            payee_lower = payee.lower()
            for conv_key, conv_acct in conventions.items():
                if conv_key.lower() in payee_lower:
                    acct_kw = conv_acct.lower()
                    conf = "high"
                    rule = f"company_convention:{conv_key}"
                    break

            if acct_kw:
                acct_id, acct_name, acct_type = _find_coa_account(coa, acct_kw)
            else:
                acct_id, acct_name, acct_type = None, None, None
                conf = "low"
                rule = "no_match"

            _new_proposal(
                qbo_txn_id=txn_id,
                qbo_txn_type="Purchase",
                txn_date=datetime.strptime(txn_date_str, "%Y-%m-%d") if txn_date_str else None,
                amount=amount,
                payee_name=payee,
                memo=memo[:500] if memo else "",
                current_account_id=line.get("AccountBasedExpenseLineDetail", {})
                                       .get("AccountRef", {}).get("value"),
                current_account_name=line_acct,
                suggested_account_id=acct_id,
                suggested_account_name=acct_name,
                suggested_account_type=acct_type,
                confidence=conf,
                reason=_build_reason(acct_kw, rule, payee, memo),
                rule_matched=rule,
            )
        existing_txn_ids.add(txn_id)

    # ── Pull Deposits with uncategorized lines ─────────────────
    try:
        deposits = client.get_deposits(db, start_date=start_date)
    except Exception:
        deposits = []

    for txn in deposits:
        txn_id = txn.get("Id", "")
        if txn_id in existing_txn_ids:
            continue

        for line in txn.get("Line", []):
            line_acct = (line.get("DepositLineDetail", {})
                            .get("AccountRef", {})
                            .get("name", ""))
            if not _is_uncategorized(line_acct):
                continue

            memo = line.get("Description", "")
            amount = float(line.get("Amount", 0))

            acct_kw, conf, rule = _match_rule("", memo)
            if acct_kw:
                acct_id, acct_name, acct_type = _find_coa_account(coa, acct_kw)
            else:
                acct_id = acct_name = acct_type = None
                conf = "low"
                rule = "no_match"

            _new_proposal(
                qbo_txn_id=txn_id,
                qbo_txn_type="Deposit",
                txn_date=datetime.strptime(txn.get("TxnDate", ""), "%Y-%m-%d")
                         if txn.get("TxnDate") else None,
                amount=amount,
                payee_name="",
                memo=memo[:500],
                current_account_name=line_acct,
                suggested_account_id=acct_id,
                suggested_account_name=acct_name,
                suggested_account_type=acct_type,
                confidence=conf,
                reason=_build_reason(acct_kw, rule, "", memo),
                rule_matched=rule,
            )
        existing_txn_ids.add(txn_id)

    return proposals


def _build_reason(acct_kw: str | None, rule: str, payee: str, memo: str) -> str:
    if rule == "no_match":
        return (
            f"No automatic category match found for payee '{payee}' / memo '{memo}'. "
            "Please select the correct expense account manually."
        )
    if rule.startswith("company_convention:"):
        convention = rule.split(":", 1)[1]
        return f"Company convention: '{convention}' → mapped to '{acct_kw}'."
    return (
        f"Payee/memo matched rule pattern '{rule}'. "
        f"Suggested account: '{acct_kw}'. "
        "Verify this matches the actual nature of the expense."
    )


# ═══════════════════════════════════════════════════════════════
# SECTION 1B — WORKITEM CATEGORIZATION ENGINE v2
# ═══════════════════════════════════════════════════════════════
#
# Design invariants (enforced here, never bypassed):
#   1. QBO writes = 0 until a WorkItem is explicitly approved by a human
#      and execute_work_item() is called on that approved item.
#   2. run_categorization_v2() is READ/ANALYZE ONLY.
#   3. Company isolation: every rule/WorkItem query is scoped to realm_id.
#   4. materiality_limit is a FLOOR signal (amount > limit → level ≥ 2),
#      NOT an authorization grant.  Autonomy is determined by four factors.
# ═══════════════════════════════════════════════════════════════

# ── Uncategorized detection keywords ────────────────────────────────────────

_UNCATEGORIZED_KEYWORDS_EN = frozenset([
    "uncategorized",
    "uncategorized expense",
    "uncategorized income",
    "uncategorized asset",
    "ask my accountant",
    "suspense",
    "undeposited funds",
    "clearing",
    "opening balance equity",
    "owner's equity",
])

_UNCATEGORIZED_KEYWORDS_ES = frozenset([
    "sin categorizar",
    "sin clasificar",
    "pregúntale a mi contador",
    "preguntale a mi contador",
    "preguntale al contador",
    "gastos sin categorizar",
    "ingresos sin categorizar",
    "cuentas por aclarar",
    "suspensión",
    "fondos sin depositar",
    "balance inicial",
])

# QBO canonical AccountSubType values that mean "needs categorization"
_UNCATEGORIZED_SUBTYPES = frozenset([
    "UncategorizedExpense",
    "UncategorizedIncome",
    "UncategorizedAsset",
    "AskMyAccountant",
    "OpeningBalanceEquity",
    "UndepositedFunds",
])


# QBO account names that are too generic to be meaningful even though
# they are not technically "uncategorized".  Transactions posted here
# should be reviewed and moved to a more specific account.
_GENERIC_CATEGORY_KEYWORDS = frozenset([
    "miscellaneous",
    "general expense",
    "general income",
    "other expense",
    "other income",
])


def _is_generic_category(account_name: str) -> bool:
    """Return True if the account exists but is a generic catch-all.

    Distinct from _is_uncategorized_v2: the account is a real QBO account,
    but too vague to be useful for financial reporting.  We do NOT re-check
    uncategorized keywords here — callers should call _is_uncategorized_v2
    first and only call this when that returns False.
    """
    name_lower = (account_name or "").strip().lower()
    return any(kw in name_lower for kw in _GENERIC_CATEGORY_KEYWORDS)


def _is_uncategorized_v2(account_name: str, account_subtype: str = "") -> bool:
    """Return True if this account/subtype indicates an uncategorized transaction.

    Handles both English and Spanish QBO environments.
    """
    name_lower = (account_name or "").strip().lower()
    subtype = (account_subtype or "").strip()

    if subtype in _UNCATEGORIZED_SUBTYPES:
        return True

    for kw in _UNCATEGORIZED_KEYWORDS_EN:
        if kw in name_lower:
            return True

    for kw in _UNCATEGORIZED_KEYWORDS_ES:
        if kw in name_lower:
            return True

    return False


# ── Risk assessment ──────────────────────────────────────────────────────────

def _assess_risk(amount: float, materiality_limit: float) -> str:
    """Assess transaction risk relative to the company's materiality limit.

    Args:
        amount: Absolute transaction amount.
        materiality_limit: Company's risk/control boundary (NOT an auth ceiling).

    Returns:
        'high' | 'medium' | 'low'
    """
    abs_amount = abs(amount or 0.0)
    if abs_amount > materiality_limit * 2:
        return "high"
    if abs_amount > materiality_limit:
        return "medium"
    return "low"


# ── Autonomy classification (four-factor, no shortcuts) ─────────────────────

def classify_autonomy(
    confidence: str,
    risk: str,
    rule_type: str,
    has_proposed_account: bool,
    amount: float,
    materiality_limit: float,
) -> int:
    """Classify the required authorization level for a WorkItem.

    Four factors must ALL pass — no single factor grants authorization:
        1. Confidence  (high / medium / low / none)
        2. Risk        (low / medium / high)
        3. Rule type   (CATEGORIZE / FLAG / SKIP / CLIENT_RULE)
        4. Proposed account available

    Returns:
        0 = AUTO          (reserved; not granted in Phase 1)
        1 = BATCH         (approve as a group)
        2 = INDIVIDUAL    (approve one-by-one)
        3 = HUMAN_REQUIRED (must be manually handled)

    Materiality floor:
        amount > materiality_limit → level raised to at least 2 (INDIVIDUAL).
        This is a FLOOR only; it can never LOWER the autonomy level.
    """
    # Factor 4: no proposed account → always HUMAN_REQUIRED
    if not has_proposed_account:
        return 3

    # Factor 3: rule type
    if rule_type == "FLAG":
        return 3   # flagged transactions always need human review
    if rule_type == "SKIP":
        return 3   # skipped items should not reach here, but guard anyway

    # Factor 1: confidence
    if not confidence or confidence in ("none", ""):
        return 3

    # Confidence × Risk matrix
    if confidence == "high":
        if risk == "low":
            base_level = 1   # BATCH
        elif risk == "medium":
            base_level = 2   # INDIVIDUAL
        else:
            base_level = 3   # high risk → HUMAN_REQUIRED
    elif confidence == "medium":
        if risk == "low":
            base_level = 2   # INDIVIDUAL
        else:
            base_level = 3
    else:
        # low confidence → always HUMAN_REQUIRED
        base_level = 3

    # Phase 1 safety: no AUTO grants
    if base_level == 0:
        base_level = 1

    # Materiality floor (raise if exceeded; NEVER lower)
    abs_amount = abs(amount or 0.0)
    if abs_amount > materiality_limit and base_level < 2:
        base_level = 2

    return base_level


# ── Company rule helpers ─────────────────────────────────────────────────────

def _load_company_rules(db: Session, realm_id: str) -> list:
    """Load active, approved company rules for this realm only.

    NEVER returns rules from other companies — realm_id is enforced.
    Ordered by priority DESC so higher-priority rules are evaluated first.
    """
    try:
        from app.database import CompanyRule as _CompanyRule  # local import avoids circular
        rules = (
            db.query(_CompanyRule)
            .filter(
                _CompanyRule.realm_id == realm_id,
                _CompanyRule.status == "active",
                _CompanyRule.approved_by.isnot(None),
            )
            .order_by(_CompanyRule.priority.desc())
            .all()
        )
        return rules
    except Exception as exc:
        logger.warning("_load_company_rules(%s): %s", realm_id, exc)
        return []


def _apply_company_rules(rules: list, payee: str, memo: str, amount: float):
    """Return the first matching CompanyRule for this transaction, or None.

    Matching is per-rule condition_type:
        PAYEE_OR_MEMO  — regex search in payee OR memo
        PAYEE_ONLY     — regex search in payee only
        MEMO_ONLY      — regex search in memo only
        AMOUNT_RANGE   — pattern is 'min:max' in absolute amount
    """
    import re as _re

    payee_str = (payee or "").strip()
    memo_str = (memo or "").strip()
    abs_amount = abs(amount or 0.0)

    for rule in rules:
        pattern = rule.pattern or ""
        flags_str = (rule.pattern_flags or "case_insensitive").lower()
        re_flags = _re.IGNORECASE if "case_insensitive" in flags_str else 0
        ctype = (rule.condition_type or "PAYEE_OR_MEMO").upper()

        try:
            if ctype == "AMOUNT_RANGE":
                # pattern format: "min:max"  (use * for open-ended, e.g. "0:500")
                parts = pattern.split(":")
                lo = float(parts[0]) if parts[0] not in ("*", "") else 0.0
                hi = float(parts[1]) if len(parts) > 1 and parts[1] not in ("*", "") else float("inf")
                if lo <= abs_amount <= hi:
                    return rule
            elif ctype == "PAYEE_ONLY":
                if payee_str and _re.search(pattern, payee_str, re_flags):
                    return rule
            elif ctype == "MEMO_ONLY":
                if memo_str and _re.search(pattern, memo_str, re_flags):
                    return rule
            else:  # PAYEE_OR_MEMO (default)
                combined = f"{payee_str} {memo_str}".strip()
                if combined and _re.search(pattern, combined, re_flags):
                    return rule
        except Exception as exc:
            logger.warning("Rule %s pattern error: %s", rule.id, exc)
            continue

    return None


# ── run_categorization_v2 ────────────────────────────────────────────────────

def run_categorization_v2(
    db: Session,
    company,          # Company ORM instance
    profile,          # CompanyProfile ORM instance
    lookback_days: int = 90,
) -> tuple:
    """READ/ANALYZE ONLY categorization engine.

    Queries QBO for Purchase, Check, Deposit, and SalesReceipt transactions.
    Creates WorkItem records in the Controller DB — NO QBO writes.

    Returns:
        (list[WorkItem], diagnostic_dict)

    diagnostic_dict keys:
        txn_counts          {type: fetched_count}
        uncategorized_count int
        work_items_created  int
        autonomy_breakdown  {AUTO:n, BATCH:n, INDIVIDUAL:n, HUMAN_REQUIRED:n}
        api_errors          list of {type, error}
        skipped_items       list of {txn_id, type, reason}
        qbo_writes          int  (always 0 — invariant)
    """
    diagnostic = {
        # ── Fetch counts (one per entity type) ──────────────────────────
        "txn_counts": {},

        # ── Transaction review summary ───────────────────────────────────
        "posted_transactions_reviewed": 0,   # total posted txns inspected
        "items_uncategorized": 0,            # no account / placeholder account
        "items_needing_review": 0,           # generic acct, flag, dup, transfer
        "items_ok": 0,                       # well-categorized, no action needed

        # ── WorkItem creation ────────────────────────────────────────────
        "work_items_created": 0,
        "autonomy_breakdown": {"AUTO": 0, "BATCH": 0, "INDIVIDUAL": 0, "HUMAN_REQUIRED": 0},
        "skipped_items": [],

        # ── Error classification (separated) ────────────────────────────
        "entities_not_supported": [],   # [{entity, reason}] — Intuit confirmed
        "api_errors": [],               # real errors requiring attention

        # ── Bank Feed limitation (always documented) ─────────────────────
        "bank_feed_limitation": True,
        "bank_feed_note": (
            "QBO Bank Feed / For Review transactions are not accessible via the "
            "QBO Accounting API v3. The AI Bookkeeper analyzes transactions after "
            "they are posted to the books."
        ),

        # ── Safety invariant ────────────────────────────────────────────
        "qbo_writes": 0,   # INVARIANT: always 0
    }

    if not profile or not profile.data_as_of:
        diagnostic["api_errors"].append({
            "type": "CONFIG",
            "error": "No company profile / data_as_of — connect QBO first.",
        })
        return [], diagnostic

    materiality_limit = getattr(profile, "materiality_limit", None) or 2500.0
    realm_id = company.realm_id
    client = QBOClient(company)
    start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    def _now_iso() -> str:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Module results structure (Phase 2) ──────────────────────────────────
    # BANKING module is the only one active in this run; others are stubs.
    # Overall engine_status is derived from module statuses after all modules run.
    module_results: dict = {
        "BANKING": {
            "status": "NOT_STARTED",
            "message": "",
            "items_scanned": 0,
            "issues_found": 0,
            "work_items_created": 0,
            "errors": [],
            "warnings": [],
            "data_freshness": "live_qbo",
            "started_at": None,
            "completed_at": None,
        },
        "CREDIT_CARDS": {
            "status": "NOT_IMPLEMENTED",
            "message": "Credit card module not yet implemented.",
            "data_freshness": "n/a",
        },
        "SALES_AR": {
            "status": "NOT_IMPLEMENTED",
            "message": "AR matching runs separately via the AR Matching engine.",
            "data_freshness": "n/a",
        },
        "EXPENSES_AP": {
            "status": "NOT_IMPLEMENTED",
            "message": "AP / accounts payable module not yet implemented.",
            "data_freshness": "n/a",
        },
        "PAYROLL": {
            "status": "NOT_IMPLEMENTED",
            "message": "Payroll module not yet implemented.",
            "data_freshness": "n/a",
        },
        "TAXES": {
            "status": "NOT_IMPLEMENTED",
            "message": "Tax module not yet implemented.",
            "data_freshness": "n/a",
        },
        "ACCOUNTING": {
            "status": "NOT_IMPLEMENTED",
            "message": "Accounting assessment runs separately via the Accounting module.",
            "data_freshness": "cached_profile",
        },
        "ASSESSMENT": {
            "status": "NOT_IMPLEMENTED",
            "message": "Assessment runs separately via the Accounting module.",
            "data_freshness": "cached_profile",
        },
        "CONTROL": {
            "status": "NOT_IMPLEMENTED",
            "message": "Control / compliance module not yet implemented.",
            "data_freshness": "n/a",
        },
    }
    diagnostic["module_results"] = module_results

    # ── 1. Load company rules (realm-scoped) ────────────────────────────────
    company_rules = _load_company_rules(db, realm_id)
    logger.info("[v2:%s] Loaded %d company rules", realm_id, len(company_rules))

    # ── 2. Fetch transactions — paginated, with Check fallback (Phase 1+3) ──
    # BANKING module starts now
    module_results["BANKING"]["status"] = "RUNNING"
    module_results["BANKING"]["started_at"] = _now_iso()

    raw_txns: list[tuple[str, dict]] = []
    # seen_raw_ids: deduplicates across entity types.
    # Critical when get_checks_with_fallback() falls back to Purchase — those
    # records are identical to ones already fetched by the Purchase query.
    seen_raw_ids: set[str] = set()

    _banking_warnings: list[str] = []  # warnings for module_results["BANKING"]
    _banking_errors: list[str] = []    # errors for module_results["BANKING"]

    def _collect(txn_type: str, results: list, retrieval_info: dict | None = None) -> int:
        """Add results to raw_txns, deduplicating by txn_id. Returns count added."""
        added = 0
        for txn in results:
            tid = str(txn.get("Id", ""))
            if tid and tid in seen_raw_ids:
                continue
            seen_raw_ids.add(tid)
            raw_txns.append((txn_type, txn))
            added += 1
        diagnostic["txn_counts"][txn_type] = added
        if retrieval_info and not retrieval_info.get("is_complete", True):
            _banking_warnings.append(
                f"{txn_type}: pagination — only {retrieval_info.get('total_fetched', added)} records "
                f"retrieved (QBO has more). Consider narrowing lookback_days."
            )
        return added

    # Simple entity types — direct _query_all (paginated)
    _simple_entities = [
        ("Purchase",    f"SELECT * FROM Purchase WHERE TxnDate >= '{start_date}'"),
        ("Deposit",     f"SELECT * FROM Deposit WHERE TxnDate >= '{start_date}'"),
        ("SalesReceipt",f"SELECT * FROM SalesReceipt WHERE TxnDate >= '{start_date}'"),
    ]
    for txn_type, sql_base in _simple_entities:
        try:
            results, is_complete, total = client._query_all(db, sql_base)
            _collect(txn_type, results or [], {"is_complete": is_complete, "total_fetched": total})
            logger.info("[v2:%s] %s: fetched %d (complete=%s)", realm_id, txn_type, total, is_complete)
        except Exception as exc:
            error_class, error_detail = classify_qbo_error(exc, entity=txn_type)
            if error_class == "entity_not_supported":
                logger.info("[v2:%s] %s: entity not supported — %s", realm_id, txn_type, error_detail)
                diagnostic["entities_not_supported"].append({"entity": txn_type, "reason": error_detail})
            else:
                logger.error("[v2:%s] %s fetch error (class=%s): %s", realm_id, txn_type, error_class, error_detail)
                diagnostic["api_errors"].append({"type": txn_type, "error": error_detail, "error_class": error_class})
                _banking_errors.append(f"{txn_type}: {error_detail}")
            diagnostic["txn_counts"][txn_type] = 0

    # Check entity — with fallback to Purchase(PaymentType='Check') for non-US companies
    try:
        check_results, check_info = client.get_checks_with_fallback(db, start_date=start_date)
        if check_info["used_fallback"]:
            # Log that Check entity was not supported and we used a fallback
            diagnostic["entities_not_supported"].append({
                "entity": "Check",
                "reason": check_info["fallback_reason"],
                "fallback_used": "Purchase WHERE PaymentType='Check'",
            })
            _banking_warnings.append(
                f"Check entity not supported for this company; used "
                f"Purchase(PaymentType='Check') fallback — "
                f"{check_info['total_fetched']} records fetched. "
                f"Duplicate IDs already in Purchase results are excluded."
            )
        _collect("Check", check_results, check_info)
        logger.info(
            "[v2:%s] Check (entity=%s fallback=%s): fetched %d (complete=%s)",
            realm_id,
            check_info["entity_used"],
            check_info["used_fallback"],
            check_info["total_fetched"],
            check_info["is_complete"],
        )
    except Exception as exc:
        error_class, error_detail = classify_qbo_error(exc, entity="Check")
        if error_class == "entity_not_supported":
            logger.info("[v2:%s] Check: entity not supported — %s", realm_id, error_detail)
            diagnostic["entities_not_supported"].append({"entity": "Check", "reason": error_detail})
        else:
            logger.error("[v2:%s] Check fetch error (class=%s): %s", realm_id, error_class, error_detail)
            diagnostic["api_errors"].append({"type": "Check", "error": error_detail, "error_class": error_class})
            _banking_errors.append(f"Check: {error_detail}")
        diagnostic["txn_counts"]["Check"] = 0

    # ── 3. Collect existing WorkItem txn_ids to avoid duplicates ────────────
    existing_txn_ids: set[str] = set(
        row[0]
        for row in db.query(WorkItem.qbo_txn_id)
        .filter(
            WorkItem.realm_id == realm_id,
            WorkItem.work_type == "CATEGORIZE",
            WorkItem.status.notin_(["rejected", "applied"]),
        )
        .all()
        if row[0]
    )

    # ── 4. Process each transaction ─────────────────────────────────────────
    accounts = _get_chart_of_accounts(db, client)   # cached fetch
    new_work_items: list[WorkItem] = []

    # ── 4-pre. Build duplicate/transfer detection structures (one pass, no extra API calls) ──
    seen_txn_signatures: dict[str, int] = {}
    cross_type_amounts: dict[str, set] = {"credit": set(), "debit": set()}
    for _tt, _txn in raw_txns:
        _payee = (_txn.get("EntityRef", {}).get("name") or "").lower().strip()
        _amt   = round(abs(float(_txn.get("TotalAmt") or _txn.get("Amount") or 0.0)), 2)
        _sig   = f"{_tt}|{_amt}|{_payee}"
        seen_txn_signatures[_sig] = seen_txn_signatures.get(_sig, 0) + 1
        if _tt in ("Deposit", "SalesReceipt"):
            cross_type_amounts["credit"].add(_amt)
        else:
            cross_type_amounts["debit"].add(_amt)

    for txn_type, txn in raw_txns:
        txn_id = str(txn.get("Id", ""))
        if not txn_id:
            diagnostic["skipped_items"].append({
                "txn_id": None, "type": txn_type, "reason": "missing Id"
            })
            continue

        diagnostic["posted_transactions_reviewed"] += 1

        # ── 4a. Extract metadata (needed for all detection paths) ───────────
        payee_name = (
            txn.get("EntityRef", {}).get("name")
            or txn.get("PaymentMethodRef", {}).get("name")
            or txn.get("CustomerRef", {}).get("name")
            or ""
        )
        memo = txn.get("PrivateNote") or txn.get("Memo") or ""
        try:
            txn_date_str = txn.get("TxnDate") or txn.get("MetaData", {}).get("CreateTime", "")[:10]
            txn_date = datetime.strptime(txn_date_str[:10], "%Y-%m-%d") if txn_date_str else None
        except Exception:
            txn_date = None
        amount = float(txn.get("TotalAmt") or txn.get("Amount") or 0.0)

        # ── 4b. Determine line account ──────────────────────────────────────
        detail_key = _txn_type_detail_key(txn_type)
        lines = txn.get(detail_key) or []
        if isinstance(lines, dict):
            lines = [lines]

        line_acct_id = None
        line_acct_name = ""
        line_acct_subtype = ""
        for line in lines:
            acct_ref = (
                line.get("AccountBasedExpenseLineDetail", {}).get("AccountRef")
                or line.get("SalesItemLineDetail", {}).get("ItemRef")
                or line.get("DepositLineDetail", {}).get("AccountRef")
                or line.get("AccountRef")
                or {}
            )
            if acct_ref.get("value"):
                line_acct_id = acct_ref["value"]
                line_acct_name = acct_ref.get("name", "")
                for a in accounts:
                    if str(a.get("Id")) == str(line_acct_id):
                        line_acct_subtype = a.get("AccountSubType", "")
                        break
                break

        # ── 4c. Classify the transaction ────────────────────────────────────
        is_uncategorized = _is_uncategorized_v2(line_acct_name, line_acct_subtype)
        is_generic = (not is_uncategorized) and _is_generic_category(line_acct_name)

        # Duplicate detection: same txn_type + payee + rounded amount
        _payee_lc = payee_name.lower().strip()
        _amt_rounded = round(abs(amount), 2)
        _dup_sig = f"{txn_type}|{_amt_rounded}|{_payee_lc}"
        is_possible_duplicate = seen_txn_signatures.get(_dup_sig, 0) > 1

        # Transfer detection: amount above threshold appears on both sides of books
        _xfer_threshold = materiality_limit * 0.05
        if txn_type in ("Deposit", "SalesReceipt"):
            is_possible_transfer = (
                _amt_rounded >= _xfer_threshold
                and _amt_rounded in cross_type_amounts["debit"]
            )
        else:
            is_possible_transfer = (
                _amt_rounded >= _xfer_threshold
                and _amt_rounded in cross_type_amounts["credit"]
            )

        # Apply company rules for ALL transactions (needed for POSSIBLE_MISCLASSIFICATION)
        matched_rule = _apply_company_rules(company_rules, payee_name, memo, amount)

        # Determine review_type — priority order matters
        if is_uncategorized:
            review_type = "UNCATEGORIZED"
            action_needed = True
        elif is_generic:
            review_type = "GENERIC_CATEGORY"
            action_needed = True
        elif matched_rule and matched_rule.rule_type == "FLAG":
            review_type = "POSSIBLE_MISCLASSIFICATION"
            action_needed = True
        elif is_possible_duplicate:
            review_type = "POSSIBLE_DUPLICATE"
            action_needed = True
        elif is_possible_transfer:
            review_type = "POSSIBLE_TRANSFER"
            action_needed = True
        else:
            review_type = "OK"
            action_needed = False

        # Update counters
        if not action_needed:
            diagnostic["items_ok"] += 1
            continue   # well-categorized — no WorkItem needed

        if review_type == "UNCATEGORIZED":
            diagnostic["items_uncategorized"] += 1
        else:
            diagnostic["items_needing_review"] += 1

        # ── 4d. Deduplicate WorkItems ───────────────────────────────────────
        if txn_id in existing_txn_ids:
            diagnostic["skipped_items"].append({
                "txn_id": txn_id, "type": txn_type, "reason": "already has pending WorkItem"
            })
            continue

        # ── 4e. Determine proposed account, confidence, reason ──────────────
        proposed_account_id = None
        proposed_account_name = None
        confidence = "none"
        reason = ""
        rule_type = "CATEGORIZE"

        if matched_rule:
            if matched_rule.rule_type == "SKIP":
                diagnostic["skipped_items"].append({
                    "txn_id": txn_id, "type": txn_type,
                    "reason": f"company rule SKIP: {matched_rule.rule_name}",
                })
                continue
            elif matched_rule.rule_type == "FLAG":
                rule_type = "FLAG"
                confidence = "low"
                reason = f"Company rule FLAG: {matched_rule.rule_name} — requires manual review."
            else:
                rule_type = "CLIENT_RULE"
                proposed_account_id = matched_rule.proposed_account_id
                proposed_account_name = matched_rule.proposed_account_name
                confidence = matched_rule.confidence_strength or "high"
                reason = f"Company rule '{matched_rule.rule_name}' matched → {proposed_account_name}."
        else:
            # No matching company rule — use global keyword library for UNCATEGORIZED/GENERIC
            if review_type in ("UNCATEGORIZED", "GENERIC_CATEGORY"):
                acct_kw, _, rule_label = _match_rule(payee_name, memo)
                if acct_kw and rule_label != "no_match":
                    # _find_coa_account expects the profile COA (lowercase keys: "id","name","type")
                    _coa_id, _coa_name, _ = _find_coa_account(
                        profile.chart_of_accounts or [], acct_kw
                    )
                    proposed_account_id = _coa_id
                    proposed_account_name = _coa_name or acct_kw
                    confidence = "medium"
                    reason = _build_reason(acct_kw, rule_label, payee_name, memo)
                else:
                    reason = _build_reason(None, "no_match", payee_name, memo)
            elif review_type == "POSSIBLE_DUPLICATE":
                rule_type = "FLAG"
                confidence = "low"
                reason = (
                    f"Possible duplicate: another {txn_type} for the same payee and "
                    f"amount (${_amt_rounded:.2f}) exists within the review window. "
                    "Verify this is not an accidental double-entry."
                )
            elif review_type == "POSSIBLE_TRANSFER":
                rule_type = "FLAG"
                confidence = "low"
                reason = (
                    f"Possible transfer: a matching amount (${_amt_rounded:.2f}) appears "
                    "on both sides of the books. Verify this is not a transfer between "
                    "accounts that should be excluded from P&L."
                )
            else:
                reason = _build_reason(None, "no_match", payee_name, memo)

        # ── 4f. Assess risk and autonomy ─────────────────────────────────────
        risk = _assess_risk(amount, materiality_limit)
        autonomy_level = classify_autonomy(
            confidence=confidence,
            risk=risk,
            rule_type=rule_type,
            has_proposed_account=bool(proposed_account_id),
            amount=amount,
            materiality_limit=materiality_limit,
        )

        # ── 4g. Create WorkItem (DB only — NO QBO write) ────────────────────
        wi = WorkItem(
            company_id=company.id,
            realm_id=realm_id,
            work_type="CATEGORIZE",
            qbo_txn_id=txn_id,
            qbo_txn_type=txn_type,
            txn_date=txn_date,
            amount=amount,
            payee_name=payee_name or None,
            memo=memo or None,
            current_account_id=line_acct_id,
            current_account_name=line_acct_name or None,
            proposed_account_id=proposed_account_id,
            proposed_account_name=proposed_account_name,
            confidence=confidence,
            risk=risk,
            autonomy_level=autonomy_level,
            rule_matched=matched_rule.rule_name if matched_rule else None,
            reason=reason,
            status="pending",
        )
        db.add(wi)
        new_work_items.append(wi)
        existing_txn_ids.add(txn_id)

        # Tally autonomy breakdown
        level_names = {0: "AUTO", 1: "BATCH", 2: "INDIVIDUAL", 3: "HUMAN_REQUIRED"}
        diagnostic["autonomy_breakdown"][level_names[autonomy_level]] += 1

    # ── 5. Flush to get IDs (no commit — caller commits) ────────────────────
    try:
        db.flush()
        diagnostic["work_items_created"] = len(new_work_items)
    except Exception as exc:
        logger.error("[v2:%s] DB flush error: %s", realm_id, exc)
        diagnostic["api_errors"].append({"type": "DB_FLUSH", "error": str(exc)})
        _banking_errors.append(f"DB_FLUSH: {exc}")
        db.rollback()
        # Still update module before returning
        module_results["BANKING"]["status"] = "FAILED"
        module_results["BANKING"]["message"] = f"Database flush error: {exc}"
        module_results["BANKING"]["completed_at"] = _now_iso()
        diagnostic["engine_status"] = "FAILED"
        return [], diagnostic

    # ── 6. Update BANKING module status (Phase 2) ────────────────────────────
    module_results["BANKING"]["completed_at"] = _now_iso()
    module_results["BANKING"]["items_scanned"] = diagnostic["posted_transactions_reviewed"]
    module_results["BANKING"]["issues_found"] = (
        diagnostic["items_uncategorized"] + diagnostic["items_needing_review"]
    )
    module_results["BANKING"]["work_items_created"] = diagnostic["work_items_created"]
    module_results["BANKING"]["errors"] = _banking_errors
    module_results["BANKING"]["warnings"] = _banking_warnings

    if _banking_errors and not _banking_warnings:
        module_results["BANKING"]["status"] = "PARTIALLY_COMPLETED"
        module_results["BANKING"]["message"] = (
            f"{len(_banking_errors)} entity fetch error(s). "
            f"Scanned {diagnostic['posted_transactions_reviewed']} transactions successfully retrieved."
        )
    elif _banking_errors:
        module_results["BANKING"]["status"] = "COMPLETED_WITH_WARNINGS"
        module_results["BANKING"]["message"] = (
            f"{len(_banking_errors)} error(s), {len(_banking_warnings)} warning(s). "
            f"Scanned {diagnostic['posted_transactions_reviewed']} transactions."
        )
    elif _banking_warnings:
        module_results["BANKING"]["status"] = "COMPLETED_WITH_WARNINGS"
        module_results["BANKING"]["message"] = (
            f"{len(_banking_warnings)} warning(s) — see details. "
            f"Scanned {diagnostic['posted_transactions_reviewed']} transactions."
        )
    else:
        module_results["BANKING"]["status"] = "COMPLETED"
        module_results["BANKING"]["message"] = (
            f"Scanned {diagnostic['posted_transactions_reviewed']} posted transactions. "
            f"{diagnostic['items_uncategorized']} uncategorized · "
            f"{diagnostic['items_needing_review']} needing review · "
            f"{diagnostic['items_ok']} OK."
        )

    # ── 7. Derive overall engine status from module statuses (Phase 2) ───────
    _active_statuses = [
        m.get("status") for m in module_results.values()
        if m.get("status") not in ("NOT_IMPLEMENTED", "NOT_APPLICABLE", "NOT_STARTED")
    ]
    if not _active_statuses:
        diagnostic["engine_status"] = "NO_MODULES_RAN"
    elif all(s == "COMPLETED" for s in _active_statuses):
        diagnostic["engine_status"] = "COMPLETED"
    elif any(s == "FAILED" for s in _active_statuses):
        # At least one module failed — if any still completed (even partially), mark partial
        _completed_any = any(
            s in ("COMPLETED", "COMPLETED_WITH_WARNINGS", "PARTIALLY_COMPLETED")
            for s in _active_statuses
        )
        diagnostic["engine_status"] = "PARTIALLY_COMPLETED" if _completed_any else "FAILED"
    elif any(s in ("COMPLETED_WITH_WARNINGS", "PARTIALLY_COMPLETED") for s in _active_statuses):
        diagnostic["engine_status"] = "COMPLETED_WITH_WARNINGS"
    else:
        diagnostic["engine_status"] = "COMPLETED"

    logger.info(
        "[v2:%s] Done. reviewed=%d uncategorized=%d needs_review=%d ok=%d "
        "created=%d not_supported=%d errors=%d autonomy=%s",
        realm_id,
        diagnostic["posted_transactions_reviewed"],
        diagnostic["items_uncategorized"],
        diagnostic["items_needing_review"],
        diagnostic["items_ok"],
        diagnostic["work_items_created"],
        len(diagnostic["entities_not_supported"]),
        len(diagnostic["api_errors"]),
        diagnostic["autonomy_breakdown"],
    )
    # Safety assertion — if this ever fails, something went very wrong
    assert diagnostic["qbo_writes"] == 0, "BUG: qbo_writes must be 0 in run_categorization_v2"

    return new_work_items, diagnostic


def _get_chart_of_accounts(db: Session, client: "QBOClient") -> list:
    """Fetch COA from QBO; return [] on error (never raises)."""
    try:
        return client.get_accounts(db) or []
    except Exception as exc:
        logger.warning("_get_chart_of_accounts error: %s", exc)
        return []


# ── execute_work_item ────────────────────────────────────────────────────────

def execute_work_item(db: Session, company, work_item: WorkItem) -> dict:
    """Apply an APPROVED WorkItem to QBO.

    Safety invariants:
        - Refuses if work_item.status != 'approved'
        - Re-fetches the QBO transaction fresh (never uses stale cached data)
        - Sets status to 'executing' before the QBO call
        - Verifies the write by re-reading the transaction post-update
        - On any failure: rolls back status to 'approved' (retryable)
        - Writes a ChangeLog audit entry regardless of outcome

    Returns:
        {'success': bool, 'message': str, 'qbo_response': dict|None}
    """
    from app.database import ChangeLog  # local import

    if work_item.status != "approved":
        return {
            "success": False,
            "message": f"WorkItem {work_item.id} is not approved (status={work_item.status}). Cannot execute.",
            "qbo_response": None,
        }

    if not work_item.proposed_account_id:
        return {
            "success": False,
            "message": "No proposed_account_id — cannot write to QBO.",
            "qbo_response": None,
        }

    client = QBOClient(company)
    realm_id = company.realm_id

    # ── Step 1: Mark as executing ────────────────────────────────────────────
    work_item.status = "executing"
    work_item.updated_at = _now()
    try:
        db.flush()
    except Exception as exc:
        logger.error("[execute:%s] status→executing flush error: %s", work_item.id, exc)
        work_item.status = "approved"
        db.rollback()
        return {"success": False, "message": f"DB error setting executing: {exc}", "qbo_response": None}

    # ── Step 2: Re-fetch QBO transaction (fresh read) ────────────────────────
    try:
        fresh_txn = client.get_transaction(db, work_item.qbo_txn_type, work_item.qbo_txn_id)
    except Exception as exc:
        logger.error("[execute:%s] QBO re-fetch error: %s", work_item.id, exc)
        work_item.status = "approved"
        work_item.error_message = f"QBO re-fetch failed: {exc}"
        db.flush()
        return {"success": False, "message": f"QBO re-fetch failed: {exc}", "qbo_response": None}

    if not fresh_txn:
        work_item.status = "approved"
        work_item.error_message = "QBO returned empty transaction on re-fetch"
        db.flush()
        return {"success": False, "message": "QBO returned empty transaction", "qbo_response": None}

    # ── Step 3: Patch the account reference on the first matching line ───────
    detail_key = _txn_type_detail_key(work_item.qbo_txn_type)
    lines = fresh_txn.get(detail_key) or []
    if isinstance(lines, dict):
        lines = [lines]

    patched = False
    for line in lines:
        for detail_field in (
            "AccountBasedExpenseLineDetail",
            "SalesItemLineDetail",
            "DepositLineDetail",
        ):
            if detail_field in line:
                line[detail_field]["AccountRef"] = {
                    "value": work_item.proposed_account_id,
                    "name": work_item.proposed_account_name or "",
                }
                patched = True
                break
        # Flat AccountRef (Deposit top-level)
        if not patched and "AccountRef" in line:
            line["AccountRef"] = {
                "value": work_item.proposed_account_id,
                "name": work_item.proposed_account_name or "",
            }
            patched = True
        if patched:
            break

    if not patched:
        work_item.status = "approved"
        work_item.error_message = "Could not locate line to patch account reference"
        db.flush()
        return {
            "success": False,
            "message": "No patchable account line found in transaction",
            "qbo_response": None,
        }

    # ── Step 4: Write to QBO ─────────────────────────────────────────────────
    txn_type_lower = work_item.qbo_txn_type.lower()
    try:
        qbo_response = client._post(db, f"/{txn_type_lower}", fresh_txn)
    except Exception as exc:
        logger.error("[execute:%s] QBO write error: %s", work_item.id, exc)
        work_item.status = "approved"
        work_item.error_message = f"QBO write failed: {exc}"
        db.flush()
        return {"success": False, "message": f"QBO write failed: {exc}", "qbo_response": None}

    # ── Step 5: Verify the write ─────────────────────────────────────────────
    verification_status = "unverified"
    try:
        verified_txn = client.get_transaction(db, work_item.qbo_txn_type, work_item.qbo_txn_id)
        v_lines = verified_txn.get(detail_key) or []
        if isinstance(v_lines, dict):
            v_lines = [v_lines]
        for vl in v_lines:
            for dfield in ("AccountBasedExpenseLineDetail", "SalesItemLineDetail", "DepositLineDetail"):
                if dfield in vl:
                    written_id = vl[dfield].get("AccountRef", {}).get("value")
                    if str(written_id) == str(work_item.proposed_account_id):
                        verification_status = "verified"
                    else:
                        verification_status = "mismatch"
                    break
            if verification_status != "unverified":
                break
    except Exception as exc:
        logger.warning("[execute:%s] Verification read error: %s", work_item.id, exc)
        verification_status = "verify_error"

    # ── Step 6: Update WorkItem ──────────────────────────────────────────────
    work_item.status = "applied"
    work_item.executed_at = _now()
    work_item.execution_result = qbo_response
    work_item.qbo_update_response = qbo_response
    work_item.verification_status = verification_status
    work_item.verified_at = _now()
    work_item.updated_at = _now()

    # ── Step 7: Audit log ────────────────────────────────────────────────────
    try:
        cl = ChangeLog(
            company_id=company.id,
            realm_id=realm_id,
            entity_type=work_item.qbo_txn_type,
            entity_id=work_item.qbo_txn_id,
            action_type="CATEGORIZE",
            description=f"WorkItem {work_item.id}: {work_item.current_account_name} → {work_item.proposed_account_name}",
            original_value={"account_id": work_item.current_account_id, "account_name": work_item.current_account_name},
            new_value={"account_id": work_item.proposed_account_id, "account_name": work_item.proposed_account_name},
        )
        db.add(cl)
    except Exception as exc:
        logger.warning("[execute:%s] ChangeLog write error: %s", work_item.id, exc)

    try:
        db.flush()
    except Exception as exc:
        logger.error("[execute:%s] Final flush error: %s", work_item.id, exc)
        db.rollback()
        return {"success": False, "message": f"DB error after QBO write: {exc}", "qbo_response": qbo_response}

    logger.info(
        "[execute:%s] Applied. account=%s verification=%s",
        work_item.id, work_item.proposed_account_name, verification_status,
    )
    return {
        "success": True,
        "message": f"Applied. Account set to '{work_item.proposed_account_name}'. Verification: {verification_status}.",
        "qbo_response": qbo_response,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — AR PAYMENT MATCHING ENGINE
# ═══════════════════════════════════════════════════════════════

def run_ar_matching(
    db: Session,
    company: Company,
    profile: CompanyProfile,
    lookback_days: int = 180,
) -> list[ProposedARMatch]:
    """
    Fetch open invoices and unapplied payments from QBO.
    Propose matches based on amount, customer, and date proximity.
    Returns newly created ProposedARMatch records (caller must commit).
    """
    if not profile or not profile.data_as_of:
        return []

    client = QBOClient(company)
    start_date = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    try:
        open_invoices = client.get_open_invoices(db)
    except Exception:
        open_invoices = []

    try:
        payments = client.get_payments(db, start_date=start_date)
    except Exception:
        payments = []

    # Filter unapplied (or partially applied) payments
    unapplied = []
    for pmt in payments:
        unapplied_amt = _payment_unapplied_amount(pmt)
        if unapplied_amt > 0.01:
            unapplied.append({
                "id": pmt.get("Id"),
                "date": pmt.get("TxnDate"),
                "amount": float(pmt.get("TotalAmt", 0)),
                "unapplied": unapplied_amt,
                "customer_id": pmt.get("CustomerRef", {}).get("value"),
                "customer_name": pmt.get("CustomerRef", {}).get("name", ""),
                "method": pmt.get("PaymentMethodRef", {}).get("name", ""),
                "memo": pmt.get("PrivateNote", "") or "",
            })

    # Already proposed pairs
    existing_pairs = {
        (r.invoice_id, r.payment_id)
        for r in db.query(ProposedARMatch.invoice_id, ProposedARMatch.payment_id)
                   .filter_by(realm_id=company.realm_id)
                   .filter(ProposedARMatch.status.in_(["pending", "approved"]))
                   .all()
    }

    proposals: list[ProposedARMatch] = []

    for inv in open_invoices:
        inv_id = inv.get("Id")
        inv_customer_id = inv.get("CustomerRef", {}).get("value")
        inv_customer_name = inv.get("CustomerRef", {}).get("name", "")
        inv_balance = float(inv.get("Balance", 0))
        inv_amount = float(inv.get("TotalAmt", 0))
        inv_date = inv.get("TxnDate", "")
        inv_due = inv.get("DueDate", "")
        inv_num = inv.get("DocNumber", "")

        if inv_balance < 0.01:
            continue

        # Find matching payments for this customer
        customer_payments = [
            p for p in unapplied
            if p["customer_id"] == inv_customer_id
        ]

        for pmt in customer_payments:
            if (inv_id, pmt["id"]) in existing_pairs:
                continue

            unapplied_amt = pmt["unapplied"]
            match_amount = min(inv_balance, unapplied_amt)

            # Confidence scoring
            confidence, reasons = _score_ar_match(inv, pmt, match_amount)

            if confidence == "low" and match_amount < 1.0:
                continue  # Skip negligible low-confidence matches

            diff = inv_balance - match_amount

            proposal = ProposedARMatch(
                company_id=company.id,
                realm_id=company.realm_id,
                invoice_id=inv_id,
                invoice_number=inv_num,
                invoice_date=datetime.strptime(inv_date, "%Y-%m-%d") if inv_date else None,
                invoice_due_date=datetime.strptime(inv_due, "%Y-%m-%d") if inv_due else None,
                invoice_amount=inv_amount,
                invoice_balance=inv_balance,
                customer_name=inv_customer_name,
                payment_id=pmt["id"],
                payment_date=datetime.strptime(pmt["date"], "%Y-%m-%d") if pmt["date"] else None,
                payment_amount=pmt["amount"],
                payment_method=pmt["method"],
                payment_memo=pmt["memo"][:500] if pmt["memo"] else "",
                match_amount=match_amount,
                match_confidence=confidence,
                match_reason="; ".join(reasons),
                amount_difference=diff,
            )
            db.add(proposal)
            proposals.append(proposal)
            existing_pairs.add((inv_id, pmt["id"]))

            # Reduce available unapplied amount for next iteration
            pmt["unapplied"] -= match_amount
            if pmt["unapplied"] < 0.01:
                break

    return proposals


def _payment_unapplied_amount(pmt: dict) -> float:
    """Calculate how much of a payment is still unapplied."""
    total = float(pmt.get("TotalAmt", 0))
    applied = sum(
        float(line.get("Amount", 0))
        for line in pmt.get("Line", [])
        if line.get("LinkedTxn")
    )
    return max(0.0, total - applied)


def _score_ar_match(inv: dict, pmt: dict, match_amount: float) -> tuple[str, list[str]]:
    """Return (confidence, [reason strings]) for an invoice-payment pair."""
    reasons = []
    score = 0

    inv_balance = float(inv.get("Balance", 0))
    pmt_amount = float(pmt.get("TotalAmt", 0))

    # Exact amount match
    if abs(inv_balance - pmt_amount) < 0.02:
        score += 3
        reasons.append(f"Exact amount match: ${match_amount:,.2f}")
    elif abs(inv_balance - match_amount) < 0.02:
        score += 2
        reasons.append(f"Partial match covers full invoice balance: ${match_amount:,.2f}")
    else:
        score += 1
        reasons.append(f"Partial match: ${match_amount:,.2f} of ${inv_balance:,.2f}")

    # Date proximity (payment within 60 days of invoice)
    try:
        inv_dt = datetime.strptime(inv.get("TxnDate", ""), "%Y-%m-%d")
        pmt_dt = datetime.strptime(pmt.get("date", ""), "%Y-%m-%d")
        days_gap = abs((pmt_dt - inv_dt).days)
        if days_gap <= 30:
            score += 2
            reasons.append(f"Payment within {days_gap} days of invoice")
        elif days_gap <= 60:
            score += 1
            reasons.append(f"Payment {days_gap} days after invoice")
        else:
            reasons.append(f"Payment {days_gap} days after invoice — verify")
    except ValueError:
        pass

    # Memo / reference match
    inv_num = inv.get("DocNumber", "").lower()
    pmt_memo = pmt.get("memo", "").lower()
    if inv_num and inv_num in pmt_memo:
        score += 2
        reasons.append(f"Invoice #{inv_num} referenced in payment memo")

    confidence = "high" if score >= 5 else "medium" if score >= 3 else "low"
    return confidence, reasons


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — BANK RECONCILIATION ENGINE
# ═══════════════════════════════════════════════════════════════

def run_bank_reconciliation(
    db: Session,
    company: Company,
    profile: CompanyProfile,
    account_id: str,
    statement_end_date: str,
    statement_ending_balance: float,
    statement_transactions: list[dict] | None = None,
) -> dict:
    """
    Generate a bank reconciliation worksheet for one bank account.

    `statement_transactions` (optional): list of {date, description, amount, type}
    dicts from the bank statement CSV. When provided, auto-matches are attempted.

    Returns a dict with:
        account_name, qbo_balance, statement_balance, difference,
        matched_items, unmatched_qbo, unmatched_statement,
        adjustments_needed, status
    """
    if not profile or not profile.data_as_of:
        return {"error": "No QBO data synced. Run a sync first."}

    coa = profile.chart_of_accounts or []
    account = next(
        (a for a in coa if a.get("id") == account_id
         or a.get("name", "").lower() == account_id.lower()),
        None,
    )
    if not account:
        return {"error": f"Account '{account_id}' not found in chart of accounts."}

    acct_name = account.get("name", account_id)
    qbo_balance = float(account.get("balance", 0))
    difference = round(statement_ending_balance - qbo_balance, 2)

    client = QBOClient(company)

    # Fetch QBO transactions for this account in the period
    try:
        # Use General Ledger for detailed account activity
        start_date = (datetime.strptime(statement_end_date, "%Y-%m-%d")
                      - timedelta(days=90)).strftime("%Y-%m-%d")
        gl = client.get_general_ledger(db, start_date, statement_end_date)
        qbo_txns = _extract_gl_account_txns(gl, acct_name)
    except Exception as e:
        qbo_txns = []

    result: dict = {
        "account_id": account_id,
        "account_name": acct_name,
        "qbo_balance": qbo_balance,
        "statement_balance": statement_ending_balance,
        "statement_end_date": statement_end_date,
        "difference": difference,
        "matched_items": [],
        "unmatched_qbo": [],
        "unmatched_statement": [],
        "adjustments_needed": [],
        "status": "reconciled" if abs(difference) < 0.02 else "unreconciled",
    }

    if not statement_transactions:
        # Return worksheet without auto-matching
        result["unmatched_qbo"] = qbo_txns
        if abs(difference) > 0.01:
            result["adjustments_needed"].append({
                "type": "difference",
                "amount": difference,
                "description": (
                    f"Unexplained difference of ${abs(difference):,.2f}. "
                    "Upload bank statement CSV for auto-matching."
                ),
            })
        return result

    # Auto-match QBO transactions to statement items
    matched_qbo_ids = set()
    matched_stmt_indices = set()

    for qi, qtxn in enumerate(qbo_txns):
        for si, stxn in enumerate(statement_transactions):
            if si in matched_stmt_indices:
                continue
            q_amt = round(float(qtxn.get("amount", 0)), 2)
            s_amt = round(float(stxn.get("amount", 0)), 2)
            if abs(q_amt - s_amt) < 0.02:
                # Amount matches — check date proximity
                try:
                    qd = datetime.strptime(qtxn.get("date", ""), "%Y-%m-%d")
                    sd = datetime.strptime(stxn.get("date", ""), "%Y-%m-%d")
                    if abs((qd - sd).days) <= 5:
                        result["matched_items"].append({
                            "qbo": qtxn, "statement": stxn, "amount": q_amt,
                        })
                        matched_qbo_ids.add(qi)
                        matched_stmt_indices.add(si)
                        break
                except ValueError:
                    pass

    result["unmatched_qbo"] = [t for i, t in enumerate(qbo_txns) if i not in matched_qbo_ids]
    result["unmatched_statement"] = [t for i, t in enumerate(statement_transactions) if i not in matched_stmt_indices]

    # Flag unmatched items as potential adjustments
    for t in result["unmatched_statement"]:
        result["adjustments_needed"].append({
            "type": "missing_in_qbo",
            "amount": t.get("amount", 0),
            "date": t.get("date"),
            "description": f"Bank statement item not found in QBO: {t.get('description', '')}",
            "action": "Enter this transaction in QBO",
        })

    for t in result["unmatched_qbo"]:
        result["adjustments_needed"].append({
            "type": "missing_in_statement",
            "amount": t.get("amount", 0),
            "date": t.get("date"),
            "description": f"QBO transaction not on bank statement: {t.get('memo', '')}",
            "action": "Verify this is an outstanding item (check, deposit in transit)",
        })

    if abs(difference) < 0.02:
        result["status"] = "reconciled"
    elif result["adjustments_needed"]:
        result["status"] = "items_to_clear"
    else:
        result["status"] = "unexplained_difference"

    return result


def _extract_gl_account_txns(gl: dict, account_name: str) -> list[dict]:
    """Extract transactions for one account from a General Ledger report."""
    txns = []
    rows = gl.get("Rows", {}).get("Row", [])
    in_account = False

    for row in rows:
        if row.get("type") == "Section":
            header = row.get("Header", {}).get("ColData", [])
            section_name = header[0].get("value", "") if header else ""
            in_account = account_name.lower() in section_name.lower()

        if not in_account:
            continue

        if row.get("type") == "Data":
            cols = row.get("ColData", [])
            if len(cols) >= 5:
                txns.append({
                    "date": cols[0].get("value", ""),
                    "type": cols[1].get("value", ""),
                    "doc_num": cols[2].get("value", ""),
                    "memo": cols[3].get("value", ""),
                    "amount": _safe_float(cols[5].get("value", "0")),
                    "id": cols[0].get("id", ""),
                })
    return txns


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — APPLY APPROVED CATEGORIZATIONS
# ═══════════════════════════════════════════════════════════════

def apply_categorization(
    db: Session,
    company: Company,
    proposal: ProposedCategorization,
) -> dict:
    """
    Apply an approved categorization by updating the QBO transaction.
    Returns {success, qbo_response, error}.
    """
    if proposal.status != "approved":
        return {"success": False, "error": "Proposal not approved"}
    if not proposal.suggested_account_id:
        return {"success": False, "error": "No suggested account ID — cannot update QBO"}

    client = QBOClient(company)

    try:
        # Fetch current transaction
        txn = client.get_transaction(db, proposal.qbo_txn_type, proposal.qbo_txn_id)
        entity_key = proposal.qbo_txn_type  # "Purchase", "Deposit", etc.
        txn_data = txn.get(entity_key, txn)

        # Update the line's account reference
        for line in txn_data.get("Line", []):
            detail_key = f"{_txn_type_detail_key(proposal.qbo_txn_type)}"
            if detail_key in line:
                current_acct = line[detail_key].get("AccountRef", {}).get("value")
                if current_acct == proposal.current_account_id:
                    line[detail_key]["AccountRef"] = {
                        "value": proposal.suggested_account_id,
                        "name": proposal.suggested_account_name,
                    }

        # Post updated transaction
        resp = client._post(db, f"/{proposal.qbo_txn_type.lower()}", txn_data)

        proposal.status = "applied"
        proposal.applied_at = _now()
        proposal.qbo_update_response = resp

        # Log the change
        log = ChangeLog(
            company_id=company.id,
            realm_id=company.realm_id,
            action_type="categorize_transaction",
            entity_type=proposal.qbo_txn_type,
            entity_id=proposal.qbo_txn_id,
            description=(
                f"Reclassified ${proposal.amount:,.2f} from "
                f"'{proposal.current_account_name}' → '{proposal.suggested_account_name}'"
            ),
            original_value={"account": proposal.current_account_name},
            new_value={"account": proposal.suggested_account_name},
            reason=proposal.reason,
            performed_by="AI Controller",
            approved_by=proposal.approved_by,
            human_approval=True,
            status="success",
        )
        db.add(log)
        db.commit()

        return {"success": True, "qbo_response": resp}

    except Exception as e:
        db.rollback()
        proposal.status = "pending"  # reset so it can be retried
        db.commit()
        return {"success": False, "error": str(e)}


def _txn_type_detail_key(txn_type: str) -> str:
    """Return the QBO line detail key for a given transaction type."""
    return {
        "Purchase": "AccountBasedExpenseLineDetail",
        "Deposit": "DepositLineDetail",
        "JournalEntry": "JournalEntryLineDetail",
        "Check": "AccountBasedExpenseLineDetail",
    }.get(txn_type, "AccountBasedExpenseLineDetail")


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — APPLY APPROVED AR MATCHES
# ═══════════════════════════════════════════════════════════════

def apply_ar_match(
    db: Session,
    company: Company,
    match: ProposedARMatch,
) -> dict:
    """
    Apply an approved AR match by updating the QBO Payment to link it to the Invoice.
    """
    if match.status != "approved":
        return {"success": False, "error": "Match not approved"}

    client = QBOClient(company)

    try:
        # Fetch the payment
        pmt_resp = client.get_transaction(db, "Payment", match.payment_id)
        pmt = pmt_resp.get("Payment", pmt_resp)

        # Add the invoice link
        pmt.setdefault("Line", []).append({
            "Amount": match.match_amount,
            "LinkedTxn": [{
                "TxnId": match.invoice_id,
                "TxnType": "Invoice",
            }],
        })

        resp = client._post(db, "/payment", pmt)

        match.status = "applied"
        match.applied_at = _now()
        match.qbo_response = resp

        log = ChangeLog(
            company_id=company.id,
            realm_id=company.realm_id,
            action_type="apply_payment",
            entity_type="Payment",
            entity_id=match.payment_id,
            description=(
                f"Applied ${match.match_amount:,.2f} payment from {match.customer_name} "
                f"to Invoice #{match.invoice_number}"
            ),
            reason="AI-proposed AR match, approved by controller",
            performed_by="AI Controller",
            approved_by=match.approved_by,
            human_approval=True,
            status="success",
        )
        db.add(log)
        db.commit()

        return {"success": True, "qbo_response": resp}

    except Exception as e:
        db.rollback()
        match.status = "pending"
        db.commit()
        return {"success": False, "error": str(e)}
