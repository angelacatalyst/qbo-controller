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

# "Not assessed" sentinel — returned any time real data is absent.
# NEVER return score=100 / "Excellent" without actual assessment data.
_NOT_ASSESSED = {
    "score": None,
    "label": "NOT YET ASSESSED",
    "sublabel": "Sync QBO data, then run Assessment",
    "color": "secondary",
    "category_scores": {},
    "assessed": False,
    "computed_at": None,
}


def calculate_health_score(profile: CompanyProfile, issues: list) -> dict:
    """
    Compute 0–100 Accounting Health Score for one company.

    *** MANDATORY DATA GATE ***
    A numeric score is NEVER returned unless:
      1. A CompanyProfile exists, AND
      2. profile.data_as_of is set (meaning QBO data has actually been synced).

    Without real data this function returns the _NOT_ASSESSED sentinel
    (score=None, assessed=False).  The UI must display "NOT YET ASSESSED"
    and never show 100 / Excellent for a company whose books have not been
    inspected.
    """
    # ── Gate 1: profile must exist ────────────────────────────
    if profile is None:
        return {**_NOT_ASSESSED, "reason": "No company profile found."}

    # ── Gate 2: QBO data must have been synced ────────────────
    if not profile.data_as_of:
        return {
            **_NOT_ASSESSED,
            "reason": (
                "QBO accounting data has not been synchronized. "
                "Run a full sync first, then run Assessment."
            ),
        }

    # ── Real score: deduct from 100 based on actual findings ──
    scores = {k: v for k, v in HEALTH_WEIGHTS.items()}
    deductions: dict = {}

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

    # Additional deductions derived directly from synced report data
    bs = _parse_balance_sheet(profile.balance_sheet_data or {})
    pl = _parse_pl(profile.pl_data or {})

    if abs(bs.get("opening_balance_equity", 0)) > 0.01:
        scores["equity_review"] = max(0, scores["equity_review"] - 3)

    if pl.get("uncategorized_income", 0) > 0 or pl.get("uncategorized_expense", 0) > 0:
        scores["uncategorized_transactions"] = max(0, scores["uncategorized_transactions"] - 4)

    if bs.get("undeposited_funds", 0) > 5000:
        scores["bank_reconciliation"] = max(0, scores["bank_reconciliation"] - 3)

    total_a = bs.get("total_assets", 0)
    total_l = bs.get("total_liabilities", 0)
    total_e = bs.get("total_equity", 0)
    if total_a and abs(total_a - (total_l + total_e)) > 1:
        scores["balance_sheet_integrity"] = max(0, scores["balance_sheet_integrity"] - 10)

    total = round(sum(scores.values()), 1)

    label, color = "Unknown", "secondary"
    for (lo, hi), (lbl, clr) in HEALTH_LABELS.items():
        if lo <= total <= hi:
            label, color = lbl, clr
            break

    return {
        "score": total,
        "label": label,
        "sublabel": f"Based on {len(issues)} open issue(s)",
        "color": color,
        "category_scores": scores,
        "assessed": True,
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
    Full TPC QBO Accounting Assessment — implements all checks from the
    TPC QuickBooks Diagnostic Template (Banking, Undeposited Funds, P&L,
    Balance Sheet, AR, AP, Chart of Accounts, Payroll, Inventory, Sales Tax).

    Every finding is based on actual synced QBO data. No placeholder results.
    Returns list of new AccountingIssue records created.
    """
    new_issues = []

    # Count existing issues ONCE (before any new ones are added to the session).
    # autoflush=False means repeated count() queries won't see newly db.add()-ed
    # objects, so _make_issue_id would return ISS-0001 every time if called in a
    # loop → UNIQUE constraint violation on db.commit(). Use a local counter instead.
    _base_count = db.query(AccountingIssue).filter_by(realm_id=company.realm_id).count()

    def add(category, severity, title, description, recommended_action,
            amount=None, financial_impact=None, account_name=None,
            risk=None, likely_cause=None, documentation_required=None,
            approval_required=False):
        issue_id = f"ISS-{_base_count + len(new_issues) + 1:04d}"
        iss = AccountingIssue(
            issue_id=issue_id,
            company_id=company.id,
            realm_id=company.realm_id,
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
        db.add(iss)
        new_issues.append(iss)

    if not profile:
        return new_issues

    # ── Helpers ───────────────────────────────────────────────
    def find_row_val(rows, *keywords):
        """Return float value of first row whose label contains ALL keywords (case-insensitive), or None."""
        for r in rows:
            lbl = r.get("label", "").lower()
            if all(kw.lower() in lbl for kw in keywords):
                return _safe_float(r.get("value", 0))
        return None

    def rows_matching(rows, *keywords):
        """Return all rows whose label contains ALL keywords."""
        out = []
        for r in rows:
            lbl = r.get("label", "").lower()
            if all(kw.lower() in lbl for kw in keywords):
                out.append(r)
        return out

    def coa_accounts(*type_kws, name_kws=None, classification_kws=None):
        """Filter COA by type/classification/name keywords."""
        result = []
        for a in coa:
            t = a.get("type", "").lower()
            n = a.get("name", "").lower()
            cl = a.get("classification", "").lower()
            if type_kws and not any(kw.lower() in t for kw in type_kws):
                continue
            if name_kws and not any(kw.lower() in n for kw in name_kws):
                continue
            if classification_kws and not any(kw.lower() in cl for kw in classification_kws):
                continue
            result.append(a)
        return result

    # ── Parse all reports ─────────────────────────────────────
    bs  = _parse_balance_sheet(profile.balance_sheet_data or {})
    pl  = _parse_pl(profile.pl_data or {})
    ar  = _parse_ar_aging(profile.ar_aging_data or {})
    ap  = _parse_ap_aging(profile.ap_aging_data or {})
    bs_rows = bs.get("raw_rows", [])
    pl_rows = pl.get("raw_rows", [])

    coa           = profile.chart_of_accounts or []
    bank_accounts = profile.bank_accounts or []
    credit_cards  = profile.credit_cards or []
    cap_threshold = float(profile.capitalization_threshold or 2500.0)

    # ═══════════════════════════════════════════════════════════
    # SECTION A — BANKING
    # ═══════════════════════════════════════════════════════════

    for bank in bank_accounts:
        name = bank.get("name", "Unknown")
        bal  = _safe_float(bank.get("balance", 0))

        # Negative bank balance
        if bal < 0:
            add(
                "bank_rec", "critical",
                f"Negative bank balance: {name}",
                f"Bank account '{name}' shows a negative balance of ${abs(bal):,.2f}. "
                "Negative bank balances indicate missing deposits or duplicate payments recorded in QBO.",
                "Compare QBO register to bank statement immediately. "
                "Identify outstanding deposits or duplicate expense entries.",
                amount=bal, financial_impact=abs(bal),
                account_name=name,
                risk="Balance Sheet misstatement; potential fraud or bookkeeping error",
                likely_cause="Missing bank deposits entered in QBO; duplicate payments recorded",
                approval_required=True,
            )

        # Last reconciliation date
        last_rec = bank.get("last_reconciled")
        if last_rec:
            try:
                rec_date = (
                    datetime.fromisoformat(str(last_rec)).date()
                    if isinstance(last_rec, str)
                    else (last_rec.date() if hasattr(last_rec, "date") else last_rec)
                )
                days_since = (date.today() - rec_date).days
                if days_since > 45:
                    sev = "high" if days_since > 90 else "medium"
                    add(
                        "bank_rec", sev,
                        f"Bank account not reconciled in {days_since} days: {name}",
                        f"'{name}' was last reconciled on {rec_date} ({days_since} days ago). "
                        "Monthly reconciliation is required for accurate books.",
                        "Reconcile this account immediately using the QBO Reconciliation tool. "
                        "Obtain bank statements for all unreconciled months.",
                        account_name=name,
                        risk="Undetected bank errors; missing transactions; fraud risk",
                        likely_cause="Reconciliation skipped; bank feed not regularly reviewed",
                        documentation_required=f"Bank statements for past {days_since // 30 + 1} month(s)",
                    )
            except Exception:
                pass
        else:
            add(
                "bank_rec", "high",
                f"Bank account has never been reconciled: {name}",
                f"No reconciliation history found for '{name}'. "
                "Monthly bank reconciliation is required for accurate financial statements.",
                "Perform initial bank reconciliation. Obtain bank statements from account opening or prior year-end.",
                account_name=name,
                risk="Books may not match bank; undetected errors or missing transactions",
                likely_cause="Reconciliation never set up; new account added without first reconciliation",
                documentation_required="Bank statements for full history or from fiscal year start",
            )

    # Credit card debit (positive) balances
    for cc in credit_cards:
        name = cc.get("name", "Unknown")
        bal  = _safe_float(cc.get("balance", 0))
        if bal > 0:
            add(
                "credit_card", "medium",
                f"Credit card shows debit (positive) balance: {name} (${bal:,.2f})",
                f"Credit card '{name}' shows a debit balance of ${bal:,.2f}. "
                "Credit card liability accounts should carry a credit (negative) balance. "
                "A positive balance typically means payments exceed charges, or entries are misclassified.",
                "Review credit card transactions. Confirm whether this is an overpayment "
                "or a misclassification of charges.",
                amount=bal, financial_impact=bal,
                account_name=name,
                risk="Liability misstated; Balance Sheet misclassification",
                likely_cause="Payments exceeding charges; returns or refunds not recorded against charges",
            )

    # ═══════════════════════════════════════════════════════════
    # SECTION B — UNDEPOSITED FUNDS
    # ═══════════════════════════════════════════════════════════

    udf = bs.get("undeposited_funds", 0)
    if udf > 1000:
        add(
            "bank_rec", "medium",
            f"Undeposited Funds balance: ${udf:,.2f}",
            f"Undeposited Funds shows ${udf:,.2f}. Payments sitting here have not been "
            "matched to bank deposits. Items older than 30 days indicate reconciliation gaps.",
            "Review Undeposited Funds detail. Match each payment to an actual bank deposit. "
            "Clear any items older than 30 days immediately.",
            amount=udf, financial_impact=udf,
            account_name="Undeposited Funds",
            risk="Cash overstated; risk of double-counting revenue; AR may be inflated",
            likely_cause="Payments recorded but bank deposit not created in QBO; "
                         "bank feed transactions imported instead of matched",
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION C — BALANCE SHEET INTEGRITY
    # ═══════════════════════════════════════════════════════════

    total_a = bs.get("total_assets", 0)
    total_l = bs.get("total_liabilities", 0)
    total_e = bs.get("total_equity", 0)
    if total_a and abs(total_a - (total_l + total_e)) > 1:
        diff = total_a - (total_l + total_e)
        add(
            "balance_sheet", "critical",
            "Balance Sheet does not balance",
            f"Assets (${total_a:,.2f}) ≠ Liabilities + Equity (${total_l + total_e:,.2f}). "
            f"Difference: ${diff:,.2f}.",
            "Run the Trial Balance report to locate the out-of-balance account. "
            "Review recent journal entries for unequal debits/credits.",
            amount=diff, financial_impact=abs(diff),
            risk="Financial statements materially misstated; audit failure",
            likely_cause="Journal entry with unequal debits/credits; data integrity issue in QBO",
            approval_required=True,
        )

    # ── BS Assets ─────────────────────────────────────────────

    # AR with credit (negative) balance on Balance Sheet
    ar_bs_bal = bs.get("accounts_receivable", 0)
    if ar_bs_bal < -0.01:
        add(
            "ar", "high",
            f"Accounts Receivable has a credit (negative) balance: ${ar_bs_bal:,.2f}",
            f"AR on the Balance Sheet shows ${ar_bs_bal:,.2f} — a credit balance. "
            "AR should always be a debit (positive) asset. This indicates overpayments "
            "from customers or misapplied credits.",
            "Run the AR Aging Detail report. Identify customers with credit balances. "
            "Apply credits to open invoices or issue refunds.",
            amount=ar_bs_bal, financial_impact=abs(ar_bs_bal),
            account_name="Accounts Receivable",
            risk="Assets understated; revenue may be overstated; customer balances incorrect",
            likely_cause="Customer overpayments; credit memos not matched to invoices",
            documentation_required="Customer-level AR detail showing credit balances",
        )

    # Uncategorized Asset balance
    unc_asset = find_row_val(bs_rows, "uncategorized asset")
    if unc_asset is not None and abs(unc_asset) > 0.01:
        add(
            "balance_sheet", "medium",
            f"Uncategorized Asset account has a balance: ${unc_asset:,.2f}",
            f"'Uncategorized Asset' shows ${unc_asset:,.2f}. Assets must be classified "
            "in specific accounts; placeholder accounts must be cleared.",
            "Review all transactions in Uncategorized Asset and reclassify each to "
            "the correct asset account (e.g., Prepaid Expenses, Other Current Assets).",
            amount=unc_asset, financial_impact=abs(unc_asset),
            account_name="Uncategorized Asset",
            risk="Balance Sheet classification incorrect; assets improperly reported",
            likely_cause="QBO auto-generated placeholder; imported transactions not categorized",
        )

    # Credit Card Receivables balance (unreconciled merchant processor funds)
    ccr_val = find_row_val(bs_rows, "credit card receivable")
    if ccr_val is None:
        ccr_val = find_row_val(bs_rows, "credit card receivables")
    if ccr_val is not None and abs(ccr_val) > 0.01:
        add(
            "balance_sheet", "medium",
            f"Credit Card Receivables balance: ${ccr_val:,.2f}",
            f"Credit Card Receivables shows ${ccr_val:,.2f}. This typically means "
            "merchant processor settlements (Stripe, Square, PayPal, etc.) have not been "
            "fully reconciled and matched to bank deposits.",
            "Reconcile Credit Card Receivables to merchant processor statements. "
            "Match each settlement to the corresponding bank deposit.",
            amount=ccr_val, financial_impact=abs(ccr_val),
            account_name="Credit Card Receivables",
            risk="Cash and receivables overstated; merchant reconciliation incomplete",
            likely_cause="Processor settlements recorded to this account instead of being cleared "
                         "when matched to bank deposit",
        )

    # Fixed assets under capitalization threshold
    fa_accounts = [a for a in coa if a.get("type", "").lower() == "fixed asset"
                   and "depreciation" not in a.get("name", "").lower()
                   and "accumulated" not in a.get("name", "").lower()]
    for fa in fa_accounts:
        fa_bal = _safe_float(fa.get("balance", 0))
        if 0 < fa_bal < cap_threshold:
            add(
                "fixed_asset", "medium",
                f"Fixed asset may be below capitalization threshold: "
                f"{fa.get('name')} (${fa_bal:,.2f})",
                f"Fixed asset '{fa.get('name')}' has a balance of ${fa_bal:,.2f}, "
                f"below the capitalization threshold of ${cap_threshold:,.2f}. "
                "Items below the threshold should generally be expensed, not capitalized.",
                f"Review '{fa.get('name')}'. If the cost is below ${cap_threshold:,.2f}, "
                "expense it in the period purchased and remove from fixed assets.",
                amount=fa_bal, financial_impact=fa_bal,
                account_name=fa.get("name"),
                risk="Fixed assets overstated; period expenses understated",
                likely_cause="No formal capitalization policy; small purchases capitalized by mistake",
                documentation_required=f"Invoice for asset; owner capitalization policy confirmation",
            )

    # Accumulated depreciation missing when fixed assets exist
    has_fixed_assets = any(
        a.get("type", "").lower() == "fixed asset"
        and _safe_float(a.get("balance", 0)) > 0
        and "depreciation" not in a.get("name", "").lower()
        for a in coa
    )
    has_accum_dep = any(
        "accumulated depreciation" in a.get("name", "").lower()
        or a.get("subtype", "").lower() == "accumulated depreciation"
        for a in coa
    )
    if has_fixed_assets and not has_accum_dep:
        add(
            "fixed_asset", "high",
            "Fixed assets present but no accumulated depreciation account found",
            "The chart of accounts has fixed asset accounts with balances, but no "
            "Accumulated Depreciation account exists. Depreciation must be recorded "
            "to properly reflect asset net book values on the Balance Sheet.",
            "Create Accumulated Depreciation accounts for each fixed asset class. "
            "Record depreciation based on useful life. Engage a CPA to determine correct "
            "depreciation method and catch-up entries.",
            risk="Fixed assets overstated; depreciation expense not recognized",
            likely_cause="Depreciation never set up in QBO; no depreciation schedule maintained",
            documentation_required="Fixed asset list with purchase dates, costs, and useful lives; "
                                   "prior depreciation schedules if available",
            approval_required=True,
        )

    # ── BS Liabilities ─────────────────────────────────────────

    # AP with debit (positive) balance on Balance Sheet
    ap_bs_bal = bs.get("accounts_payable", 0)
    if ap_bs_bal > 0.01:
        add(
            "ap", "high",
            f"Accounts Payable has a debit (positive) balance: ${ap_bs_bal:,.2f}",
            f"AP on the Balance Sheet shows ${ap_bs_bal:,.2f} — a debit balance. "
            "AP should always be a credit (liability) balance. This indicates vendor "
            "overpayments or duplicate payments.",
            "Run the AP Aging Detail report. Identify vendors with debit balances. "
            "Request refunds or apply as credits against future bills.",
            amount=ap_bs_bal, financial_impact=abs(ap_bs_bal),
            account_name="Accounts Payable",
            risk="Liabilities understated; vendor balances incorrect",
            likely_cause="Duplicate vendor payments; bill credits not applied",
            documentation_required="Vendor-level AP detail showing debit balances",
        )

    # Payroll liabilities not clearing
    payroll_liab = [a for a in coa if
                    "payroll" in a.get("name", "").lower() and
                    a.get("type", "").lower() in ("other current liability", "current liability",
                                                   "long-term liability")]
    for pla in payroll_liab:
        pla_bal = _safe_float(pla.get("balance", 0))
        if abs(pla_bal) > 500:
            add(
                "payroll", "high",
                f"Payroll liability not clearing: {pla.get('name')} (${pla_bal:,.2f})",
                f"Payroll liability '{pla.get('name')}' carries a balance of ${pla_bal:,.2f}. "
                "Payroll liabilities should clear each period when taxes are remitted.",
                "Review payroll liability account detail. Confirm all payroll tax deposits "
                "have been made on time. Run Payroll Tax Liability report in QBO.",
                amount=pla_bal, financial_impact=abs(pla_bal),
                account_name=pla.get("name"),
                risk="Payroll taxes may be underpaid; IRS/state penalties possible",
                likely_cause="Payroll taxes paid outside QBO without matching journal entry; "
                             "manual payroll entries not clearing liabilities",
                documentation_required="Payroll tax deposit confirmations; 941 and state payroll filings",
                approval_required=True,
            )

    # Sales tax payable balance — check it's clearing and not growing unexpectedly
    st_liab = [a for a in coa if
               "sales tax" in a.get("name", "").lower() and
               a.get("type", "").lower() in ("other current liability", "current liability")]
    for sta in st_liab:
        sta_bal = _safe_float(sta.get("balance", 0))
        if abs(sta_bal) > 500:
            add(
                "sales_tax", "medium",
                f"Sales Tax Payable balance to verify: {sta.get('name')} (${sta_bal:,.2f})",
                f"Sales Tax Payable '{sta.get('name')}' shows ${sta_bal:,.2f}. "
                "Confirm this matches the actual amount owed to the tax authority "
                "and that it is being remitted on time.",
                "Run Sales Tax Liability report. Compare to actual filed returns and payments. "
                "Ensure all jurisdictions are reconciled and payments posted correctly.",
                amount=sta_bal, financial_impact=abs(sta_bal),
                account_name=sta.get("name"),
                risk="Sales tax liability misstated; compliance and penalty risk",
                likely_cause="Sales tax not remitted; timing difference between collection and payment",
                documentation_required="Sales tax returns filed; payment confirmations by jurisdiction",
            )

    # ── BS Equity ──────────────────────────────────────────────

    # Opening Balance Equity
    obe = bs.get("opening_balance_equity", 0)
    if abs(obe) > 0.01:
        add(
            "equity", "high",
            f"Opening Balance Equity has a balance: ${abs(obe):,.2f}",
            f"Opening Balance Equity shows ${abs(obe):,.2f}. This account should always be "
            "zero — a balance means initial setup entries were not properly reclassified.",
            "Review every transaction in Opening Balance Equity. Reclassify to appropriate "
            "equity accounts (Owner's Equity, Retained Earnings, or Paid-in Capital). "
            "Requires owner review to confirm correct equity accounts.",
            amount=obe, financial_impact=abs(obe),
            account_name="Opening Balance Equity",
            risk="Owner equity incorrectly classified; Balance Sheet misstatement",
            likely_cause="QBO initial setup — bank/credit card balances entered without "
                         "matching equity accounts",
            documentation_required="List of all Opening Balance Equity transactions; "
                                   "owner authorization for reclassification",
            approval_required=True,
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION D — P&L: INCOME
    # ═══════════════════════════════════════════════════════════

    # Uncategorized Income
    if pl.get("uncategorized_income", 0) > 0:
        amt = pl["uncategorized_income"]
        add(
            "uncategorized", "high",
            f"Uncategorized Income: ${amt:,.2f}",
            f"${amt:,.2f} in revenue is recorded in 'Uncategorized Income' instead of "
            "a proper revenue account. This makes revenue reporting unreliable.",
            "Review each transaction in Uncategorized Income and reclassify to the "
            "correct revenue account.",
            amount=amt, financial_impact=amt,
            account_name="Uncategorized Income",
            risk="Revenue reporting inaccurate; tax returns may be misstated",
            likely_cause="Transactions entered without selecting a proper income category",
            documentation_required="Invoice or source document for each transaction",
        )

    # Negative income account balances (debit balance on income account)
    income_coa = [a for a in coa if
                  a.get("type", "").lower() in ("income", "other income") or
                  a.get("classification", "").lower() == "revenue"]
    for inc in income_coa:
        inc_bal = _safe_float(inc.get("balance", 0))
        if inc_bal < -100:
            add(
                "revenue", "medium",
                f"Income account has negative balance: {inc.get('name')} (${inc_bal:,.2f})",
                f"'{inc.get('name')}' is an income account but shows a negative (debit) balance "
                f"of ${inc_bal:,.2f}. Income accounts should carry credit (positive) balances.",
                f"Review transactions in '{inc.get('name')}'. Identify refunds, credits, or "
                "misclassified expenses causing the negative balance.",
                amount=inc_bal, financial_impact=abs(inc_bal),
                account_name=inc.get("name"),
                risk="Revenue understated; returns or refunds may be improperly classified",
                likely_cause="Refunds or credits posted directly to income; reversed entries",
            )

    # Customer deposits recorded as income (should be liability)
    for kw in ["customer deposit", "advance payment", "deferred revenue"]:
        dep_val = find_row_val(pl_rows, kw)
        if dep_val is not None and dep_val > 0:
            add(
                "revenue", "medium",
                f"Possible unearned customer deposits recorded as income: ${dep_val:,.2f}",
                f"An account containing '{kw}' appears on the P&L with ${dep_val:,.2f}. "
                "Customer deposits/prepayments are liabilities until the service or product "
                "is delivered — they should not appear as income.",
                "Move unearned customer deposits to a liability account "
                "(Customer Deposits or Deferred Revenue). Recognize as income only when earned.",
                amount=dep_val, financial_impact=dep_val,
                account_name=kw.title(),
                risk="Revenue overstated; deferred revenue not properly recorded as liability",
                likely_cause="Customer prepayments recorded directly to income",
            )
            break

    # Loan proceeds appearing as income
    for kw in ["loan proceeds", "ppp loan", "sba loan", "eidl"]:
        loan_inc = find_row_val(pl_rows, kw)
        if loan_inc is not None and abs(loan_inc) > 500:
            add(
                "revenue", "high",
                f"Loan proceeds may be recorded as income: ${loan_inc:,.2f}",
                f"An account containing '{kw}' appears on the P&L with ${loan_inc:,.2f}. "
                "Loan proceeds are liabilities, not income. Recording them as income "
                "overstates revenue and taxable income.",
                "Review this entry. Reclassify loan proceeds to a Notes Payable "
                "(liability) account. Consult your CPA for forgiven loan treatment.",
                amount=loan_inc, financial_impact=abs(loan_inc),
                account_name=kw.title(),
                risk="Revenue and taxable income overstated; loan liability missing from Balance Sheet",
                likely_cause="Loan funds deposited to bank and categorized as income",
                approval_required=True,
            )
            break

    # Unapplied Cash Payment Income (cash-basis artifact)
    ucpi = find_row_val(pl_rows, "unapplied cash payment income")
    if ucpi is not None and abs(ucpi) > 100:
        add(
            "ar", "medium",
            f"Unapplied Cash Payment Income: ${ucpi:,.2f}",
            f"'Unapplied Cash Payment Income' shows ${ucpi:,.2f}. "
            "QBO generates this account when customer payments are received but not applied "
            "to specific invoices (cash-basis reporting). It masks unmatched cash receipts.",
            "Open each unapplied customer payment and apply it to the correct open invoice. "
            "This account should net to zero when all payments are properly applied.",
            amount=ucpi, financial_impact=abs(ucpi),
            account_name="Unapplied Cash Payment Income",
            risk="Revenue misstated; customer accounts show incorrect open balances",
            likely_cause="Customer payments received without selecting the corresponding invoice",
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION E — P&L: COGS
    # ═══════════════════════════════════════════════════════════

    # Negative COGS balances
    cogs_coa = [a for a in coa if
                a.get("type", "").lower() in ("cost of goods sold", "cogs") or
                "cost of goods" in a.get("name", "").lower() or
                "cost of sales" in a.get("name", "").lower()]
    for cogs in cogs_coa:
        cogs_bal = _safe_float(cogs.get("balance", 0))
        if cogs_bal < -100:
            add(
                "pl", "medium",
                f"COGS account has negative balance: {cogs.get('name')} (${cogs_bal:,.2f})",
                f"COGS account '{cogs.get('name')}' shows a negative balance of ${cogs_bal:,.2f}. "
                "COGS should carry a debit (positive) balance.",
                f"Review '{cogs.get('name')}' transactions. Identify vendor credits, returns, "
                "or reversed entries causing the negative balance.",
                amount=cogs_bal, financial_impact=abs(cogs_bal),
                account_name=cogs.get("name"),
                risk="Gross profit overstated; COGS misclassified",
                likely_cause="Vendor credit or return posted directly to COGS; reversal entries",
            )

    # COGS unusually high (>90% of revenue)
    total_income = pl.get("total_income", 0)
    total_cogs   = pl.get("total_cogs", 0)
    if total_income > 0 and total_cogs > 0:
        cogs_pct = total_cogs / total_income
        if cogs_pct > 0.90:
            add(
                "pl", "medium",
                f"COGS unusually high: {cogs_pct * 100:.0f}% of revenue",
                f"Cost of Goods Sold (${total_cogs:,.2f}) is {cogs_pct * 100:.0f}% of revenue "
                f"(${total_income:,.2f}). This leaves very little gross margin and may indicate "
                "misclassified operating expenses in COGS.",
                "Review COGS accounts in detail. Confirm all items are true product/service costs. "
                "Move overhead or operating expenses to the appropriate expense categories.",
                financial_impact=total_cogs,
                risk="Gross profit understated; COGS may contain operating expenses",
                likely_cause="Operating expenses miscategorized as COGS; pricing issues",
            )

    # ═══════════════════════════════════════════════════════════
    # SECTION F — P&L: EXPENSES
    # ═══════════════════════════════════════════════════════════

    # Uncategorized Expense
    if pl.get("uncategorized_expense", 0) > 0:
        amt = pl["uncategorized_expense"]
        add(
            "uncategorized", "high",
            f"Uncategorized Expense: ${amt:,.2f}",
            f"${amt:,.2f} in expenses are in 'Uncategorized Expense' instead of proper accounts.",
            "Review and reclassify all Uncategorized Expense transactions to the correct accounts.",
            amount=amt, financial_impact=amt,
            account_name="Uncategorized Expense",
            risk="Expense reporting inaccurate; deductions may be under/over-stated",
            likely_cause="Bank feed transactions imported but not categorized",
            documentation_required="Receipts or source documents for each expense",
        )

    # Ask My Accountant balance
    ama_val = find_row_val(pl_rows, "ask my accountant")
    if ama_val is None:
        ama_val = find_row_val(bs_rows, "ask my accountant")
    if ama_val is None:
        ama_coa = next((a for a in coa if "ask my accountant" in a.get("name", "").lower()), None)
        if ama_coa:
            ama_val = _safe_float(ama_coa.get("balance", 0))
    if ama_val is not None and abs(ama_val) > 0.01:
        add(
            "uncategorized", "high",
            f"'Ask My Accountant' account has a balance: ${ama_val:,.2f}",
            f"'Ask My Accountant' shows ${ama_val:,.2f}. This is a placeholder account — "
            "every transaction here needs to be properly classified before financials are reliable.",
            "Review every transaction in 'Ask My Accountant' and reclassify each to the "
            "correct income or expense account. This account must reach zero.",
            amount=ama_val, financial_impact=abs(ama_val),
            account_name="Ask My Accountant",
            risk="Financial statements unreliable until all items are classified",
            likely_cause="Client flagged transactions for accountant review; imported transactions not categorized",
            documentation_required="Source documents (receipts, invoices) for each transaction",
        )

    # Reconciliation Discrepancy balance
    rd_val = find_row_val(pl_rows, "reconciliation discrepancy")
    if rd_val is None:
        rd_val = find_row_val(bs_rows, "reconciliation discrepancy")
    if rd_val is None:
        rd_coa = next((a for a in coa if "reconciliation discrepancy" in a.get("name", "").lower()), None)
        if rd_coa:
            rd_val = _safe_float(rd_coa.get("balance", 0))
    if rd_val is not None and abs(rd_val) > 0.01:
        add(
            "bank_rec", "critical",
            f"Reconciliation Discrepancy account has a balance: ${rd_val:,.2f}",
            f"'Reconciliation Discrepancy' shows ${rd_val:,.2f}. QBO auto-posts to this "
            "account to force a reconciliation to close — it means the books and bank "
            "statement do not truly agree.",
            "Investigate the source of each discrepancy. Review prior reconciliations. "
            "Do NOT allow future reconciliations to post to this account. "
            "Reopen affected reconciliations and correct the underlying entries.",
            amount=rd_val, financial_impact=abs(rd_val),
            account_name="Reconciliation Discrepancy",
            risk="Bank reconciliation is invalid; books do not match bank statement",
            likely_cause="Reconciliation completed incorrectly; transactions deleted after reconciling",
            documentation_required="Bank statements for affected periods; reconciliation reports",
            approval_required=True,
        )

    # Negative expense account balances (significant)
    skip_exp_names = {
        "ask my accountant", "reconciliation discrepancy",
        "uncategorized", "depreciation",
    }
    exp_coa = [a for a in coa if
               a.get("type", "").lower() in ("expense", "other expense") or
               a.get("classification", "").lower() == "expense"]
    for exp in exp_coa:
        exp_name = exp.get("name", "")
        if any(s in exp_name.lower() for s in skip_exp_names):
            continue
        exp_bal = _safe_float(exp.get("balance", 0))
        if exp_bal < -500:
            add(
                "expenses", "low",
                f"Expense account has negative balance: {exp_name} (${exp_bal:,.2f})",
                f"Expense account '{exp_name}' shows a negative balance of ${exp_bal:,.2f}. "
                "While vendor refunds can cause this, significant negative balances "
                "warrant review for misclassification.",
                f"Review transactions in '{exp_name}'. Verify refunds or credits are "
                "correctly posted and the negative balance is legitimate.",
                amount=exp_bal, financial_impact=abs(exp_bal),
                account_name=exp_name,
                risk="Expenses understated; possible misclassification of income or refunds",
                likely_cause="Vendor refund posted to expense; reversing entries; income coded to expense",
            )

    # Loan payments recorded as expenses (only principal portion is wrong)
    loan_exp_accounts = [a for a in exp_coa if
                          "loan payment" in a.get("name", "").lower() and
                          _safe_float(a.get("balance", 0)) > 500]
    for le in loan_exp_accounts:
        le_bal = _safe_float(le.get("balance", 0))
        add(
            "loan", "high",
            f"Loan payments may be recorded as expenses: {le.get('name')} (${le_bal:,.2f})",
            f"'{le.get('name')}' appears in expenses with ${le_bal:,.2f}. "
            "Only loan interest is tax-deductible — principal payments reduce the "
            "loan liability and must NOT be recorded as an expense.",
            "Review all loan payment entries. Separate principal (to Notes Payable) "
            "from interest (to Interest Expense). Obtain loan statements showing the split.",
            amount=le_bal, financial_impact=le_bal,
            account_name=le.get("name"),
            risk="Expenses overstated; loan liability understated; incorrect tax deductions",
            likely_cause="Loan payments categorized as expense rather than split between "
                         "liability principal and interest expense",
            documentation_required="Loan amortization schedule; bank loan statements",
            approval_required=True,
        )

    # Fixed assets potentially expensed above capitalization threshold
    cap_risk_exp = [a for a in exp_coa if
                    _safe_float(a.get("balance", 0)) > cap_threshold and
                    any(kw in a.get("name", "").lower() for kw in
                        ["equipment", "furniture", "vehicle", "computer", "machinery",
                         "improvement", "renovation", "remodel", "buildout", "leasehold"])]
    for cre in cap_risk_exp:
        cre_bal = _safe_float(cre.get("balance", 0))
        add(
            "fixed_asset", "medium",
            f"Large expense may need to be capitalized: {cre.get('name')} (${cre_bal:,.2f})",
            f"Expense account '{cre.get('name')}' has ${cre_bal:,.2f}, which exceeds the "
            f"capitalization threshold of ${cap_threshold:,.2f}. Individual items over "
            f"${cap_threshold:,.2f} must be capitalized as fixed assets and depreciated.",
            f"Review individual transactions in '{cre.get('name')}'. For any single item "
            f"costing more than ${cap_threshold:,.2f}, reclassify to a Fixed Asset account "
            "and set up depreciation.",
            amount=cre_bal, financial_impact=cre_bal,
            account_name=cre.get("name"),
            risk="Expenses overstated; fixed assets understated; depreciation not recorded",
            likely_cause="Capital expenditure expensed in full instead of capitalized",
            documentation_required=f"Invoices for individual items over ${cap_threshold:,.2f}",
        )

    # Unapplied Bill Payment Expense (cash-basis artifact)
    ubpe = find_row_val(pl_rows, "unapplied bill payment expense")
    if ubpe is not None and abs(ubpe) > 100:
        add(
            "ap", "medium",
            f"Unapplied Bill Payment Expense: ${ubpe:,.2f}",
            f"'Unapplied Bill Payment Expense' shows ${ubpe:,.2f}. "
            "QBO generates this account when vendor payments are made but not applied "
            "to specific bills (cash-basis reporting).",
            "Open each unapplied vendor payment and apply it to the correct open bill. "
            "This account should net to zero when all payments are properly applied.",
            amount=ubpe, financial_impact=abs(ubpe),
            account_name="Unapplied Bill Payment Expense",
            risk="Expenses misstated; vendor accounts show incorrect open balances",
            likely_cause="Vendor payments recorded without selecting the corresponding bill",
        )

    # Unusually high net loss (>50% of revenue)
    if total_income > 0 and pl.get("net_income", 0) < (total_income * -0.5):
        add(
            "pl", "medium",
            "Unusually high net loss relative to revenue",
            f"Net income (${pl['net_income']:,.2f}) is a loss exceeding 50% of revenue "
            f"(${total_income:,.2f}). Review expenses for duplicates, misclassifications, "
            "or unusual one-time items.",
            "Perform a detailed expense review. Compare month-by-month to identify anomalies. "
            "Check for capital expenditures miscoded as expenses.",
            financial_impact=abs(pl.get("net_income", 0)),
            risk="Profitability severely misstated; may indicate significant recording errors",
            likely_cause="Duplicate expenses; capital items expensed; one-time non-recurring items",
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION G — AR ANALYSIS
    # ═══════════════════════════════════════════════════════════

    total_ar       = ar.get("total_ar", 0)
    overdue_90_plus = ar.get("overdue_90_plus", 0)

    if overdue_90_plus > 500:
        add(
            "ar", "high",
            f"AR over 90 days past due: ${overdue_90_plus:,.2f}",
            f"${overdue_90_plus:,.2f} of accounts receivable is more than 90 days past due. "
            "This represents significant collection risk and may require bad debt write-off.",
            "Contact each customer with 90+ day balances. Send formal demand letters. "
            "Consider allowance for doubtful accounts or write-off after collection exhausted.",
            amount=overdue_90_plus, financial_impact=overdue_90_plus,
            account_name="Accounts Receivable",
            risk="Uncollectible accounts; AR overstated on Balance Sheet",
            likely_cause="Customer payment delays; no follow-up collection process",
            documentation_required="Customer aging detail; records of collection attempts",
        )

    if total_ar > 0 and ar.get("overdue_60", 0) > (total_ar * 0.30):
        add(
            "ar", "medium",
            f"High percentage of AR overdue 60+ days "
            f"({ar.get('overdue_60', 0) / total_ar * 100:.0f}%)",
            f"{ar.get('overdue_60', 0) / total_ar * 100:.0f}% of total AR is 60+ days overdue.",
            "Review collection procedures. Tighten credit terms for repeat late-paying customers.",
            financial_impact=ar.get("overdue_60", 0),
            account_name="Accounts Receivable",
            risk="Collection risk; may require bad debt allowance",
            likely_cause="Weak collection process; customers on extended informal terms",
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION H — AP ANALYSIS
    # ═══════════════════════════════════════════════════════════

    ap_overdue = (ap.get("overdue_30", 0) + ap.get("overdue_60", 0) +
                  ap.get("overdue_90", 0) + ap.get("overdue_90_plus", 0))
    if ap_overdue > 1000:
        add(
            "ap", "medium",
            f"Overdue vendor bills: ${ap_overdue:,.2f}",
            f"${ap_overdue:,.2f} in vendor bills are past due. "
            "Late payments may trigger late fees, service interruption, or vendor credit holds.",
            "Review vendor aging report. Prioritize critical vendors. "
            "Set up a payment schedule and communicate with affected vendors.",
            amount=ap_overdue, financial_impact=ap_overdue,
            account_name="Accounts Payable",
            risk="Vendor relationship damage; late fees; supply chain risk",
            likely_cause="Cash flow constraints; bills entered but payment not scheduled",
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION I — CHART OF ACCOUNTS: PROBLEM ACCOUNTS
    # ═══════════════════════════════════════════════════════════

    # Account type misuse: expense named like income or vice versa
    for acct in coa:
        acct_name  = acct.get("name", "")
        acct_class = acct.get("classification", "").lower()
        acct_bal   = _safe_float(acct.get("balance", 0))
        if abs(acct_bal) < 1:
            continue  # Only flag active accounts with balances
        if acct_class == "revenue" and "expense" in acct_name.lower():
            add(
                "uncategorized", "low",
                f"Account type mismatch — income account named like an expense: '{acct_name}'",
                f"'{acct_name}' is classified as an income account but its name suggests "
                "it may be an expense. This can cause reporting confusion.",
                f"Review '{acct_name}'. Correct its account type if it is actually an expense.",
                account_name=acct_name,
                risk="Incorrect financial statement classification",
                likely_cause="Account set up with incorrect type during initial QBO configuration",
            )
        elif acct_class == "expense" and any(kw in acct_name.lower() for kw in ["income", "revenue"]):
            add(
                "uncategorized", "low",
                f"Account type mismatch — expense account named like income: '{acct_name}'",
                f"'{acct_name}' is classified as an expense account but its name suggests "
                "income. This can cause misclassification and reporting errors.",
                f"Review '{acct_name}'. Correct its account type if it is actually income.",
                account_name=acct_name,
                risk="Incorrect financial statement classification",
                likely_cause="Account set up with incorrect type during initial QBO configuration",
            )

    # ═══════════════════════════════════════════════════════════
    # SECTION J — PAYROLL
    # ═══════════════════════════════════════════════════════════

    payroll_exp_accounts = [a for a in coa if
                             "payroll" in a.get("name", "").lower() and
                             a.get("type", "").lower() in ("expense", "other expense")]
    has_payroll_liab = any(
        "payroll" in a.get("name", "").lower() and
        a.get("type", "").lower() in ("other current liability", "current liability")
        for a in coa
    )
    if payroll_exp_accounts and not has_payroll_liab:
        add(
            "payroll", "high",
            "Payroll expenses present but no payroll liability accounts found",
            "Payroll expense accounts exist but no payroll liability accounts are set up. "
            "When payroll runs, QBO should create liabilities for withheld taxes and employer "
            "taxes payable — these must appear on the Balance Sheet until remitted.",
            "Set up payroll liability accounts for each payroll tax type (federal/state "
            "withholding, FICA, FUTA, SUTA). Ensure payroll entries create matching liabilities. "
            "Consider using QBO Payroll or an integrated payroll service.",
            risk="Payroll taxes may not be tracked or remitted; IRS penalties possible",
            likely_cause="Manual payroll journal entries made without liability accounts; "
                         "payroll processed outside QBO without integration",
            documentation_required="Payroll records; tax deposit confirmations; 941 filings",
            approval_required=True,
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION K — SALES TAX: EXPENSE MISCLASSIFICATION
    # ═══════════════════════════════════════════════════════════

    st_as_expense = [a for a in exp_coa if
                      "sales tax" in a.get("name", "").lower() and
                      _safe_float(a.get("balance", 0)) > 100]
    for ste in st_as_expense:
        ste_bal = _safe_float(ste.get("balance", 0))
        add(
            "sales_tax", "high",
            f"Sales tax recorded as expense: {ste.get('name')} (${ste_bal:,.2f})",
            f"Sales tax appears in expense account '{ste.get('name')}' with ${ste_bal:,.2f}. "
            "Sales tax collected from customers is a liability — it belongs in Sales Tax Payable "
            "until remitted to the state, not in expenses.",
            "Review sales tax entries. Reclassify collected sales tax to Sales Tax Payable "
            "(liability). Only sales tax paid on your own business purchases is an expense.",
            amount=ste_bal, financial_impact=abs(ste_bal),
            account_name=ste.get("name"),
            risk="Expenses overstated; sales tax liability understated; compliance risk",
            likely_cause="Sales tax remittance payments miscategorized as expense "
                         "instead of reducing the payable",
        )

    # ═══════════════════════════════════════════════════════════
    # SECTION L — INVENTORY
    # ═══════════════════════════════════════════════════════════

    inv_accounts = [a for a in coa if
                     "inventory" in a.get("name", "").lower() and
                     a.get("type", "").lower() in ("inventory", "other current asset",
                                                     "current asset")]
    for inv in inv_accounts:
        inv_bal = _safe_float(inv.get("balance", 0))
        if inv_bal < 0:
            add(
                "balance_sheet", "high",
                f"Negative inventory balance: {inv.get('name')} (${inv_bal:,.2f})",
                f"Inventory account '{inv.get('name')}' shows a negative balance of ${inv_bal:,.2f}. "
                "Inventory cannot be negative — this indicates sales recorded without matching receipts.",
                "Run a physical inventory count. Compare to QBO quantities. "
                "Identify items sold that were not properly received into QBO inventory.",
                amount=inv_bal, financial_impact=abs(inv_bal),
                account_name=inv.get("name"),
                risk="Balance Sheet misstated; COGS likely incorrect",
                likely_cause="Sales recorded before inventory received; quantity tracking errors",
                documentation_required="Physical inventory count; receiving records; "
                                       "inventory valuation report",
            )
        elif inv_bal > 0:
            inv_bs_row = find_row_val(bs_rows, "inventory")
            if inv_bs_row is not None and abs(inv_bs_row - inv_bal) > 100:
                add(
                    "balance_sheet", "medium",
                    f"Inventory balance discrepancy: COA ${inv_bal:,.2f} vs. "
                    f"Balance Sheet ${inv_bs_row:,.2f}",
                    f"The inventory balance in the Chart of Accounts (${inv_bal:,.2f}) "
                    f"differs from the Balance Sheet (${inv_bs_row:,.2f}) by "
                    f"${abs(inv_bs_row - inv_bal):,.2f}.",
                    "Run the Inventory Valuation Summary report. "
                    "Reconcile product quantities and costs. Check for multiple inventory accounts.",
                    financial_impact=abs(inv_bs_row - inv_bal),
                    account_name=inv.get("name"),
                    risk="Inventory and COGS may be misstated",
                    likely_cause="Inventory adjustments not properly recorded; "
                                 "multiple inventory accounts with different totals",
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
    """
    Generate audit readiness assessment.

    *** DATA GATE ***
    Returns "NOT YET ASSESSED" when QBO data has not been synced.
    A score of 100 / "Ready" is never displayed without real assessment data.
    """
    # Gate: no score without synced data
    if profile is None or not profile.data_as_of:
        return {
            "audit_score": None,
            "audit_status": "NOT YET ASSESSED",
            "audit_color": "secondary",
            "assessed": False,
            "reason": (
                "QBO accounting data has not been synchronized. "
                "Run a full sync and then an Assessment before evaluating audit readiness."
            ),
            "total_issues": 0,
            "critical_count": 0,
            "high_count": 0,
            "medium_count": 0,
            "areas": [],
            "data_as_of": None,
            "company_name": company.company_name,
            "realm_id": company.realm_id,
        }

    issues = db.query(AccountingIssue).filter_by(
        realm_id=company.realm_id, status="open"
    ).all()

    critical = [i for i in issues if i.severity == "critical"]
    high = [i for i in issues if i.severity == "high"]
    medium = [i for i in issues if i.severity == "medium"]

    # Score audit readiness — only from real assessment findings
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
        "assessed": True,
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

        # Determine assessed state: requires both a sync AND a completed assessment
        data_synced = bool(profile and profile.data_as_of)
        assessment_ran = bool(profile and profile.health_score_updated)
        assessed = data_synced and assessment_ran

        if assessed:
            health = profile.health_score  # may be 0 legitimately
            health_label, health_color = get_health_label(health if health is not None else 0)
        else:
            health = None
            health_label = "NOT YET ASSESSED"
            health_color = "secondary"

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
            "assessed": assessed,
            "open_issues": open_issues,
            "critical_issues": critical,
            "high_issues": high,
            "month_end_status": month_closes.status if month_closes else "Not Started",
            "month_end_period": month_closes.period if month_closes else None,
            "environment": company.qbo_environment,
        })
    return rows
