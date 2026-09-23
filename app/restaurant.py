"""
Restaurant Integration Engine
──────────────────────────────
Handles daily sales data from all platforms used by restaurant companies.

Supported platforms:
  • Square     — REST API (OAuth 2.0)
  • GrubHub    — CSV report import (no public API for merchants)
  • Uber Eats  — CSV report import
  • DoorDash   — CSV report import
  • Otter      — CSV / webhook (order aggregator)
  • Picnic     — CSV import

Workflow for each day:
  1. Fetch / import daily sales per platform
  2. Store in RestaurantSalesData
  3. Generate a ProposedJournalEntry that records:
       DR  [Platform] Merchant Receivable   net payout amount
       DR  Platform Fees Expense            commission / processing fee
       DR  Sales Tax Payable (contra)       clears collected tax (already a liability)
         CR  Food & Beverage Revenue        gross sales
  4. Require controller approval before posting to QBO

Chart-of-accounts mapping is stored in CompanyProfile.accounting_conventions
under the key "restaurant_accounts" (see RESTAURANT_ACCOUNT_DEFAULTS below).
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, date, timedelta
from typing import Optional
from sqlalchemy.orm import Session

import requests

from app.database import (
    Company, CompanyProfile, ExternalCredentials,
    RestaurantSalesData, ProposedJournalEntry, ChangeLog, _now,
)
from app.security import encrypt_token, decrypt_token


# ── Default account name mapping (override per company in accounting_conventions) ──
RESTAURANT_ACCOUNT_DEFAULTS = {
    "food_revenue": "Food Sales",
    "beverage_revenue": "Beverage Sales",
    "other_revenue": "Other Revenue",
    "sales_tax_payable": "Sales Tax Payable",
    "tips_payable": "Tips Payable",
    # Platform receivable / clearing accounts
    "square_receivable": "Square Merchant Account",
    "grubhub_receivable": "GrubHub Receivable",
    "ubereats_receivable": "Uber Eats Receivable",
    "doordash_receivable": "DoorDash Receivable",
    "otter_receivable": "Otter Clearing",
    "picnic_receivable": "Picnic Receivable",
    "instore_cash": "Undeposited Funds",
    # Expense accounts
    "platform_fees": "Delivery Platform Fees",
    "processing_fees": "Merchant Processing Fees",
    "tips_expense": "Tips Expense",
}

PLATFORM_RECEIVABLE_KEY = {
    "square":    "square_receivable",
    "grubhub":   "grubhub_receivable",
    "ubereats":  "ubereats_receivable",
    "doordash":  "doordash_receivable",
    "otter":     "otter_receivable",
    "picnic":    "picnic_receivable",
    "instore":   "instore_cash",
}

PLATFORM_DISPLAY = {
    "square":   "Square",
    "grubhub":  "GrubHub",
    "ubereats": "Uber Eats",
    "doordash": "DoorDash",
    "otter":    "Otter",
    "picnic":   "Picnic",
    "instore":  "In-Store",
}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — SQUARE API CLIENT
# ═══════════════════════════════════════════════════════════════

SQUARE_API_BASE = "https://connect.squareup.com/v2"
SQUARE_SANDBOX  = "https://connect.squareupsandbox.com/v2"


class SquareClient:
    """
    Square REST API client bound to one company / location.
    Uses the access token stored in ExternalCredentials.
    """

    def __init__(self, creds: ExternalCredentials, sandbox: bool = False):
        self.creds = creds
        self.location_id = creds.location_id
        self.base = SQUARE_SANDBOX if sandbox else SQUARE_API_BASE
        self._token: str | None = None

    @property
    def token(self) -> str:
        if not self._token:
            enc = self.creds.credentials_enc
            raw = decrypt_token(enc) if enc else ""
            data = json.loads(raw) if raw else {}
            self._token = data.get("access_token", "")
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Square-Version": "2024-01-17",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{self.base}{path}", headers=self._headers(),
            params=params or {}, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self.base}{path}", headers=self._headers(),
            json=payload, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def list_locations(self) -> list[dict]:
        data = self._get("/locations")
        return data.get("locations", [])

    def get_orders_summary(self, start_date: str, end_date: str) -> dict:
        """
        Fetch daily sales summary for a date range using the Orders API.
        Returns aggregated totals.
        """
        start_dt = f"{start_date}T00:00:00Z"
        end_dt   = f"{end_date}T23:59:59Z"

        payload = {
            "location_ids": [self.location_id],
            "query": {
                "filter": {
                    "date_time_filter": {
                        "created_at": {"start_at": start_dt, "end_at": end_dt}
                    },
                    "state_filter": {"states": ["COMPLETED"]},
                },
                "sort": {"sort_field": "CREATED_AT", "sort_order": "ASC"},
            },
            "limit": 500,
        }

        all_orders = []
        cursor = None
        while True:
            if cursor:
                payload["cursor"] = cursor
            resp = self._post("/orders/search", payload)
            all_orders.extend(resp.get("orders", []))
            cursor = resp.get("cursor")
            if not cursor:
                break

        return _aggregate_square_orders(all_orders, start_date)

    def get_payments_summary(self, start_date: str, end_date: str) -> dict:
        """Fetch payment totals (includes processing fees) for one day."""
        params = {
            "begin_time": f"{start_date}T00:00:00Z",
            "end_time":   f"{end_date}T23:59:59Z",
            "location_id": self.location_id,
            "limit": 200,
        }
        all_payments = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = self._get("/payments", params)
            all_payments.extend(resp.get("payments", []))
            cursor = resp.get("cursor")
            if not cursor:
                break

        return _aggregate_square_payments(all_payments)


def _aggregate_square_orders(orders: list[dict], sales_date: str) -> dict:
    """Sum up gross sales, refunds, tax, tips from Square orders."""
    gross = refunds = tax = tips = 0.0
    for order in orders:
        total_money = order.get("total_money", {})
        gross += _sq_money(total_money)
        refunds += _sq_money(order.get("total_discount_money", {}))
        tax += _sq_money(order.get("total_tax_money", {}))
        tips += _sq_money(order.get("total_tip_money", {}))
    net = gross - refunds
    return {
        "date": sales_date,
        "platform": "square",
        "gross_sales": round(gross, 2),
        "refunds": round(refunds, 2),
        "net_sales": round(net, 2),
        "tax_collected": round(tax, 2),
        "tips": round(tips, 2),
    }


def _aggregate_square_payments(payments: list[dict]) -> dict:
    """Sum processing fees from Square payment objects."""
    total_fees = 0.0
    total_payouts = 0.0
    for pmt in payments:
        status = pmt.get("status", "")
        if status != "COMPLETED":
            continue
        total = _sq_money(pmt.get("total_money", {}))
        fee = _sq_money(pmt.get("processing_fee", [{}])[0].get("amount_money", {})) if pmt.get("processing_fee") else 0.0
        total_payouts += total - fee
        total_fees += fee
    return {
        "processing_fees": round(total_fees, 2),
        "net_payout": round(total_payouts, 2),
    }


def _sq_money(money_obj: dict) -> float:
    """Convert Square money object (amount in cents) to dollars."""
    return float(money_obj.get("amount", 0)) / 100.0


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — DELIVERY PLATFORM CSV PARSERS
# ═══════════════════════════════════════════════════════════════

def parse_grubhub_csv(csv_content: str, sales_date: str) -> dict:
    """
    Parse a GrubHub daily/weekly payout report CSV.
    GrubHub format: Order Date, Order ID, Subtotal, Marketplace Facilitated Tax,
                    GrubHub Fees, Adjustments, Payout
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    gross = tax = fees = payout = refunds = 0.0
    for row in reader:
        row_date = _normalize_date(row.get("Order Date", "") or row.get("Date", ""))
        if row_date and row_date != sales_date:
            continue
        gross   += _csv_float(row, ["Subtotal", "Order Subtotal", "Gross Sales"])
        tax     += _csv_float(row, ["Marketplace Facilitated Tax", "Tax"])
        fees    += abs(_csv_float(row, ["GrubHub Fees", "Commission", "Fee"]))
        refunds += abs(_csv_float(row, ["Adjustments", "Refund", "Refunds"]))
        payout  += _csv_float(row, ["Payout", "Net Payout", "Settlement"])

    net = gross - refunds
    return {
        "date": sales_date,
        "platform": "grubhub",
        "gross_sales": round(gross, 2),
        "refunds": round(refunds, 2),
        "net_sales": round(net, 2),
        "tax_collected": round(tax, 2),
        "tips": 0.0,
        "platform_fees": round(fees, 2),
        "payout_amount": round(payout, 2),
    }


