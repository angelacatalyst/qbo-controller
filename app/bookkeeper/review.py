"""Review modules that create WorkItems and never post to QBO."""
from datetime import datetime, date

from app.accounting import _parse_ap_aging, _parse_ar_aging, _parse_balance_sheet, _parse_pl
from app.bookkeeper.freshness import live_read
from app.bookkeeper.tools import QboTools
from app.bookkeeper.write_guard import WriteRefused
from app.database import WorkItem
from app.qbo_client import classify_qbo_error

_PAYROLL_HINTS = (
    "payroll",
    "wages payable",
    "federal tax",
    "state withholding",
    "unemployment",
    "fica",
    "payroll liability",
)


def _module(name: str, source: dict) -> dict:
    return {
        "module": name,
        "status": "NOT_STARTED",
        "message": "",
        "work_items_created": 0,
        "findings": [],
        "errors": [],
        "source": source.get("source"),
        "freshness": source.get("freshness"),
        "freshness_label": source.get("label"),
    }


def _open_keys(db, realm_id: str, work_type: str) -> set[str]:
    rows = (
        db.query(WorkItem.qbo_txn_id)
        .filter(
            WorkItem.realm_id == realm_id,
            WorkItem.work_type == work_type,
            WorkItem.status.notin_(["rejected", "verified", "failed"]),
        )
        .all()
    )
    return {row[0] for row in rows if row[0]}


def _add_review(
    db,
    company,
    *,
    work_type: str,
    txn_id: str,
    txn_type: str,
    amount,
    payee: str,
    reason: str,
    current_name: str = "",
    txn_date=None,
) -> bool:
    if txn_id in _open_keys(db, company.realm_id, work_type):
        return False
    db.add(WorkItem(
        company_id=company.id,
        realm_id=company.realm_id,
        work_type=work_type,
        qbo_txn_id=txn_id,
        qbo_txn_type=txn_type,
        txn_date=txn_date,
        amount=amount,
        payee_name=payee or None,
        current_account_name=current_name or None,
        confidence="none",
        risk="medium",
        autonomy_level=3,
        reason=reason,
        status="pending",
    ))
    return True


def review_credit_cards(db, company, profile) -> dict:
    """Book balances only. QBO has no statement-reconciliation resource."""
    result = _module("CREDIT_CARDS", {
        "source": "CACHED_SYNC",
        "freshness": "CACHED",
        "label": "Credit card book balances come from the chart stored at last sync.",
    })
    cards = list((profile.credit_cards if profile else None) or [])
    created = 0
    for card in cards:
        balance = float(card.get("balance") or 0)
        name = card.get("name") or "Credit card"
        card_id = str(card.get("id") or name)
        reason = (
            f"{name} book balance is ${balance:,.2f}. "
            "QuickBooks Online does not expose a reconciliation statement through the Accounting API. "
            "A person needs the statement ending balance before this account can be reconciled. "
            "HUMAN_REQUIRED."
        )
        result["findings"].append({"account": name, "book_balance": balance, "status": "HUMAN_REQUIRED"})
        if _add_review(
            db, company,
            work_type="CC_RECONCILE",
            txn_id=f"cc:{card_id}",
            txn_type="Account",
            amount=balance,
            payee=name,
            current_name=name,
            reason=reason,
        ):
            created += 1
    result["work_items_created"] = created
    result["status"] = "COMPLETED"
    result["message"] = (
        f"{len(cards)} credit card account(s) on the chart. "
        "Statement matching is HUMAN_REQUIRED because QBO does not provide the statement."
        if cards else
        "No credit card accounts on the synced chart."
    )
    return result


