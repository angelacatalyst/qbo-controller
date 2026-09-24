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


def fetch_company_name(access_token: str, realm_id: str, environment: str = "sandbox") -> str | None:
    """
    Fetch the real QBO company name immediately after OAuth, before a Company
    object exists. Returns None on failure (non-fatal — display name used instead).
    No DB writes — READ ONLY.
    """
    try:
        base = (
            "https://quickbooks.api.intuit.com"
            if environment == "production"
            else "https://sandbox-quickbooks.api.intuit.com"
        )
        url = f"{base}/v3/company/{realm_id}/companyinfo/{realm_id}"
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("CompanyInfo", {}).get("CompanyName")
    except Exception:
        pass
    return None


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


def classify_qbo_error(exc: Exception, entity: str = "") -> tuple[str, str]:
    """Classify a QBO API exception from a failed entity query.

    Returns (error_class, detail_message) where error_class is one of:

        'entity_not_supported' — Intuit confirmed this entity is unavailable
                                 for this company's configuration (e.g. Check
                                 on a non-US company). Safe to skip + retry
                                 with a fallback entity.

        'auth_error'           — HTTP 401/403 or Intuit auth error code.
                                 Token may be expired; reconnect required.

        'rate_limit'           — HTTP 429 or Intuit throttling. Back off and
                                 retry later.

        'temp_error'           — HTTP 5xx. Intuit server error; retry later.

        'malformed_query'      — HTTP 400 with Intuit query-validation code
                                 (4001 / 4002). The SQL itself is wrong.

        'api_error'            — Catch-all: network error, ambiguous 400,
                                 unknown response. Requires investigation.

    IMPORTANT — conservative by design:
        A bare HTTP 400 is NOT classified as entity_not_supported. Intuit's
        response body must explicitly confirm the entity is unavailable.
        Anything ambiguous returns 'api_error' so it surfaces for review.

    Args:
        exc:    The exception raised during a QBO _query() / _get() call.
        entity: The entity name being queried (e.g. 'Check'), used in the
                returned detail message.

    Returns:
        (error_class, detail_message)
    """
    try:
        if not isinstance(exc, requests.exceptions.HTTPError):
            return "api_error", str(exc)

        response = getattr(exc, "response", None)
        if response is None:
            return "api_error", str(exc)

        status = response.status_code

        # ── Auth errors (401 / 403) ───────────────────────────────────────
        if status in (401, 403):
            return "auth_error", (
                f"HTTP {status} — authentication/authorization failure for {entity or 'API call'}. "
                "Token may be expired or missing required scope. Reconnect QBO."
            )

        # ── Rate limit (429) ──────────────────────────────────────────────
        if status == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            return "rate_limit", (
                f"HTTP 429 — Intuit rate limit exceeded for {entity or 'API call'}. "
                f"Retry-After: {retry_after}s."
            )

        # ── Transient server errors (5xx) ─────────────────────────────────
        if status >= 500:
            return "temp_error", (
                f"HTTP {status} — Intuit server error for {entity or 'API call'}. "
                "This is likely transient; retry later."
            )

        # ── HTTP 400 — inspect Intuit response body ───────────────────────
        if status != 400:
            return "api_error", f"HTTP {status}: {exc}"

        try:
            body = response.json()
        except Exception:
            return "api_error", f"HTTP 400 (unparseable response body): {exc}"

        fault = body.get("Fault", {})
        errors = fault.get("Error", [])

        for err in errors:
            code = str(err.get("code", ""))
            message = (err.get("Message") or "").lower()
            detail  = (err.get("Detail") or "").lower()

            # ── Intuit error 4000 — entity not supported ──────────────────
            # "Invalid query — entity type not recognized / not available"
            if code == "4000":
                return "entity_not_supported", (
                    f"{entity}: not a valid query entity for this company "
                    f"(Intuit error {code}: {err.get('Message', '')})"
                )

            # ── Intuit error 4001 / 4002 — query validation failure ───────
            # Malformed SQL (bad field name, invalid operator, etc.)
            if code in ("4001", "4002"):
                return "malformed_query", (
                    f"{entity}: query validation error "
                    f"(Intuit error {code}: {err.get('Message', '')} — {err.get('Detail', '')})"
                )

            # ── Explicit "not supported / not available" language ─────────
            _unsupported_phrases = (
                "not supported",
                "not available",
                "invalid entity",
                "is not an available",
                "entity type not supported",
                "object not found",
                "unsupported entity",
            )
            if any(ph in detail for ph in _unsupported_phrases):
                return "entity_not_supported", (
                    f"{entity}: entity not available for this company configuration "
                    f"(Intuit error {code}: {err.get('Message', '')})"
                )
            if any(ph in message for ph in _unsupported_phrases):
                return "entity_not_supported", (
                    f"{entity}: entity not available for this company configuration "
                    f"(Intuit error {code}: {err.get('Message', '')})"
                )

        # ── Unknown 400 ───────────────────────────────────────────────────
        error_summary = "; ".join(
            f"code {e.get('code', '?')}: {e.get('Message', 'unknown')}"
            for e in errors
        ) if errors else str(exc)
        return "api_error", f"HTTP 400 — {error_summary}"

    except Exception:
        return "api_error", str(exc)


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
        """Run a QBO SQL-style query (single page, caller controls MAXRESULTS)."""
        data = self._get(db, "/query", {"query": sql, "minorversion": "65"})
        qr = data.get("QueryResponse", {})
        for key, val in qr.items():
            if isinstance(val, list):
                return val
        return []

    def _query_all(self, db: Session, sql_base: str, page_size: int = 1000) -> tuple[list, bool, int]:
        """Paginate through ALL QBO results for a SQL query.

        QBO hard-caps each response at 1,000 records. This method issues
        successive queries with STARTPOSITION until the last page returns
        fewer than page_size records, meaning we have everything.

        Args:
            sql_base:  SQL **without** STARTPOSITION or MAXRESULTS — those
                       are injected here.  Any existing STARTPOSITION /
                       MAXRESULTS in sql_base are stripped before use.
            page_size: Records per request (QBO max = 1000).

        Returns:
            (records, is_complete, total_fetched)
            - records:       all fetched records across all pages
            - is_complete:   True  → last page < page_size (all records retrieved)
                             False → loop stopped before exhausting results
                                     (shouldn't happen unless an error is raised)
            - total_fetched: len(records)
        """
        import re as _re
        # Strip any caller-supplied pagination clauses so we control them fully
        clean = _re.sub(r'\s+STARTPOSITION\s+\d+', '', sql_base, flags=_re.IGNORECASE)
        clean = _re.sub(r'\s+MAXRESULTS\s+\d+', '', clean, flags=_re.IGNORECASE).strip()

        all_records: list = []
        start = 1
        is_complete = False

        while True:
            sql = f"{clean} STARTPOSITION {start} MAXRESULTS {page_size}"
            page = self._query(db, sql)
            if not page:
                is_complete = True
                break
            all_records.extend(page)
            if len(page) < page_size:
                # Last page — we have everything
                is_complete = True
                break
            start += page_size
            # Safety: if QBO somehow returns 0 next page this loop exits above

        return all_records, is_complete, len(all_records)

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

    def get_checks_with_fallback(self, db: Session, start_date: str = None) -> tuple[list, dict]:
        """Fetch paper checks with automatic fallback for non-US companies.

        The QBO ``Check`` entity is only available for US companies that have
        paper check printing enabled.  Non-US companies (and some US
        configurations) return a 400 / entity_not_supported error.

        Fallback strategy:
            If Check entity returns entity_not_supported, retry with
            ``SELECT * FROM Purchase WHERE PaymentType = 'Check'``.
            These are the same transactions — just stored as Purchase
            records with PaymentType = 'Check' in QBO's data model.

        NOTE: When the fallback is used, the returned records have the
        same IDs as records already fetched by get_expenses().  Callers
        MUST deduplicate by transaction ID before processing.

        Returns:
            (records, retrieval_info)

            retrieval_info keys:
                entity_used    : 'Check' | 'Purchase_Check'
                used_fallback  : bool
                is_complete    : bool  (False if pagination was truncated)
                total_fetched  : int
                fallback_reason: str | None  (the original error detail)
        """
        # Build Check SQL (without MAXRESULTS — _query_all handles pagination)
        check_sql = "SELECT * FROM Check"
        if start_date:
            check_sql += f" WHERE TxnDate >= '{start_date}'"

        try:
            records, is_complete, total = self._query_all(db, check_sql)
            return records, {
                "entity_used": "Check",
                "used_fallback": False,
                "is_complete": is_complete,
                "total_fetched": total,
                "fallback_reason": None,
            }
        except Exception as exc:
            error_class, error_detail = classify_qbo_error(exc, entity="Check")
            if error_class != "entity_not_supported":
                # Real error (auth, temp, malformed) — propagate so the
                # caller's error handler can classify and log it properly.
                raise

            # ── Check entity not supported → fallback to Purchase(PaymentType='Check') ──
            fallback_sql = "SELECT * FROM Purchase WHERE PaymentType = 'Check'"
            if start_date:
                fallback_sql += f" AND TxnDate >= '{start_date}'"

            # Let fallback errors propagate — caller handles them
            records, is_complete, total = self._query_all(db, fallback_sql)
            return records, {
                "entity_used": "Purchase_Check",
                "used_fallback": True,
                "is_complete": is_complete,
                "total_fetched": total,
                "fallback_reason": error_detail,
            }

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