def parse_ubereats_csv(csv_content: str, sales_date: str) -> dict:
    """
    Parse an Uber Eats weekly earnings CSV.
    Uber Eats format: Date, Order ID, Customer Total, Delivery Fee, Uber Eats Fees,
                      Tip, Adjustments, Your Earnings
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    gross = tax = fees = payout = tips = refunds = 0.0
    for row in reader:
        row_date = _normalize_date(row.get("Date", "") or row.get("Trip Date", ""))
        if row_date and row_date != sales_date:
            continue
        gross   += _csv_float(row, ["Customer Total", "Subtotal", "Order Subtotal"])
        tax     += _csv_float(row, ["Tax", "Sales Tax"])
        fees    += abs(_csv_float(row, ["Uber Eats Fees", "Uber Fee", "Service Fee", "Commission"]))
        tips    += _csv_float(row, ["Tip", "Tip Amount"])
        refunds += abs(_csv_float(row, ["Adjustments", "Refund"]))
        payout  += _csv_float(row, ["Your Earnings", "Net Payout", "Payout"])

    net = gross - refunds
    return {
        "date": sales_date,
        "platform": "ubereats",
        "gross_sales": round(gross, 2),
        "refunds": round(refunds, 2),
        "net_sales": round(net, 2),
        "tax_collected": round(tax, 2),
        "tips": round(tips, 2),
        "platform_fees": round(fees, 2),
        "payout_amount": round(payout, 2),
    }


def parse_doordash_csv(csv_content: str, sales_date: str) -> dict:
    """
    Parse a DoorDash weekly payout CSV.
    DoorDash format: Date, Order ID, Subtotal, Tax, Dasher Tip, DoorDash Commission,
                     Payment Processing, Adjustments, Net Payout
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    gross = tax = fees = payout = tips = refunds = 0.0
    for row in reader:
        row_date = _normalize_date(row.get("Date", "") or row.get("Delivery Date", ""))
        if row_date and row_date != sales_date:
            continue
        gross   += _csv_float(row, ["Subtotal", "Order Subtotal", "Gross Sales"])
        tax     += _csv_float(row, ["Tax", "Sales Tax", "Marketplace Tax"])
        fees    += abs(_csv_float(row, ["DoorDash Commission", "Commission", "Platform Fee"]))
        fees    += abs(_csv_float(row, ["Payment Processing", "Processing Fee"]))
        tips    += _csv_float(row, ["Dasher Tip", "Tip", "Customer Tip"])
        refunds += abs(_csv_float(row, ["Adjustments", "Refund", "Error Charge"]))
        payout  += _csv_float(row, ["Net Payout", "Payout", "Your Payout"])

    net = gross - refunds
    return {
        "date": sales_date,
        "platform": "doordash",
        "gross_sales": round(gross, 2),
        "refunds": round(refunds, 2),
        "net_sales": round(net, 2),
        "tax_collected": round(tax, 2),
        "tips": round(tips, 2),
        "platform_fees": round(fees, 2),
        "payout_amount": round(payout, 2),
    }