def review_bank_reconcile(db, company, profile) -> dict:
    result = _module("RECONCILE", {
        "source": "CACHED_SYNC",
        "freshness": "CACHED",
        "label": "Bank book balances come from the chart stored at last sync. Not a live reconciliation.",
    })
    banks = list((profile.bank_accounts if profile else None) or [])
    created = 0
    for bank in banks:
        balance = float(bank.get("balance") or 0)
        name = bank.get("name") or "Bank"
        bank_id = str(bank.get("id") or name)
        reason = (
            f"{name} book balance is ${balance:,.2f}. "
            "No statement ending balance is on file, so the difference cannot be computed. "
            "QBO does not expose bank reconciliation state on the Accounting API. HUMAN_REQUIRED."
        )
        result["findings"].append({
            "account": name,
            "book_balance": balance,
            "statement_ending_balance": None,
            "difference": None,
            "status": "HUMAN_REQUIRED",
        })
        if _add_review(
            db, company,
            work_type="BANK_RECONCILE",
            txn_id=f"bank:{bank_id}",
            txn_type="Account",
            amount=balance,
            payee=name,
            current_name=name,
            reason=reason,
        ):
            created += 1
    result["work_items_created"] = created
    result["status"] = "COMPLETED"
    result["message"] = (
        f"{len(banks)} bank account(s). Statement ending balance is required before a difference can be explained."
        if banks else
        "No bank accounts on the synced chart."
    )
    return result


def review_ap(db, company) -> dict:
    result = _module("AP", live_read("Bill"))
    tools = QboTools(company)
    today = date.today()
    try:
        bills, complete, _total = tools.query_entity(db, "Bill", "Balance > '0'")
    except Exception as exc:
        kind, detail = classify_qbo_error(exc, entity="Bill")
        result["status"] = "FAILED"
        result["errors"].append({"error_class": kind, "detail": detail})
        result["message"] = detail
        return result
    if not complete:
        result["errors"].append({
            "error_class": "incomplete",
            "detail": "Open bill query stopped before the last page.",
        })

    seen_docs: dict[tuple, list] = {}
    created = 0
    overdue = 0
    for bill in bills:
        bill_id = str(bill.get("Id") or "")
        if not bill_id:
            continue
        vendor = (bill.get("VendorRef") or {}).get("name") or ""
        vendor_id = (bill.get("VendorRef") or {}).get("value") or ""
        balance = float(bill.get("Balance") or 0)
        total = float(bill.get("TotalAmt") or 0)
        due_raw = bill.get("DueDate") or ""
        doc = (bill.get("DocNumber") or "").strip()
        due = None
        try:
            due = datetime.strptime(due_raw[:10], "%Y-%m-%d").date() if due_raw else None
        except ValueError:
            due = None
        is_overdue = bool(due and due < today and balance > 0)
        if is_overdue:
            overdue += 1
        key = (vendor_id, doc or f"{total:.2f}|{bill.get('TxnDate')}")
        seen_docs.setdefault(key, []).append(bill_id)
        if not is_overdue:
            continue
        reason = (
            f"Bill {doc or bill_id} for {vendor or 'a vendor'} "
            f"balance ${balance:,.2f} was due {due.isoformat()}. HUMAN_REQUIRED to pay or schedule."
        )
        txn_date = None
        if bill.get("TxnDate"):
            try:
                txn_date = datetime.strptime(bill["TxnDate"][:10], "%Y-%m-%d")
            except ValueError:
                txn_date = None
        if _add_review(
            db, company,
            work_type="AP_REVIEW",
            txn_id=bill_id,
            txn_type="Bill",
            amount=balance,
            payee=vendor,
            reason=reason,
            txn_date=txn_date,
        ):
            created += 1

    duplicate_groups = [ids for ids in seen_docs.values() if len(ids) > 1]
    for ids in duplicate_groups:
        reason = (
            "Possible duplicate bills: same vendor and document or amount/date "
            f"({', '.join(ids)}). HUMAN_REQUIRED before any payment."
        )
        if _add_review(
            db, company,
            work_type="AP_DUPLICATE",
            txn_id=ids[0],
            txn_type="Bill",
            amount=None,
            payee="",
            reason=reason,
        ):
            created += 1

    result["work_items_created"] = created
    result["findings"].append({
        "open_bills": len(bills),
        "overdue": overdue,
        "duplicate_groups": len(duplicate_groups),
        "complete": complete,
    })
    result["status"] = "COMPLETED" if complete else "COMPLETED_WITH_WARNINGS"
    result["message"] = (
        f"Live QBO: {len(bills)} open bill(s), {overdue} overdue, "
        f"{len(duplicate_groups)} possible duplicate group(s)."
    )
    return result


