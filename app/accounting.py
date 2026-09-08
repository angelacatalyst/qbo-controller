"""
QBO AI Accounting Analysis Engine
───────────────────────────────────
Performs Controller-level analysis on live QBO data.
Every analysis is strictly scoped to one realm_id.

WORKFLOW:  DETECT → ANALYZE → RECOMMEND → REVIEW → APPROVE → EXECUTE → VERIFY → DOCUMENT
"""
from datetime import datetime, date, timedelta
from typing import Any, Optional
from sqlalchemy.orm import Session

from app.database import (
    Company, CompanyProfile, AccountingIssue, MonthEndClose, _now
)


# ─── Constants ────────────────────────────────────────────────

SEVERITY = {"critical": 4, "high": 3, "medium": 2, "low": 1}

HEALTH_WEIGHTS = {
    "bank_reconciliation":      20,
    "credit_card_reconciliation": 8,
    "balance_sheet_integrity":  18,
    "pl_integrity":             10,
    "ar_status":                10,
    "ap_status":                 8,
    "uncategorized_transactions": 10,
    "payroll":                   6,
    "sales_tax":                 5,
    "equity_review":             5,
}  # Total: 100

HEALTH_LABELS = {
    (90, 100): ("Excellent", "success"),
    (80,  89): ("Good",      "primary"),
    (70,  79): ("Needs Attention", "warning"),
    (60,  69): ("Significant Cleanup", "orange"),
    (0,   59): ("High Risk",  "danger"),
}

MONTH_END_STEPS = [
    {"step_num": 1,  "title": "Synchronize QBO Data"},
    {"step_num": 2,  "title": "Review Bank Transactions"},
    {"step_num": 3,  "title": "Review Credit Card Transactions"},
    {"step_num": 4,  "title": "Reconcile Bank Accounts"},
    {"step_num": 5,  "title": "Reconcile Credit Cards"},
    {"step_num": 6,  "title": "Review Revenue"},
    {"step_num": 7,  "title": "Review Expenses"},
    {"step_num": 8,  "title": "Review Accounts Receivable"},
    {"step_num": 9,  "title": "Review Accounts Payable"},
    {"step_num": 10, "title": "Review Payroll"},
    {"step_num": 11, "title": "Review Sales Tax"},
    {"step_num": 12, "title": "Review Loans"},
    {"step_num": 13, "title": "Review Fixed Assets"},
    {"step_num": 14, "title": "Review Equity"},
    {"step_num": 15, "title": "Review Balance Sheet"},
    {"step_num": 16, "title": "Review Profit & Loss"},
    {"step_num": 17, "title": "Identify Adjustments Needed"},
    {"step_num": 18, "title": "Prepare Journal Entries"},
    {"step_num": 19, "title": "Obtain Approval for Adjustments"},
    {"step_num": 20, "title": "Post Approved Journal Entries"},
    {"step_num": 21, "title": "Re-run Financial Reports"},
    {"step_num": 22, "title": "Verify Reports"},
    {"step_num": 23, "title": "Close the Month"},
    {"step_num": 24, "title": "Generate Controller Report"},
]


# ─── Issue Factory ────────────────────────────────────────────

def _make_issue_id(db: Session, realm_id: str) -> str:
    count = db.query(AccountingIssue).filter_by(realm_id=realm_id).count()
    return f"ISS-{count + 1:04d}"


def _create_issue(db: Session, realm_id: str, company_id: str, **kwargs) -> AccountingIssue:
    issue = AccountingIssue(
        issue_id=_make_issue_id(db, realm_id),
        company_id=company_id,
        realm_id=realm_id,
        **kwargs,
    )
    db.add(issue)
    return issue


# ─── Report Parsers ───────────────────────────────────────────

def _extract_report_rows(report_data: dict) -> list:
    """Flatten QBO report rows into a list of {label, value} dicts."""
    rows = []
    if not report_data:
        return rows

    def walk(sections):
        for section in (sections if isinstance(sections, list) else [sections]):
            header = section.get("Header", {})
            col_data = header.get("ColData", [])
            if col_data:
                rows.append({
                    "label": col_data[0].get("value", "") if col_data else "",
                    "value": col_data[1].get("value", "0") if len(col_data) > 1 else "0",
                    "id": col_data[0].get("id", "") if col_data else "",
                })
            for row in section.get("Rows", {}).get("Row", []):
                if row.get("type") == "Data":
                    cd = row.get("ColData", [])
                    rows.append({
                        "label": cd[0].get("value", "") if cd else "",
                        "value": cd[1].get("value", "0") if len(cd) > 1 else "0",
                        "id": cd[0].get("id", "") if cd else "",
                    })
                elif row.get("type") == "Section":
                    walk([row])

    walk(report_data.get("Rows", {}).get("Row", []))
    return rows


