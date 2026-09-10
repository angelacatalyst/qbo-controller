"""
QuickBooks Online API Client
────────────────────────────
Handles OAuth 2.0, token refresh, and all QBO API calls.
Every call is scoped to a specific realm_id — never cross-company.
"""
import secrets
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Company, SyncHistory, _now
from app.security import decrypt_token, encrypt_token


# ─── OAuth Helpers ────────────────────────────────────────────

def build_auth_url(state: str) -> str:
    """Generate the Intuit OAuth 2.0 authorization URL."""
    params = {
        "client_id": settings.QBO_CLIENT_ID,
        "response_type": "code",
        "scope": settings.QBO_SCOPES,
        "redirect_uri": settings.QBO_REDIRECT_URI,
        "state": state,
    }
    return f"{settings.QBO_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_tokens(code: str, realm_id: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    resp = requests.post(
        settings.QBO_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.QBO_REDIRECT_URI,
        },
        auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_in": data.get("expires_in", 3600),
        "realm_id": realm_id,
    }


def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token using the refresh token."""
    resp = requests.post(
        settings.QBO_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in": data.get("expires_in", 3600),
    }


def revoke_token(token: str) -> bool:
    """Revoke a QBO token (disconnect)."""
    try:
        resp = requests.post(
            settings.QBO_REVOKE_URL,
            data={"token": token},
            auth=(settings.QBO_CLIENT_ID, settings.QBO_CLIENT_SECRET),
            headers={"Accept": "application/json"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ─── QBO API Client ───────────────────────────────────────────

class QBOClient:
    """
    Isolated QBO API client bound to a single realm_id.
    All requests are scoped to this company — no cross-company calls possible.
    """

    def __init__(self, company: Company):
        if not company.realm_id:
            raise ValueError("realm_id is required — cannot create an unscoped QBO client")
        self.realm_id = company.realm_id
        self.company_id = company.id
        self.environment = company.qbo_environment or settings.QBO_ENVIRONMENT
        self._company = company
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    @property
    def base_url(self) -> str:
        if self.environment == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"

    @property
    def company_url(self) -> str:
        return f"{self.base_url}/v3/company/{self.realm_id}"

    def _get_token(self, db: Session) -> str:
        """Get a valid access token, refreshing if needed."""
        company = db.query(Company).filter_by(realm_id=self.realm_id).first()
        if not company:
            raise ValueError(f"Company with realm_id {self.realm_id} not found")

        # Check if token needs refresh
        buffer = timedelta(seconds=settings.TOKEN_REFRESH_BUFFER)
        if company.token_expires_at and datetime.utcnow() >= (company.token_expires_at - buffer):
            refresh_tok = decrypt_token(company.refresh_token_enc)
            if not refresh_tok:
                raise ValueError("No refresh token available — reconnection required")
            tokens = refresh_access_token(refresh_tok)
            company.access_token_enc = encrypt_token(tokens["access_token"])
            company.refresh_token_enc = encrypt_token(tokens["refresh_token"])
            company.token_expires_at = datetime.utcnow() + timedelta(seconds=tokens["expires_in"])
            company.connection_status = "connected"
            db.commit()

        return decrypt_token(company.access_token_enc)

    def _get(self, db: Session, endpoint: str, params: dict = None) -> dict:
        """GET request to QBO API."""
        token = self._get_token(db)
        resp = self._session.get(
            f"{self.company_url}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, db: Session, endpoint: str, payload: dict) -> dict:
        """POST request to QBO API."""
        token = self._get_token(db)
        resp = self._session.post(
            f"{self.company_url}{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _query(self, db: Session, sql: str) -> list:
        """Run a QBO SQL-style query."""
        data = self._get(db, "/query", {"query": sql, "minorversion": "65"})
        qr = data.get("QueryResponse", {})
        # Return whatever entity list is in the response
        for key, val in qr.items():
            if isinstance(val, list):
                return val
        return []

    def _report(self, db: Session, report_name: str, params: dict = None) -> dict:
        """Fetch a QBO financial report."""
        token = self._get_token(db)
        resp = self._session.get(
            f"{self.company_url}/reports/{report_name}",
            headers={"Authorization": f"Bearer {token}"},
            params={**(params or {}), "minorversion": "65"},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Company Info ──────────────────────────────────────────

    def get_company_info(self, db: Session) -> dict:
        return self._get(db, "/companyinfo/" + self.realm_id)

    # ── Chart of Accounts ─────────────────────────────────────

    def get_accounts(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM Account MAXRESULTS 1000")

    def get_accounts_by_type(self, db: Session, account_type: str) -> list:
        return self._query(db, f"SELECT * FROM Account WHERE AccountType='{account_type}' MAXRESULTS 500")

    # ── Customers & Vendors ───────────────────────────────────

    def get_customers(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM Customer WHERE Active=true MAXRESULTS 1000")

    def get_vendors(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM Vendor WHERE Active=true MAXRESULTS 1000")

    # ── Transactions ──────────────────────────────────────────

    def get_invoices(self, db: Session, start_date: str = None, end_date: str = None) -> list:
        sql = "SELECT * FROM Invoice"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
            if end_date:
                sql += f" AND TxnDate <= '{end_date}'"
        sql += " MAXRESULTS 1000"
        return self._query(db, sql)

    def get_open_invoices(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM Invoice WHERE Balance > '0' MAXRESULTS 1000")

    def get_bills(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM Bill"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        sql += " MAXRESULTS 1000"
        return self._query(db, sql)

    def get_open_bills(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM Bill WHERE Balance > '0' MAXRESULTS 1000")

    def get_payments(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM Payment"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        return self._query(db, sql + " MAXRESULTS 1000")

    def get_expenses(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM Purchase"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        return self._query(db, sql + " MAXRESULTS 1000")

    def get_journal_entries(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM JournalEntry"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        return self._query(db, sql + " MAXRESULTS 500")

    def get_deposits(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM Deposit"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        return self._query(db, sql + " MAXRESULTS 1000")

    def get_transfers(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM Transfer MAXRESULTS 500")

    def get_checks(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM Check"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        return self._query(db, sql + " MAXRESULTS 1000")

    def get_credit_card_credits(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM CreditCardCredit MAXRESULTS 500")

    def get_sales_receipts(self, db: Session, start_date: str = None) -> list:
        sql = "SELECT * FROM SalesReceipt"
        if start_date:
            sql += f" WHERE TxnDate >= '{start_date}'"
        return self._query(db, sql + " MAXRESULTS 1000")

    def get_refund_receipts(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM RefundReceipt MAXRESULTS 500")

    def get_vendor_credits(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM VendorCredit MAXRESULTS 500")

    def get_tax_codes(self, db: Session) -> list:
        return self._query(db, "SELECT * FROM TaxCode MAXRESULTS 200")

    def get_payroll_items(self, db: Session) -> list:
        try:
            return self._query(db, "SELECT * FROM PayrollItem MAXRESULTS 200")
        except Exception:
            return []

    # ── Financial Reports ─────────────────────────────────────

    def get_balance_sheet(self, db: Session, start_date: str = None, end_date: str = None, accounting_method: str = "Accrual") -> dict:
        params = {"accounting_method": accounting_method, "minorversion": "65"}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return self._report(db, "BalanceSheet", params)

    def get_profit_and_loss(self, db: Session, start_date: str, end_date: str, accounting_method: str = "Accrual") -> dict:
        return self._report(db, "ProfitAndLoss", {
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": accounting_method,
            "minorversion": "65",
        })

    def get_trial_balance(self, db: Session, start_date: str, end_date: str) -> dict:
        return self._report(db, "TrialBalance", {
            "start_date": start_date,
            "end_date": end_date,
            "minorversion": "65",
        })

    def get_general_ledger(self, db: Session, start_date: str, end_date: str) -> dict:
        return self._report(db, "GeneralLedger", {
            "start_date": start_date,
            "end_date": end_date,
            "columns": "tx_date,txn_type,doc_num,name,memo,account_name,amount,balance",
            "minorversion": "65",
        })

    def get_ar_aging(self, db: Session) -> dict:
        return self._report(db, "AgedReceivables", {"minorversion": "65"})

    def get_ap_aging(self, db: Session) -> dict:
        return self._report(db, "AgedPayables", {"minorversion": "65"})

    def get_cash_flow(self, db: Session, start_date: str, end_date: str) -> dict:
        return self._report(db, "CashFlow", {
            "start_date": start_date,
            "end_date": end_date,
            "minorversion": "65",
        })

    def get_transaction_list(self, db: Session, start_date: str, end_date: str) -> dict:
        return self._report(db, "TransactionList", {
            "start_date": start_date,
            "end_date": end_date,
            "minorversion": "65",
        })

    def get_account_list(self, db: Session) -> dict:
        return self._report(db, "AccountList", {"minorversion": "65"})

    # ── Write Operations (require approval) ───────────────────

    def create_journal_entry(self, db: Session, je_data: dict) -> dict:
        """Create a journal entry in QBO. Requires prior approval."""
        return self._post(db, "/journalentry", je_data)

    def get_transaction(self, db: Session, txn_type: str, txn_id: str) -> dict:
        """Fetch a specific transaction to verify it was created."""
        return self._get(db, f"/{txn_type.lower()}/{txn_id}")


# ─── Sync Engine ──────────────────────────────────────────────

def sync_company_data(db: Session, company: Company, sync_type: str = "full") -> SyncHistory:
    """
    Perform a full or incremental data sync for one company.
    Returns a SyncHistory record.
    """
    sync = SyncHistory(
        company_id=company.id,
        realm_id=company.realm_id,
        sync_type=sync_type,
        status="running",
        started_at=_now(),
    )
    db.add(sync)
    db.commit()

    client = QBOClient(company)
    records = 0
    errors = []
    endpoints_done = []

    try:
        from app.database import CompanyProfile

        profile = db.query(CompanyProfile).filter_by(realm_id=company.realm_id).first()
        if not profile:
            profile = CompanyProfile(
                company_id=company.id,
                realm_id=company.realm_id,
            )
            db.add(profile)

        # 1. Company info
        try:
            info = client.get_company_info(db)
            ci = info.get("CompanyInfo", {})
            company.qbo_company_name = ci.get("CompanyName", company.company_name)
            company.qbo_country = ci.get("Country", "US")
            fys = ci.get("FiscalYearStartMonth")
            if fys:
                # QBO returns month as a name ("January") or integer — normalize to int
                _month_map = {
                    "January": 1, "February": 2, "March": 3, "April": 4,
                    "May": 5, "June": 6, "July": 7, "August": 8,
                    "September": 9, "October": 10, "November": 11, "December": 12,
                }
                fys_int = _month_map.get(fys, fys) if isinstance(fys, str) else fys
                try:
                    fys_int = int(fys_int)
                except (ValueError, TypeError):
                    fys_int = 1  # default to January if unparseable
                company.qbo_fiscal_year_start_month = fys_int
                profile.fiscal_year_start_month = fys_int
            endpoints_done.append("company_info")
        except Exception as e:
            errors.append({"endpoint": "company_info", "error": str(e)})

        # 2. Chart of Accounts
        try:
            accounts = client.get_accounts(db)
            profile.chart_of_accounts = [
                {
                    "id": a.get("Id"),
                    "name": a.get("Name"),
                    "type": a.get("AccountType"),
                    "subtype": a.get("AccountSubType"),
                    "balance": a.get("CurrentBalance", 0),
                    "active": a.get("Active", True),
                    "classification": a.get("Classification"),
                }
                for a in accounts
            ]
            records += len(accounts)

            # Identify bank, credit card, loan accounts
            profile.bank_accounts = [
                {"id": a["id"], "name": a["name"], "balance": a["balance"]}
                for a in profile.chart_of_accounts
                if a["type"] == "Bank"
            ]
            profile.credit_cards = [
                {"id": a["id"], "name": a["name"], "balance": a["balance"]}
                for a in profile.chart_of_accounts
                if a["type"] == "Credit Card"
            ]
            profile.loans = [
                {"id": a["id"], "name": a["name"], "balance": a["balance"]}
                for a in profile.chart_of_accounts
                if a["subtype"] in ("LinesOfCredit", "LoanPayable", "OtherCurrentLiabilities", "NotesPayable")
            ]
            endpoints_done.append("accounts")
        except Exception as e:
            errors.append({"endpoint": "accounts", "error": str(e)})

        # 3. Financial Reports
        from datetime import date
        today = date.today()
        start_of_year = f"{today.year}-01-01"
        today_str = today.strftime("%Y-%m-%d")

        try:
            profile.balance_sheet_data = client.get_balance_sheet(db, end_date=today_str)
            records += 1
            endpoints_done.append("balance_sheet")
        except Exception as e:
            errors.append({"endpoint": "balance_sheet", "error": str(e)})

        try:
            profile.pl_data = client.get_profit_and_loss(db, start_of_year, today_str)
            records += 1
            endpoints_done.append("profit_and_loss")
        except Exception as e:
            errors.append({"endpoint": "profit_and_loss", "error": str(e)})

        try:
            profile.trial_balance_data = client.get_trial_balance(db, start_of_year, today_str)
            records += 1
            endpoints_done.append("trial_balance")
        except Exception as e:
            errors.append({"endpoint": "trial_balance", "error": str(e)})

        try:
            profile.ar_aging_data = client.get_ar_aging(db)
            records += 1
            endpoints_done.append("ar_aging")
        except Exception as e:
            errors.append({"endpoint": "ar_aging", "error": str(e)})

        try:
            profile.ap_aging_data = client.get_ap_aging(db)
            records += 1
            endpoints_done.append("ap_aging")
        except Exception as e:
            errors.append({"endpoint": "ap_aging", "error": str(e)})

        profile.data_as_of = _now()

        # Update company status
        company.last_sync = _now()
        company.last_sync_status = "success" if not errors else "partial"
        company.connection_status = "connected"

        sync.status = "success" if not errors else "partial"
        sync.records_retrieved = records
        sync.endpoints_synced = endpoints_done
        sync.errors = errors if errors else None
        sync.api_calls_made = len(endpoints_done)
        sync.completed_at = _now()

        db.commit()

    except Exception as e:
        # Must rollback the failed transaction before we can write anything new
        try:
            db.rollback()
        except Exception:
            pass
        sync.status = "failed"
        sync.errors = [{"fatal": str(e)}]
        sync.completed_at = _now()
        company.last_sync = _now()
        company.last_sync_status = "failed"
        company.last_sync_error = str(e)
        try:
            db.commit()
        except Exception:
            pass  # Best-effort — don't let logging failure mask the real error

    return sync