def review_payroll(db, company, profile) -> dict:
    """Report book balances. The payroll-provider total is not on the Accounting API."""
    result = _module("PAYROLL", {
        "source": "CACHED_SYNC",
        "freshness": "CACHED",
        "label": (
            "Payroll liabilities are book balances from the last chart sync. "
            "The payroll provider total is not on the Accounting API."
        ),
    })
    created = 0
    matches = []
    accounts = list(profile.chart_of_accounts or []) if profile else []
    for account in accounts:
        name = (account.get("name") or "").lower()
        if not any(hint in name for hint in _PAYROLL_HINTS):
            continue
        balance = float(account.get("balance") or 0)
        matches.append(account)
        if abs(balance) < 0.005:
            continue
        reason = (
            f"QBO payroll-related balance in {account.get('name')} is ${balance:,.2f}. "
            "The payroll-provider total is not available through the QBO Accounting API, "
            "so the difference cannot be computed from here. Status: NEEDS REVIEW. HUMAN_REQUIRED."
        )
        result["findings"].append({
            "account": account.get("name"),
            "qbo_balance": balance,
            "provider_total": None,
            "difference": None,
            "status": "HUMAN_REQUIRED",
        })
        if _add_review(
            db, company,
            work_type="PAYROLL_REVIEW",
            txn_id=f"payroll:{account.get('id')}",
            txn_type="Account",
            amount=balance,
            payee=account.get("name") or "",
            current_name=account.get("name") or "",
            reason=reason,
        ):
            created += 1
    result["work_items_created"] = created
    result["status"] = "COMPLETED"
    result["message"] = (
        f"{len(matches)} payroll-related account(s) on the chart. "
        "Provider-to-QBO difference is HUMAN_REQUIRED."
        if matches else
        "No payroll-named accounts on the synced chart. Confirm the payroll system on the company profile."
    )
    return result


def review_sales_tax(db, company, profile) -> dict:
    result = _module("SALES_TAX", {
        "source": "CACHED_SYNC",
        "freshness": "CACHED",
        "label": "Sales tax balances come from the last chart sync. Filing is not performed by the agent.",
    })
    created = 0
    found = False
    accounts = list(profile.chart_of_accounts or []) if profile else []
    for account in accounts:
        name = (account.get("name") or "").lower()
        subtype = account.get("subtype") or ""
        if "sales tax" not in name and subtype not in ("SalesTaxPayable", "GlobalTaxPayable"):
            continue
        found = True
        balance = float(account.get("balance") or 0)
        reason = (
            f"Sales tax account {account.get('name')} book balance is ${balance:,.2f}. "
            "The agent can prepare this figure. Filing and the return are HUMAN_REQUIRED."
        )
        result["findings"].append({
            "account": account.get("name"),
            "balance": balance,
            "status": "HUMAN_REQUIRED",
        })
        if abs(balance) < 0.005:
            continue
        if _add_review(
            db, company,
            work_type="SALES_TAX_REVIEW",
            txn_id=f"tax:{account.get('id')}",
            txn_type="Account",
            amount=balance,
            payee=account.get("name") or "",
            current_name=account.get("name") or "",
            reason=reason,
        ):
            created += 1
    result["work_items_created"] = created
    result["status"] = "COMPLETED"
    result["message"] = (
        "Sales tax accounts found. Filing remains HUMAN_REQUIRED."
        if found else
        "No sales tax payable account on the synced chart."
    )
    return result