def _safe_float(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


def _parse_balance_sheet(bs_data: dict) -> dict:
    """Extract key balances from Balance Sheet report."""
    rows = _extract_report_rows(bs_data)
    result = {
        "total_assets": 0.0, "total_liabilities": 0.0, "total_equity": 0.0,
        "current_assets": 0.0, "current_liabilities": 0.0,
        "accounts_receivable": 0.0, "accounts_payable": 0.0,
        "cash_and_bank": 0.0, "opening_balance_equity": 0.0,
        "retained_earnings": 0.0, "undeposited_funds": 0.0,
        "raw_rows": rows,
    }
    for r in rows:
        lbl = r["label"].lower()
        val = _safe_float(r["value"])
        if "total assets" in lbl:
            result["total_assets"] = val
        elif "total liabilities" in lbl:
            result["total_liabilities"] = val
        elif "total equity" in lbl or "total stockholder" in lbl:
            result["total_equity"] = val
        elif "accounts receivable" in lbl:
            result["accounts_receivable"] = val
        elif "accounts payable" in lbl:
            result["accounts_payable"] = val
        elif "opening balance equity" in lbl:
            result["opening_balance_equity"] = val
        elif "retained earnings" in lbl:
            result["retained_earnings"] = val
        elif "undeposited funds" in lbl:
            result["undeposited_funds"] = val
    return result


def _parse_pl(pl_data: dict) -> dict:
    """Extract key figures from P&L report."""
    rows = _extract_report_rows(pl_data)
    result = {
        "total_income": 0.0, "total_cogs": 0.0, "gross_profit": 0.0,
        "total_expenses": 0.0, "net_income": 0.0,
        "uncategorized_income": 0.0, "uncategorized_expense": 0.0,
        "raw_rows": rows,
    }
    for r in rows:
        lbl = r["label"].lower()
        val = _safe_float(r["value"])
        if "total income" in lbl:
            result["total_income"] = val
        elif "total cogs" in lbl or "cost of goods" in lbl:
            result["total_cogs"] = val
        elif "gross profit" in lbl:
            result["gross_profit"] = val
        elif "total expenses" in lbl:
            result["total_expenses"] = val
        elif "net income" in lbl or "net loss" in lbl:
            result["net_income"] = val
        elif "uncategorized income" in lbl:
            result["uncategorized_income"] = val
        elif "uncategorized expense" in lbl:
            result["uncategorized_expense"] = val
    return result


def _parse_ar_aging(ar_data: dict) -> dict:
    """Extract AR aging summary."""
    rows = _extract_report_rows(ar_data)
    result = {"total_ar": 0.0, "current": 0.0, "overdue_30": 0.0,
              "overdue_60": 0.0, "overdue_90": 0.0, "overdue_90_plus": 0.0}
    for r in rows:
        lbl = r["label"].lower()
        val = _safe_float(r["value"])
        if "total" in lbl:
            result["total_ar"] = val
        elif "current" in lbl:
            result["current"] = val
        elif "1 - 30" in lbl:
            result["overdue_30"] = val
        elif "31 - 60" in lbl:
            result["overdue_60"] = val
        elif "61 - 90" in lbl:
            result["overdue_90"] = val
        elif "91" in lbl or "over 90" in lbl:
            result["overdue_90_plus"] = val
    return result


def _parse_ap_aging(ap_data: dict) -> dict:
    result = {"total_ap": 0.0, "current": 0.0, "overdue_30": 0.0,
              "overdue_60": 0.0, "overdue_90": 0.0, "overdue_90_plus": 0.0}
    rows = _extract_report_rows(ap_data)
    for r in rows:
        lbl = r["label"].lower()
        val = _safe_float(r["value"])
        if "total" in lbl:
            result["total_ap"] = val
        elif "current" in lbl:
            result["current"] = val
        elif "1 - 30" in lbl:
            result["overdue_30"] = val
        elif "31 - 60" in lbl:
            result["overdue_60"] = val
        elif "61 - 90" in lbl:
            result["overdue_90"] = val
        elif "91" in lbl or "over 90" in lbl:
            result["overdue_90_plus"] = val
    return result


# ─── Health Score Engine ─────────────────────────────────────

def calculate_health_score(profile: CompanyProfile, issues: list) -> dict:
    """
    Compute 0–100 Accounting Health Score for one company.
    Deduct points based on severity and category of issues.
    """
    scores = {k: v for k, v in HEALTH_WEIGHTS.items()}  # Start full
    deductions = {}

    severity_deductions = {"critical": 8, "high": 4, "medium": 2, "low": 0.5}

    category_map = {
        "bank_rec": "bank_reconciliation",
        "credit_card": "credit_card_reconciliation",
        "balance_sheet": "balance_sheet_integrity",
        "pl": "pl_integrity",
        "ar": "ar_status",
        "ap": "ap_status",
        "uncategorized": "uncategorized_transactions",
        "payroll": "payroll",
        "sales_tax": "sales_tax",
        "equity": "equity_review",
        "fixed_asset": "balance_sheet_integrity",
        "loan": "balance_sheet_integrity",
        "duplicate": "uncategorized_transactions",
        "revenue": "pl_integrity",
        "expenses": "pl_integrity",
    }

    for issue in issues:
        if issue.status in ("resolved", "dismissed"):
            continue
        cat = category_map.get(issue.category, "balance_sheet_integrity")
        deduct = severity_deductions.get(issue.severity, 1)
        deductions[cat] = deductions.get(cat, 0) + deduct

    for cat, deduct in deductions.items():
        if cat in scores:
            scores[cat] = max(0, scores[cat] - deduct)

    # Auto-detect additional issues from profile data
    if profile:
        bs = _parse_balance_sheet(profile.balance_sheet_data or {})
        pl = _parse_pl(profile.pl_data or {})

        # Penalize Opening Balance Equity
        if abs(bs.get("opening_balance_equity", 0)) > 0.01:
            scores["equity_review"] = max(0, scores["equity_review"] - 3)

        # Penalize uncategorized
        if pl.get("uncategorized_income", 0) > 0 or pl.get("uncategorized_expense", 0) > 0:
            scores["uncategorized_transactions"] = max(0, scores["uncategorized_transactions"] - 4)

        # Penalize Undeposited Funds if high
        if bs.get("undeposited_funds", 0) > 5000:
            scores["bank_reconciliation"] = max(0, scores["bank_reconciliation"] - 3)

        # Check balance sheet equation: Assets = Liabilities + Equity
        total_a = bs.get("total_assets", 0)
        total_l = bs.get("total_liabilities", 0)
        total_e = bs.get("total_equity", 0)
        if total_a and abs(total_a - (total_l + total_e)) > 1:
            scores["balance_sheet_integrity"] = max(0, scores["balance_sheet_integrity"] - 10)

    total = round(sum(scores.values()), 1)

    # Determine label
    label, color = "Unknown", "secondary"
    for (lo, hi), (lbl, clr) in HEALTH_LABELS.items():
        if lo <= total <= hi:
            label, color = lbl, clr
            break

    return {
        "score": total,
        "label": label,
        "color": color,
        "category_scores": scores,
        "computed_at": _now().isoformat(),
    }


def get_health_label(score: float) -> tuple:
    for (lo, hi), (lbl, clr) in HEALTH_LABELS.items():
        if lo <= score <= hi:
            return lbl, clr
    return "Unknown", "secondary"


# ─── Assessment Engine ────────────────────────────────────────

def run_accounting_assessment(db: Session, company: Company, profile: CompanyProfile) -> list:
    """
    Full QBO Accounting Assessment.
    Analyzes all available data and creates AccountingIssue records.
    Returns list of new issues found.
    """
    new_issues = []

    def add(category, severity, title, description, recommended_action,
            amount=None, financial_impact=None, account_name=None,
            risk=None, likely_cause=None, documentation_required=None,
            approval_required=False):
        iss = _create_issue(
            db,
            realm_id=company.realm_id,
            company_id=company.id,
            category=category,
            severity=severity,
            title=title,
            description=description,
            recommended_action=recommended_action,
            amount=amount,
            financial_impact=financial_impact,
            account_name=account_name,
            risk=risk or "",
            likely_cause=likely_cause or "",
            documentation_required=documentation_required or "",
            approval_required=approval_required,
        )
        new_issues.append(iss)

    if not profile:
        return new_issues

    # ── Balance Sheet Analysis ────────────────────────────────
    bs = _parse_balance_sheet(profile.balance_sheet_data or {})

    # Opening Balance Equity
    obe = bs.get("opening_balance_equity", 0)
    if abs(obe) > 0.01:
        add(
            "equity", "high",
            "Opening Balance Equity has a balance",
            f"Opening Balance Equity account shows ${abs(obe):,.2f}. "
            "This account should be zero — balances here indicate unresolved initial setup items.",
            "Review Opening Balance Equity transactions and reclassify to proper equity accounts. "
            "Requires owner review to determine appropriate equity accounts.",
            amount=obe, financial_impact=abs(obe),
            account_name="Opening Balance Equity",
            risk="Balance Sheet misstatement; owner equity incorrectly classified",
            likely_cause="QuickBooks initial setup — bank balances entered without matching equity accounts",
            documentation_required="List of all Opening Balance Equity transactions; Owner authorization for reclassification",
            approval_required=True,
        )

    # Undeposited Funds
    udf = bs.get("undeposited_funds", 0)
    if udf > 1000:
        add(
            "bank_rec", "medium",
            f"Undeposited Funds balance: ${udf:,.2f}",
            f"Undeposited Funds shows ${udf:,.2f}. Payments sitting here have not been matched to bank deposits.",
            "Review Undeposited Funds and match all items to actual bank deposits. "
            "Run bank register vs Undeposited Funds comparison.",
            amount=udf, financial_impact=udf,
            account_name="Undeposited Funds",
            risk="Cash overstated; AR/revenue double-counted",
            likely_cause="Payments recorded but bank deposit not created in QBO",
        )

    # Balance Sheet equation
    total_a = bs.get("total_assets", 0)
    total_l = bs.get("total_liabilities", 0)
    total_e = bs.get("total_equity", 0)
    if total_a and abs(total_a - (total_l + total_e)) > 1:
        diff = total_a - (total_l + total_e)
        add(
            "balance_sheet", "critical",
            "Balance Sheet does not balance",
            f"Assets (${total_a:,.2f}) ≠ Liabilities + Equity (${total_l + total_e:,.2f}). "
            f"Difference: ${diff:,.2f}. This indicates a data integrity issue.",
            "Run Trial Balance to identify the out-of-balance account. "
            "Review recent journal entries for debit/credit errors.",
            amount=diff, financial_impact=abs(diff),
            risk="Financial statements are materially misstated",
            likely_cause="Journal entry with unequal debits/credits; corrupted QBO data",
            approval_required=True,
        )

    # ── P&L Analysis ─────────────────────────────────────────
    pl = _parse_pl(profile.pl_data or {})

    if pl.get("uncategorized_income", 0) > 0:
        add(
            "uncategorized", "high",
            f"Uncategorized Income: ${pl['uncategorized_income']:,.2f}",
            "Revenue is recorded in 'Uncategorized Income' instead of a proper revenue account. "
            "This makes revenue reporting unreliable.",
            "Review each transaction in Uncategorized Income and reclassify to the correct revenue account.",
            amount=pl["uncategorized_income"], financial_impact=pl["uncategorized_income"],
            account_name="Uncategorized Income",
            risk="Revenue reporting inaccurate; tax returns may be affected",
            likely_cause="Transactions entered without selecting a proper income category",
            documentation_required="Invoice or source document for each transaction",
        )

    if pl.get("uncategorized_expense", 0) > 0:
        add(
            "uncategorized", "high",
            f"Uncategorized Expense: ${pl['uncategorized_expense']:,.2f}",
            "Expenses are recorded in 'Uncategorized Expense' instead of proper expense accounts.",
            "Review and reclassify all Uncategorized Expense transactions.",
            amount=pl["uncategorized_expense"], financial_impact=pl["uncategorized_expense"],
            account_name="Uncategorized Expense",
            risk="Expense reporting inaccurate; deductions may be under/over-stated",
            likely_cause="Imported bank transactions not categorized",
            documentation_required="Receipts or source documents for each expense",
        )

    # Net income sanity
    if pl.get("total_income", 0) > 0 and pl.get("net_income", 0) < (pl.get("total_income", 0) * -0.5):
        add(
            "pl", "medium",
            "Unusually high net loss relative to revenue",
            f"Net income (${pl['net_income']:,.2f}) is more than 50% below revenue. "
            "Review expenses for duplicates, misclassifications, or unusual items.",
            "Perform detailed expense review. Compare month-by-month to identify anomalies.",
            financial_impact=abs(pl.get("net_income", 0)),
            risk="Expenses may be overstated; profitability reporting inaccurate",
            likely_cause="Duplicate expenses, misclassified capital expenditures, or one-time items",
        )

    # ── AR Analysis ───────────────────────────────────────────
    ar = _parse_ar_aging(profile.ar_aging_data or {})
    total_ar = ar.get("total_ar", 0)
    overdue_90_plus = ar.get("overdue_90_plus", 0)

    if overdue_90_plus > 500:
        add(
            "ar", "high",
            f"AR over 90 days past due: ${overdue_90_plus:,.2f}",
            f"${overdue_90_plus:,.2f} of accounts receivable is more than 90 days past due. "
            "This represents significant collection risk and may require bad debt write-off.",
            "Contact customers with balances over 90 days. Consider allowance for doubtful accounts.",
            amount=overdue_90_plus, financial_impact=overdue_90_plus,
            account_name="Accounts Receivable",
            risk="Uncollectible accounts; AR overstated",
            likely_cause="Customer payment delays; missing payment follow-up",
            documentation_required="Customer aging detail; collection attempt records",
        )

    if total_ar > 0 and ar.get("overdue_60", 0) > (total_ar * 0.3):
        add(
            "ar", "medium",
            "High percentage of AR overdue 60+ days",
            f"{ar.get('overdue_60', 0) / total_ar * 100:.0f}% of AR is 60+ days overdue.",
            "Review collection procedures. Update customer payment terms if needed.",
            financial_impact=ar.get("overdue_60", 0),
            account_name="Accounts Receivable",
            risk="Collection risk; may need allowance for doubtful accounts",
        )

    # ── AP Analysis ───────────────────────────────────────────
    ap = _parse_ap_aging(profile.ap_aging_data or {})
    ap_overdue = ap.get("overdue_30", 0) + ap.get("overdue_60", 0) + ap.get("overdue_90", 0) + ap.get("overdue_90_plus", 0)

    if ap_overdue > 1000:
        add(
            "ap", "medium",
            f"Overdue vendor bills: ${ap_overdue:,.2f}",
            f"${ap_overdue:,.2f} in vendor bills are past due. "
            "Late payments may result in vendor penalties or service interruption.",
            "Review vendor aging. Pay critical vendors. Set up payment schedule.",
            amount=ap_overdue, financial_impact=ap_overdue,
            account_name="Accounts Payable",
            risk="Vendor relationship risk; potential late fees",
            likely_cause="Cash flow constraints or missing bill entry",
        )

    # ── Bank Account Analysis ─────────────────────────────────
    bank_accounts = profile.bank_accounts or []
    for bank in bank_accounts:
        bal = bank.get("balance", 0)
        if bal < 0:
            add(
                "bank_rec", "critical",
                f"Negative bank balance: {bank.get('name', 'Unknown')}",
                f"Bank account '{bank.get('name')}' shows negative balance of ${abs(bal):,.2f}. "
                "Negative bank balances indicate missing deposits or duplicate payments.",
                "Investigate outstanding deposits and review for duplicate payments.",
                amount=bal, financial_impact=abs(bal),
                account_name=bank.get("name"),
                risk="Financial statement misstatement; may indicate fraud or bookkeeping error",
                likely_cause="Missing bank deposits; duplicate payments recorded",
                approval_required=True,
            )

    # ── Credit Card Analysis ──────────────────────────────────
    credit_cards = profile.credit_cards or []
    for cc in credit_cards:
        bal = cc.get("balance", 0)
        if bal > 0:
            add(
                "credit_card", "low",
                f"Credit card shows debit balance: {cc.get('name', 'Unknown')}",
                f"Credit card '{cc.get('name')}' shows a debit balance of ${bal:,.2f}. "
                "Credit card liabilities should normally have credit (negative) balances.",
                "Review credit card transactions. Confirm if this represents an overpayment or misclassification.",
                amount=bal, financial_impact=bal,
                account_name=cc.get("name"),
                risk="Liability misstated; Balance Sheet misclassification",
                likely_cause="Payments exceeding charges; returns not recorded",
            )

    db.commit()
    return new_issues


# ─── Bank Reconciliation Analysis ────────────────────────────

def analyze_bank_reconciliation(profile: CompanyProfile, bank_account_name: str) -> dict:
    """
    Generate a reconciliation status report for one bank account.
    Returns structured reconciliation data.
    """
    bank_accounts = profile.bank_accounts or []
    account = next((b for b in bank_accounts if b.get("name") == bank_account_name), None)

    if not account:
        return {"error": f"Account '{bank_account_name}' not found in this company"}

    qbo_balance = account.get("balance", 0)

    return {
        "account_name": bank_account_name,
        "account_id": account.get("id"),
        "qbo_balance": qbo_balance,
        "bank_statement_balance": None,  # Requires user input
        "last_reconciled_date": account.get("last_reconciled"),
        "status": "Pending Statement",
        "items": [
            {
                "type": "info",
                "message": "Enter the bank statement ending balance to complete reconciliation.",
                "action": "provide_statement_balance",
            }
        ],
        "reconciliation_steps": [
            "1. Obtain bank statement ending balance",
            "2. Compare to QBO balance",
            "3. Identify outstanding deposits",
            "4. Identify outstanding checks",
            "5. Identify bank charges not in QBO",
            "6. Identify interest income not in QBO",
            "7. Calculate adjusted balance",
            "8. Verify adjusted balance matches bank statement",
        ],
        "data_as_of": profile.data_as_of.isoformat() if profile.data_as_of else None,
    }


# ─── Revenue Reconciliation ───────────────────────────────────

def analyze_revenue(profile: CompanyProfile) -> dict:
    """Controller-level revenue review."""
    pl = _parse_pl(profile.pl_data or {})
    bs = _parse_balance_sheet(profile.balance_sheet_data or {})

    issues = []
    if pl.get("uncategorized_income", 0) > 0:
        issues.append({
            "type": "uncategorized_income",
            "amount": pl["uncategorized_income"],
            "description": "Income recorded without proper revenue classification",
        })

    return {
        "total_revenue": pl.get("total_income", 0),
        "gross_profit": pl.get("gross_profit", 0),
        "gross_margin_pct": (
            pl.get("gross_profit", 0) / pl.get("total_income", 1) * 100
            if pl.get("total_income", 0) > 0 else 0
        ),
        "uncategorized_income": pl.get("uncategorized_income", 0),
        "net_income": pl.get("net_income", 0),
        "issues": issues,
        "merchant_processors": profile.merchant_processors or [],
        "note": (
            "IMPORTANT: If merchant processors (Stripe, Square, etc.) are used, "
            "reconcile gross processor revenue against net bank deposits. "
            "Never treat net deposits as gross revenue."
        ),
    }


# ─── Month-End Close ──────────────────────────────────────────

def create_month_end_close(db: Session, company: Company, period: str) -> MonthEndClose:
    """Initialize a new month-end close checklist for one company."""
    # Check if one already exists for this period + realm_id
    existing = db.query(MonthEndClose).filter_by(
        realm_id=company.realm_id, period=period
    ).first()
    if existing:
        return existing

    checklist = [
        {**step, "status": "pending", "notes": "", "completed_at": None, "completed_by": None}
        for step in MONTH_END_STEPS
    ]

    close = MonthEndClose(
        company_id=company.id,
        realm_id=company.realm_id,
        period=period,
        status="open",
        checklist=checklist,
        started_at=_now(),
    )
    db.add(close)
    db.commit()
    return close


def update_close_step(db: Session, close: MonthEndClose, step_num: int,
                      status: str, notes: str = "", completed_by: str = "Controller") -> MonthEndClose:
    """Mark a month-end close step as complete or in_progress."""
    checklist = close.checklist or []
    for step in checklist:
        if step["step_num"] == step_num:
            step["status"] = status
            step["notes"] = notes
            if status == "complete":
                step["completed_at"] = _now().isoformat()
                step["completed_by"] = completed_by
    close.checklist = checklist

    # Check if all steps are complete
    if all(s["status"] == "complete" for s in checklist):
        close.status = "closed"
        close.completed_at = _now()
        close.closed_by = completed_by

    elif any(s["status"] in ("complete", "in_progress") for s in checklist):
        close.status = "in_progress"

    db.commit()
    return close


# ─── Audit Readiness Report ───────────────────────────────────

def generate_audit_readiness(db: Session, company: Company, profile: CompanyProfile) -> dict:
    """Generate a comprehensive audit readiness assessment."""
    issues = db.query(AccountingIssue).filter_by(
        realm_id=company.realm_id, status="open"
    ).all()

    critical = [i for i in issues if i.severity == "critical"]
    high = [i for i in issues if i.severity == "high"]
    medium = [i for i in issues if i.severity == "medium"]

    # Score audit readiness
    deductions = len(critical) * 15 + len(high) * 8 + len(medium) * 3
    audit_score = max(0, 100 - deductions)

    if audit_score >= 85:
        audit_status, audit_color = "Ready for Audit", "success"
    elif audit_score >= 70:
        audit_status, audit_color = "Minor Preparation Needed", "warning"
    elif audit_score >= 50:
        audit_status, audit_color = "Significant Preparation Required", "orange"
    else:
        audit_status, audit_color = "Not Audit Ready", "danger"

    areas = []
    categories = {
        "bank_rec": "Bank Reconciliations",
        "credit_card": "Credit Card Reconciliations",
        "ar": "Accounts Receivable",
        "ap": "Accounts Payable",
        "payroll": "Payroll",
        "sales_tax": "Sales Tax",
        "equity": "Equity",
        "fixed_asset": "Fixed Assets",
        "loan": "Loans",
        "uncategorized": "Uncategorized Transactions",
        "balance_sheet": "Balance Sheet",
        "pl": "Profit & Loss",
    }

    for cat, label in categories.items():
        cat_issues = [i for i in issues if i.category == cat]
        if cat_issues:
            max_sev = max(cat_issues, key=lambda x: SEVERITY.get(x.severity, 0))
            areas.append({
                "area": label,
                "status": "Needs Work",
                "severity": max_sev.severity,
                "issue_count": len(cat_issues),
            })
        else:
            areas.append({"area": label, "status": "Clear", "severity": "none", "issue_count": 0})

    return {
        "audit_score": audit_score,
        "audit_status": audit_status,
        "audit_color": audit_color,
        "total_issues": len(issues),
        "critical_count": len(critical),
        "high_count": len(high),
        "medium_count": len(medium),
        "areas": areas,
        "data_as_of": profile.data_as_of.isoformat() if profile and profile.data_as_of else None,
        "company_name": company.company_name,
        "realm_id": company.realm_id,
    }


# ─── Controller Portfolio Summary ────────────────────────────

def get_portfolio_summary(db: Session, companies: list) -> list:
    """Build summary row for each company in the Controller Portfolio."""
    rows = []
    for company in companies:
        profile = db.query(CompanyProfile).filter_by(realm_id=company.realm_id).first()
        issues = db.query(AccountingIssue).filter_by(
            realm_id=company.realm_id, status="open"
        ).all()

        open_issues = len(issues)
        critical = sum(1 for i in issues if i.severity == "critical")
        high = sum(1 for i in issues if i.severity == "high")

        health = profile.health_score if profile and profile.health_score else 0
        health_label, health_color = get_health_label(health)

        month_closes = db.query(MonthEndClose).filter_by(
            realm_id=company.realm_id
        ).order_by(MonthEndClose.period.desc()).first()

        rows.append({
            "id": company.id,
            "realm_id": company.realm_id,
            "company_name": company.company_name,
            "connection_status": company.connection_status,
            "last_sync": company.last_sync.strftime("%Y-%m-%d %H:%M") if company.last_sync else "Never",
            "health_score": health,
            "health_label": health_label,
            "health_color": health_color,
            "open_issues": open_issues,
            "critical_issues": critical,
            "high_issues": high,
            "month_end_status": month_closes.status if month_closes else "Not Started",
            "month_end_period": month_closes.period if month_closes else None,
            "environment": company.qbo_environment,
        })
    return rows
