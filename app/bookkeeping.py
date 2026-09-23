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

from app.database import (
    Company, CompanyProfile,
    ProposedCategorization, ProposedARMatch,
    ProposedJournalEntry, ChangeLog, _now,
)
from app.qbo_client import QBOClient


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