def review_statements(db, company, profile) -> dict:
    result = _module("STATEMENTS", {
        "source": "CACHED_SYNC",
        "freshness": "CACHED",
        "label": "Financial statements are the last sync snapshot, not a live report pull.",
    })
    if not profile or not profile.data_as_of:
        result["status"] = "FAILED"
        result["message"] = "No snapshot. Sync this company before reading statements."
        result["freshness"] = "MISSING"
        return result
    parsed_bs = _parse_balance_sheet(profile.balance_sheet_data or {})
    parsed_pl = _parse_pl(profile.pl_data or {})
    parsed_ar = _parse_ar_aging(profile.ar_aging_data or {})
    parsed_ap = _parse_ap_aging(profile.ap_aging_data or {})
    assets = parsed_bs["total_assets"]
    liabilities = parsed_bs["total_liabilities"]
    equity = parsed_bs["total_equity"]
    equation_gap = round(assets - (liabilities + equity), 2)
    created = 0

    def flag(key: str, severity: str, reason: str, amount):
        nonlocal created
        result["findings"].append({
            "key": key,
            "severity": severity,
            "detail": reason,
            "source": "CACHED_SYNC",
        })
        if _add_review(
            db, company,
            work_type="STATEMENT_REVIEW",
            txn_id=f"stmt:{key}",
            txn_type="Report",
            amount=amount,
            payee="",
            reason=f"{severity}: {reason} Source: cached snapshot, not live QBO.",
        ):
            created += 1

    if abs(equation_gap) > 1:
        flag(
            "balance_sheet_equation",
            "CRITICAL",
            (
                f"Balance sheet does not balance. Assets ${assets:,.2f} versus "
                f"liabilities and equity ${liabilities + equity:,.2f}. Difference ${equation_gap:,.2f}."
            ),
            equation_gap,
        )
    if abs(parsed_bs["opening_balance_equity"]) > 1:
        flag(
            "opening_balance_equity",
            "HIGH",
            f"Opening Balance Equity is ${parsed_bs['opening_balance_equity']:,.2f}.",
            parsed_bs["opening_balance_equity"],
        )
    if abs(parsed_bs["undeposited_funds"]) > 1:
        flag(
            "undeposited_funds",
            "MEDIUM",
            f"Undeposited Funds is ${parsed_bs['undeposited_funds']:,.2f}.",
            parsed_bs["undeposited_funds"],
        )
    if abs(parsed_pl["uncategorized_expense"]) > 0 or abs(parsed_pl["uncategorized_income"]) > 0:
        flag(
            "uncategorized_pl",
            "HIGH",
            (
                f"P&L uncategorized income ${parsed_pl['uncategorized_income']:,.2f}, "
                f"uncategorized expense ${parsed_pl['uncategorized_expense']:,.2f}."
            ),
            parsed_pl["uncategorized_expense"] + parsed_pl["uncategorized_income"],
        )
    if parsed_ar.get("overdue_90_plus", 0) > 0:
        flag(
            "ar_over_90",
            "HIGH",
            f"AR over 90 days is ${parsed_ar['overdue_90_plus']:,.2f}.",
            parsed_ar["overdue_90_plus"],
        )
    if parsed_ap.get("overdue_90_plus", 0) > 0:
        flag(
            "ap_over_90",
            "MEDIUM",
            f"AP over 90 days is ${parsed_ap['overdue_90_plus']:,.2f}.",
            parsed_ap["overdue_90_plus"],
        )

    result["work_items_created"] = created
    result["status"] = "COMPLETED"
    result["message"] = (
        f"Cached statements. Net income ${parsed_pl['net_income']:,.2f}. "
        f"{len(result['findings'])} item(s) to review."
    )
    result["totals"] = {
        "total_assets": assets,
        "total_liabilities": liabilities,
        "total_equity": equity,
        "equation_gap": equation_gap,
        "net_income": parsed_pl["net_income"],
    }
    return result


def safe_review(fn, *args) -> dict:
    try:
        return fn(*args)
    except WriteRefused as exc:
        return {
            "module": getattr(fn, "__name__", "review"),
            "status": "FAILED",
            "message": str(exc),
            "work_items_created": 0,
            "findings": [],
            "errors": [{"error_class": "write_refused", "detail": str(exc)}],
            "source": "NONE",
            "freshness": "MISSING",
        }
    except Exception as exc:
        kind, detail = classify_qbo_error(exc, entity=getattr(fn, "__name__", "review"))
        return {
            "module": getattr(fn, "__name__", "review"),
            "status": "FAILED",
            "message": detail,
            "work_items_created": 0,
            "findings": [],
            "errors": [{"error_class": kind, "detail": detail}],
            "source": "NONE",
            "freshness": "MISSING",
        }