def parse_otter_csv(csv_content: str, sales_date: str) -> dict:
    """
    Parse an Otter export CSV (order aggregator for multiple channels).
    Otter typically exports: Date, Platform, Order ID, Subtotal, Tax, Fees, Net
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    gross = tax = fees = payout = refunds = 0.0
    for row in reader:
        row_date = _normalize_date(row.get("Date", "") or row.get("Order Date", ""))
        if row_date and row_date != sales_date:
            continue
        gross   += _csv_float(row, ["Subtotal", "Order Amount", "Gross"])
        tax     += _csv_float(row, ["Tax", "Sales Tax"])
        fees    += abs(_csv_float(row, ["Fees", "Commission", "Platform Fee"]))
        refunds += abs(_csv_float(row, ["Refunds", "Refund", "Adjustments"]))
        payout  += _csv_float(row, ["Net", "Net Payout", "Payout"])

    net = gross - refunds
    return {
        "date": sales_date,
        "platform": "otter",
        "gross_sales": round(gross, 2),
        "refunds": round(refunds, 2),
        "net_sales": round(net, 2),
        "tax_collected": round(tax, 2),
        "tips": 0.0,
        "platform_fees": round(fees, 2),
        "payout_amount": round(payout, 2),
    }


def parse_picnic_csv(csv_content: str, sales_date: str) -> dict:
    """
    Parse a Picnic export CSV.
    Picnic format may vary; we attempt common column names.
    """
    reader = csv.DictReader(io.StringIO(csv_content))
    gross = tax = fees = payout = refunds = 0.0
    for row in reader:
        row_date = _normalize_date(row.get("Date", "") or row.get("Order Date", ""))
        if row_date and row_date != sales_date:
            continue
        gross   += _csv_float(row, ["Subtotal", "Sales", "Gross Sales"])
        tax     += _csv_float(row, ["Tax", "Sales Tax"])
        fees    += abs(_csv_float(row, ["Fees", "Commission", "Platform Fee", "Service Fee"]))
        refunds += abs(_csv_float(row, ["Refunds", "Refund", "Void"]))
        payout  += _csv_float(row, ["Net", "Payout", "Net Payout", "Your Earnings"])

    net = gross - refunds
    return {
        "date": sales_date,
        "platform": "picnic",
        "gross_sales": round(gross, 2),
        "refunds": round(refunds, 2),
        "net_sales": round(net, 2),
        "tax_collected": round(tax, 2),
        "tips": 0.0,
        "platform_fees": round(fees, 2),
        "payout_amount": round(payout, 2),
    }


# ── Helpers ─────────────────────────────────────────────────────

def _csv_float(row: dict, keys: list[str]) -> float:
    """Try each key in order; return float or 0."""
    for k in keys:
        val = row.get(k, "").replace("$", "").replace(",", "").strip()
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    return 0.0


def _normalize_date(raw: str) -> str | None:
    """Try to parse a date string into YYYY-MM-DD."""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%b %d, %Y", "%B %d, %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — SAVE SALES DATA
# ═══════════════════════════════════════════════════════════════

def save_platform_sales(
    db: Session,
    company: Company,
    sales_data: dict,
    source: str = "api",
    raw_data: dict | None = None,
) -> RestaurantSalesData:
    """
    Upsert a RestaurantSalesData record.
    If one already exists for (realm_id, sales_date, platform), it is updated.
    """
    sales_date = sales_data["date"]
    platform   = sales_data["platform"]

    existing = (
        db.query(RestaurantSalesData)
          .filter_by(realm_id=company.realm_id, sales_date=sales_date, platform=platform)
          .first()
    )
    if existing:
        rec = existing
    else:
        rec = RestaurantSalesData(
            company_id=company.id,
            realm_id=company.realm_id,
            sales_date=sales_date,
            platform=platform,
        )
        db.add(rec)

    rec.gross_sales      = sales_data.get("gross_sales", 0.0)
    rec.refunds          = sales_data.get("refunds", 0.0)
    rec.net_sales        = sales_data.get("net_sales", 0.0)
    rec.tax_collected    = sales_data.get("tax_collected", 0.0)
    rec.tips             = sales_data.get("tips", 0.0)
    rec.platform_fees    = sales_data.get("platform_fees", 0.0)
    rec.payout_amount    = sales_data.get("payout_amount", 0.0)
    rec.source           = source
    rec.raw_data         = raw_data

    return rec


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — DAILY SALES JOURNAL ENTRY GENERATOR
# ═══════════════════════════════════════════════════════════════

def generate_daily_sales_je(
    db: Session,
    company: Company,
    profile: CompanyProfile,
    sales_date: str,
) -> ProposedJournalEntry | None:
    """
    Aggregate all platform sales for `sales_date` and generate ONE
    ProposedJournalEntry that records the day's revenue, tax, fees,
    and expected platform payouts.

    Journal Entry structure:
    ──────────────────────────────────────────────────────────────
    DEBIT:   [Platform] Merchant Receivable    (net payout per platform)
    DEBIT:   Platform Fees Expense             (total commissions + processing)
    DEBIT:   Sales Tax Payable                 (contra — reverses tax liability on payout)
    CREDIT:  Food & Beverage Revenue           (total gross net sales)
    CREDIT:  Sales Tax Payable                 (tax collected, creates liability)
    NOTE:    Tips are recorded separately (tips payable / tips expense).
    ──────────────────────────────────────────────────────────────
    """
    # Load all platform records for this date
    platform_records = (
        db.query(RestaurantSalesData)
          .filter_by(realm_id=company.realm_id, sales_date=sales_date)
          .filter(RestaurantSalesData.je_status == "pending")
          .all()
    )

    if not platform_records:
        return None

    # Load account conventions
    conventions: dict = profile.accounting_conventions or {}
    ra: dict = {**RESTAURANT_ACCOUNT_DEFAULTS, **conventions.get("restaurant_accounts", {})}

    # Build JE lines
    lines = []
    total_gross_sales = 0.0
    total_tax         = 0.0
    total_fees        = 0.0
    total_tips        = 0.0

    for rec in platform_records:
        platform = rec.platform
        payout   = rec.payout_amount or (rec.net_sales - rec.platform_fees)
        fees     = rec.platform_fees or 0.0
        tax      = rec.tax_collected or 0.0
        tips     = rec.tips or 0.0
        gross    = rec.gross_sales or 0.0
        net_sales = rec.net_sales or 0.0

        total_gross_sales += net_sales   # we credit NET sales (after refunds)
        total_tax         += tax
        total_fees        += fees
        total_tips        += tips

        display = PLATFORM_DISPLAY.get(platform, platform.title())
        recv_acct_key = PLATFORM_RECEIVABLE_KEY.get(platform, "square_receivable")
        recv_acct = ra.get(recv_acct_key, f"{display} Receivable")

        if payout > 0.01:
            lines.append({
                "account_name": recv_acct,
                "description": f"{display} net payout — {sales_date}",
                "debit": round(payout, 2),
                "credit": 0.0,
            })
        if fees > 0.01:
            lines.append({
                "account_name": ra.get("platform_fees", "Delivery Platform Fees"),
                "description": f"{display} commission / processing fee — {sales_date}",
                "debit": round(fees, 2),
                "credit": 0.0,
            })

    # Credit: total net sales as revenue
    if total_gross_sales > 0.01:
        lines.append({
            "account_name": ra.get("food_revenue", "Food Sales"),
            "description": f"Daily gross sales (net of refunds) — {sales_date}",
            "debit": 0.0,
            "credit": round(total_gross_sales, 2),
        })

    # Credit: sales tax collected (creates / increases liability)
    if total_tax > 0.01:
        lines.append({
            "account_name": ra.get("sales_tax_payable", "Sales Tax Payable"),
            "description": f"Sales tax collected — {sales_date}",
            "debit": 0.0,
            "credit": round(total_tax, 2),
        })

    # Tips (if any): DR Tips Payable (we owe to staff), CR Revenue (collected from customer)
    if total_tips > 0.01:
        lines.append({
            "account_name": ra.get("tips_payable", "Tips Payable"),
            "description": f"Customer tips — to be distributed to staff — {sales_date}",
            "debit": 0.0,
            "credit": round(total_tips, 2),
        })
        # When tips are paid out to employees they debit Tips Payable.
        # Record the debit side here as an asset (receivable from platforms)
        # already covered above in the payout line. Tips increase payout amount.

    # Validate: debits == credits (they should since payout = net_sales + tax - fees +/- adj)
    total_debits  = round(sum(l["debit"]  for l in lines), 2)
    total_credits = round(sum(l["credit"] for l in lines), 2)

    # If there's a rounding difference, add it to the last line
    rounding_diff = round(total_debits - total_credits, 2)
    if abs(rounding_diff) > 0.00 and abs(rounding_diff) < 1.00:
        lines.append({
            "account_name": "Rounding Difference",
            "description": "Rounding adjustment",
            "debit": 0.0 if rounding_diff > 0 else abs(rounding_diff),
            "credit": rounding_diff if rounding_diff > 0 else 0.0,
        })
        total_debits  = round(sum(l["debit"]  for l in lines), 2)
        total_credits = round(sum(l["credit"] for l in lines), 2)

    platform_names = ", ".join(
        PLATFORM_DISPLAY.get(r.platform, r.platform.title())
        for r in platform_records
    )

    # Count existing JEs for this company to generate JE number
    je_count = db.query(ProposedJournalEntry).filter_by(realm_id=company.realm_id).count()
    je_number = f"JE-{je_count + 1:04d}"

    je = ProposedJournalEntry(
        je_number=je_number,
        company_id=company.id,
        realm_id=company.realm_id,
        je_date=datetime.strptime(sales_date, "%Y-%m-%d"),
        description=f"Daily Sales — {sales_date} ({platform_names})",
        memo=f"Restaurant daily sales journal entry — {platform_names}",
        period=sales_date[:7],
        lines=lines,
        total_debits=total_debits,
        total_credits=total_credits,
        reason=(
            f"Consolidated daily sales for {sales_date} across {len(platform_records)} platform(s). "
            f"Gross sales: ${total_gross_sales:,.2f} | Fees: ${total_fees:,.2f} | "
            f"Tax: ${total_tax:,.2f} | Tips: ${total_tips:,.2f}."
        ),
        materiality=_materiality_label(total_gross_sales),
        risk="low",
        approval_status="pending",
        qbo_action="create_journal_entry",
    )
    db.add(je)

    # Link the sales records to this JE
    for rec in platform_records:
        rec.je_status = "proposed"
        rec.je_number = je_number

    return je


def _materiality_label(amount: float) -> str:
    if amount < 500:
        return "immaterial"
    elif amount < 5000:
        return "low"
    elif amount < 25000:
        return "medium"
    elif amount < 100000:
        return "high"
    return "material"


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — SQUARE OAUTH FLOW HELPERS
# ═══════════════════════════════════════════════════════════════

SQUARE_OAUTH_URL = "https://connect.squareup.com/oauth2/authorize"
SQUARE_TOKEN_URL = "https://connect.squareup.com/oauth2/token"


def build_square_auth_url(
    square_app_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Generate the Square OAuth authorization URL."""
    import urllib.parse
    params = {
        "client_id": square_app_id,
        "scope": "MERCHANT_PROFILE_READ ORDERS_READ PAYMENTS_READ SETTLEMENTS_READ",
        "session": "false",
        "state": state,
    }
    return f"{SQUARE_OAUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_square_code(
    code: str,
    square_app_id: str,
    square_app_secret: str,
    redirect_uri: str,
) -> dict:
    """Exchange Square OAuth code for tokens."""
    resp = requests.post(
        SQUARE_TOKEN_URL,
        json={
            "client_id": square_app_id,
            "client_secret": square_app_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Square-Version": "2024-01-17", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def save_square_credentials(
    db: Session,
    company: Company,
    access_token: str,
    merchant_id: str,
    location_id: str = "",
    location_name: str = "",
) -> ExternalCredentials:
    """Store Square credentials (encrypted) for a company."""
    existing = (
        db.query(ExternalCredentials)
          .filter_by(realm_id=company.realm_id, platform="square")
          .first()
    )
    creds_json = json.dumps({"access_token": access_token, "merchant_id": merchant_id})

    if existing:
        rec = existing
    else:
        rec = ExternalCredentials(
            company_id=company.id,
            realm_id=company.realm_id,
            platform="square",
        )
        db.add(rec)

    rec.credentials_enc = encrypt_token(creds_json)
    rec.account_id      = merchant_id
    rec.location_id     = location_id
    rec.location_name   = location_name
    rec.connected_at    = _now()
    rec.status          = "connected"
    db.commit()
    return rec


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — FETCH SQUARE DAILY SALES (ALL-IN-ONE)
# ═══════════════════════════════════════════════════════════════

def fetch_square_sales_for_date(
    db: Session,
    company: Company,
    profile: CompanyProfile,
    sales_date: str,
    sandbox: bool = False,
) -> RestaurantSalesData | None:
    """
    Fetch Square sales for `sales_date`, save to RestaurantSalesData,
    and return the record. Returns None if no Square credentials exist.
    """
    creds = (
        db.query(ExternalCredentials)
          .filter_by(realm_id=company.realm_id, platform="square", status="connected")
          .first()
    )
    if not creds:
        return None

    client = SquareClient(creds, sandbox=sandbox)

    try:
        orders_summary = client.get_orders_summary(sales_date, sales_date)
        pmt_summary    = client.get_payments_summary(sales_date, sales_date)
    except Exception as e:
        return None

    payout = (
        orders_summary.get("net_sales", 0)
        + orders_summary.get("tips", 0)
        - pmt_summary.get("processing_fees", 0)
    )

    sales_data = {
        **orders_summary,
        "platform_fees": pmt_summary.get("processing_fees", 0),
        "payout_amount": round(payout, 2),
    }

    rec = save_platform_sales(db, company, sales_data, source="api", raw_data={
        "orders": orders_summary,
        "payments": pmt_summary,
    })
    db.commit()
    return rec
