"""
Database models — every model enforces isolation via realm_id.
ONE COMPANY = ONE ISOLATED ACCOUNTING WORKSPACE.
"""
import uuid
from datetime import datetime

from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import sqlalchemy
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, create_engine, event
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator
import json

from app.config import settings


# ── JSON column for SQLite ────────────────────────────────────
class JSONType(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(value) if value is not None else None

    def process_result_value(self, value, dialect):
        return json.loads(value) if value is not None else None


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.utcnow()


# ── Base ──────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Client (pre-QBO entity — one row per bookkeeping client) ──
class Client(Base):
    __tablename__ = "clients"

    id = Column(String(36), primary_key=True, default=_uuid)
    client_name = Column(String(255), nullable=False)
    legal_business_name = Column(String(255))
    contact_name = Column(String(255))
    email = Column(String(255))
    phone = Column(String(50))
    notes = Column(Text)
    status = Column(String(20), default="active")   # active | inactive | prospect
    # Which realm_id is the "active" workspace for this client (when multiple QBO companies)
    active_realm_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    companies = relationship("Company", back_populates="client")


# ── Company (one row = one QBO connection) ────────────────────
class Company(Base):
    __tablename__ = "companies"

    id = Column(String(36), primary_key=True, default=_uuid)
    realm_id = Column(String(100), unique=True, nullable=False, index=True)
    company_name = Column(String(255), nullable=False)
    qbo_environment = Column(String(20), default="sandbox")

    # Encrypted OAuth tokens (encrypted before storage)
    access_token_enc = Column(Text)
    refresh_token_enc = Column(Text)
    token_expires_at = Column(DateTime)

    # QBO-provided company info
    qbo_company_name = Column(String(255))
    qbo_country = Column(String(10))
    qbo_fiscal_year_start_month = Column(Integer)

    # Status
    connection_status = Column(String(30), default="disconnected")
    last_sync = Column(DateTime)
    last_sync_status = Column(String(30))
    last_sync_error = Column(Text)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    # Company type — used to activate restaurant-specific features
    company_type = Column(String(30), default="standard")   # standard | restaurant

    # Active workspace flag — only one Company should have is_active=True at a time
    is_active = Column(Boolean, default=False)

    # Client linkage (nullable — existing companies without a client are still valid)
    client_id = Column(String(36), ForeignKey("clients.id"), nullable=True, index=True)

    # Relationships
    client = relationship("Client", back_populates="companies")
    profile = relationship("CompanyProfile", back_populates="company", uselist=False,
                           cascade="all, delete-orphan")
    sync_history = relationship("SyncHistory", back_populates="company",
                                order_by="desc(SyncHistory.started_at)", cascade="all, delete-orphan")
    issues = relationship("AccountingIssue", back_populates="company",
                          cascade="all, delete-orphan")
    journal_entries = relationship("ProposedJournalEntry", back_populates="company",
                                   cascade="all, delete-orphan")
    change_log = relationship("ChangeLog", back_populates="company",
                              cascade="all, delete-orphan")
    month_end_closes = relationship("MonthEndClose", back_populates="company",
                                    cascade="all, delete-orphan")
    external_credentials = relationship("ExternalCredentials", back_populates="company",
                                        cascade="all, delete-orphan")
    proposed_categorizations = relationship("ProposedCategorization", back_populates="company",
                                            cascade="all, delete-orphan")
    restaurant_sales = relationship("RestaurantSalesData", back_populates="company",
                                    cascade="all, delete-orphan")
    ar_matches = relationship("ProposedARMatch", back_populates="company",
                              cascade="all, delete-orphan")
    work_items = relationship("WorkItem", back_populates="company",
                              cascade="all, delete-orphan")
    company_rules = relationship("CompanyRule", back_populates="company",
                                 cascade="all, delete-orphan")


# ── Company Accounting Profile ────────────────────────────────
class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), unique=True, nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    # Business identity
    industry = Column(String(100))
    entity_type = Column(String(50))           # LLC, S-Corp, C-Corp, Sole Prop, Nonprofit
    accounting_method = Column(String(20))     # Cash | Accrual
    fiscal_year_start_month = Column(Integer, default=1)
    currency = Column(String(10), default="USD")
    tax_jurisdictions = Column(JSONType)       # list of states/localities

    # QBO Account Structures (populated at sync — company-specific)
    chart_of_accounts = Column(JSONType)       # [{id, name, type, subtype, balance}]
    bank_accounts = Column(JSONType)           # [{id, name, last_reconciled, balance}]
    credit_cards = Column(JSONType)
    loans = Column(JSONType)
    payroll_system = Column(String(100))
    sales_tax_agency = Column(String(100))
    merchant_processors = Column(JSONType)     # [Stripe, Square, PayPal, etc.]

    # Company-specific accounting policies (learned only from THIS company)
    capitalization_threshold = Column(Float)
    reporting_preferences = Column(JSONType)
    accounting_conventions = Column(JSONType)  # e.g. {"software_account": "Technology Expense"}
    approved_decisions = Column(Text)          # formally approved accounting treatments (free text)
    controller_notes = Column(Text)            # internal controller notes
    tax_year_end = Column(String(50))          # e.g. "December 31"
    notes = Column(Text)

    # Risk/control boundary — NOT automatic authorization ceiling
    # WorkItems with amount > materiality_limit require autonomy_level >= 2 (INDIVIDUAL review)
    materiality_limit = Column(Float, default=2500.0)

    # Health score
    health_score = Column(Float)
    health_score_updated = Column(DateTime)
    health_details = Column(JSONType)          # breakdown by category

    # Cached QBO report data
    balance_sheet_data = Column(JSONType)
    pl_data = Column(JSONType)
    trial_balance_data = Column(JSONType)
    ar_aging_data = Column(JSONType)
    ap_aging_data = Column(JSONType)
    cash_flow_data = Column(JSONType)
    data_as_of = Column(DateTime)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="profile")


# ── Sync History ──────────────────────────────────────────────
class SyncHistory(Base):
    __tablename__ = "sync_history"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    sync_type = Column(String(20))    # full | incremental | manual | report
    started_at = Column(DateTime, default=_now)
    completed_at = Column(DateTime)
    status = Column(String(20))       # running | success | failed | partial

    records_retrieved = Column(Integer, default=0)
    records_changed = Column(Integer, default=0)
    endpoints_synced = Column(JSONType)
    errors = Column(JSONType)
    api_calls_made = Column(Integer, default=0)

    company = relationship("Company", back_populates="sync_history")


# ── Accounting Issues (Assessment Findings) ───────────────────
class AccountingIssue(Base):
    __tablename__ = "accounting_issues"

    id = Column(String(36), primary_key=True, default=_uuid)
    issue_id = Column(String(50), unique=True, nullable=False)  # e.g. ISS-0001
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    # Classification
    category = Column(String(50))     # bank_rec | credit_card | ar | ap | payroll | sales_tax | equity | fixed_asset | uncategorized | duplicate | loan | revenue | expenses
    severity = Column(String(20))     # critical | high | medium | low
    title = Column(String(255))
    description = Column(Text)

    # Transaction context
    account_id = Column(String(100))
    account_name = Column(String(255))
    transaction_id = Column(String(100))
    transaction_date = Column(DateTime)
    amount = Column(Float)
    period = Column(String(20))       # YYYY-MM or YYYY

    # Impact
    financial_impact = Column(Float)
    risk = Column(Text)
    likely_cause = Column(Text)
    evidence = Column(JSONType)       # [{description, qbo_id, amount}]

    # Resolution
    recommended_action = Column(Text)
    documentation_required = Column(Text)
    approval_required = Column(Boolean, default=False)

    # Status
    status = Column(String(30), default="open")  # open | in_progress | resolved | dismissed
    resolved_at = Column(DateTime)
    resolved_by = Column(String(100))
    resolution_notes = Column(Text)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="issues")


# ── Proposed Journal Entries ──────────────────────────────────
class ProposedJournalEntry(Base):
    __tablename__ = "proposed_journal_entries"

    id = Column(String(36), primary_key=True, default=_uuid)
    je_number = Column(String(50))    # JE-0001
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    # JE Details
    je_date = Column(DateTime)
    description = Column(Text)
    memo = Column(String(500))
    period = Column(String(20))

    # Lines: [{account_id, account_name, debit, credit, description}]
    lines = Column(JSONType)
    total_debits = Column(Float)
    total_credits = Column(Float)

    # Context
    reason = Column(Text)
    supporting_evidence = Column(JSONType)
    financial_statement_impact = Column(JSONType)  # {bs_accounts: [], pl_accounts: []}
    materiality = Column(String(30))   # immaterial | low | medium | high | material
    risk = Column(String(20))          # low | medium | high
    related_issue_id = Column(String(50))

    # Approval Workflow (READ → RECOMMEND → APPROVE → EXECUTE → VERIFY)
    approval_status = Column(String(20), default="pending")  # pending | approved | rejected | executed | verified
    submitted_by = Column(String(100))
    submitted_at = Column(DateTime)
    approved_by = Column(String(100))
    approved_at = Column(DateTime)
    rejection_reason = Column(Text)

    # Execution
    qbo_action = Column(String(50))           # create_journal_entry
    qbo_transaction_id = Column(String(100))
    qbo_transaction_number = Column(String(50))
    executed_at = Column(DateTime)
    execution_result = Column(JSONType)
    verification_status = Column(String(20))  # verified | failed
    verified_at = Column(DateTime)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="journal_entries")


# ── Audit Change Log ──────────────────────────────────────────
class ChangeLog(Base):
    __tablename__ = "change_log"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    action_type = Column(String(50))          # create_je | update_transaction | reconcile | etc.
    entity_type = Column(String(50))          # journal_entry | issue | company | etc.
    entity_id = Column(String(100))           # ID of the entity being changed
    description = Column(Text)
    notes = Column(Text)
    qbo_transaction_id = Column(String(100))
    action_datetime = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)

    # Change details
    original_value = Column(JSONType)
    new_value = Column(JSONType)
    before_values = Column(JSONType)
    after_values = Column(JSONType)
    reason = Column(Text)

    # People
    performed_by = Column(String(100))        # Who initiated the action
    approved_by = Column(String(100))         # Controller who approved
    ai_recommendation = Column(Text)
    human_approval = Column(Boolean, default=False)
    approving_user = Column(String(100))
    approval_datetime = Column(DateTime)

    # QBO API tracking
    api_response = Column(JSONType)
    api_status_code = Column(Integer)
    status = Column(String(20), default="success")   # success | error | pending
    error_message = Column(Text)
    verification_status = Column(String(20))
    verified_at = Column(DateTime)

    company = relationship("Company", back_populates="change_log")


# ── Month-End Close Checklist ────────────────────────────────
class MonthEndClose(Base):
    __tablename__ = "month_end_closes"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    period = Column(String(7), nullable=False)    # YYYY-MM
    status = Column(String(20), default="open")   # open | in_progress | closed

    # 24-step checklist stored as JSON
    # [{step_num, title, status, notes, completed_at, completed_by}]
    checklist = Column(JSONType)

    health_score_at_close = Column(Float)
    issues_at_close = Column(Integer)
    adjustments_posted = Column(Integer)

    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    closed_by = Column(String(100))

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="month_end_closes")


# ── External Platform Credentials ────────────────────────────
class ExternalCredentials(Base):
    """Encrypted OAuth tokens / API keys for third-party platforms (Square, etc.)."""
    __tablename__ = "external_credentials"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    platform = Column(String(50), nullable=False)   # square | grubhub | ubereats | doordash | otter | picnic
    credentials_enc = Column(Text)                  # encrypted JSON: {access_token, refresh_token, ...}
    account_id = Column(String(200))                # platform account / merchant ID
    location_id = Column(String(200))               # Square location ID (or equivalent)
    location_name = Column(String(200))
    webhook_signature_key_enc = Column(Text)        # for webhook verification

    connected_at = Column(DateTime)
    last_sync = Column(DateTime)
    status = Column(String(20), default="disconnected")  # connected | disconnected | error

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="external_credentials")


# ── Proposed Categorizations ──────────────────────────────────
class ProposedCategorization(Base):
    """
    AI-suggested reclassification for uncategorized / Ask My Accountant transactions.
    One row per transaction that needs categorization.
    """
    __tablename__ = "proposed_categorizations"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    # Source transaction
    qbo_txn_id = Column(String(100), nullable=False)
    qbo_txn_type = Column(String(50))       # Purchase | Expense | Check | Deposit | etc.
    txn_date = Column(DateTime)
    amount = Column(Float)
    payee_name = Column(String(255))        # vendor or customer name
    memo = Column(String(500))

    # Current (wrong) account
    current_account_id = Column(String(100))
    current_account_name = Column(String(255))

    # Suggested (correct) account
    suggested_account_id = Column(String(100))
    suggested_account_name = Column(String(255))
    suggested_account_type = Column(String(50))

    # AI reasoning
    confidence = Column(String(10))         # high | medium | low
    reason = Column(Text)                   # why this category was suggested
    rule_matched = Column(String(255))      # which rule/pattern triggered this

    # Approval workflow
    status = Column(String(20), default="pending")  # pending | approved | rejected | applied | skipped
    approved_by = Column(String(100))
    approved_at = Column(DateTime)
    applied_at = Column(DateTime)
    rejection_reason = Column(Text)
    qbo_update_response = Column(JSONType)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="proposed_categorizations")


# ── Restaurant Daily Sales ────────────────────────────────────
class RestaurantSalesData(Base):
    """
    Daily sales summary per platform for restaurant companies.
    Sourced from Square API, delivery platform exports, or manual entry.
    """
    __tablename__ = "restaurant_sales_data"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    sales_date = Column(String(10), nullable=False)     # YYYY-MM-DD
    platform = Column(String(50), nullable=False)       # square | grubhub | ubereats | doordash | otter | picnic | instore

    # Sales figures (all positive)
    gross_sales = Column(Float, default=0.0)            # total customer-facing sales
    refunds = Column(Float, default=0.0)                # refunds / voids
    net_sales = Column(Float, default=0.0)              # gross - refunds
    tax_collected = Column(Float, default=0.0)          # sales tax collected from customers
    tips = Column(Float, default=0.0)
    platform_fees = Column(Float, default=0.0)          # commissions, processing fees
    other_deductions = Column(Float, default=0.0)       # chargebacks, adjustments
    payout_amount = Column(Float, default=0.0)          # net deposit to bank

    # Source tracking
    source = Column(String(20), default="api")          # api | csv | manual
    raw_data = Column(JSONType)                         # original API/CSV response for audit

    # QBO Journal Entry
    je_id = Column(String(36))                          # FK to ProposedJournalEntry.id
    je_number = Column(String(50))
    je_status = Column(String(20), default="pending")   # pending | proposed | approved | posted

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="restaurant_sales")


# ── AR Proposed Match ─────────────────────────────────────────
class ProposedARMatch(Base):
    """
    AI-proposed match between an open invoice and an unapplied payment.
    Requires approval before applying in QBO.
    """
    __tablename__ = "proposed_ar_matches"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    # Invoice side
    invoice_id = Column(String(100))
    invoice_number = Column(String(50))
    invoice_date = Column(DateTime)
    invoice_due_date = Column(DateTime)
    invoice_amount = Column(Float)
    invoice_balance = Column(Float)
    customer_name = Column(String(255))

    # Payment side
    payment_id = Column(String(100))
    payment_date = Column(DateTime)
    payment_amount = Column(Float)
    payment_method = Column(String(50))
    payment_memo = Column(String(500))

    # Match details
    match_amount = Column(Float)                        # amount to apply (may be partial)
    match_confidence = Column(String(10))               # high | medium | low
    match_reason = Column(Text)                         # why this match was suggested
    amount_difference = Column(Float, default=0.0)      # difference after match

    # Status
    status = Column(String(20), default="pending")      # pending | approved | rejected | applied
    approved_by = Column(String(100))
    approved_at = Column(DateTime)
    applied_at = Column(DateTime)
    qbo_response = Column(JSONType)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="ar_matches")


# ── Work Items (generalized AI action queue) ──────────────────
class WorkItem(Base):
    """
    Generalized proposed action. Every AI-detected work item lives here
    before it is approved and executed. Phase 1 implements work_type=CATEGORIZE.
    Extensible to AR_MATCH, BANK_REC, DUPLICATE, MISSING_ENTRY, etc.

    SAFETY INVARIANT: Nothing is written to QBO until status='approved'
    and execute_work_item() is explicitly called by a controller action.
    The engine may READ from QBO and CREATE/UPDATE WorkItem records freely.
    It must NOT write categorization changes to QBO without explicit approval.
    """
    __tablename__ = "work_items"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)

    # Which engine created this and what action it proposes
    work_type = Column(String(30), nullable=False, default="CATEGORIZE")
    # Supported: CATEGORIZE | AR_MATCH | BANK_REC | DUPLICATE | MISSING_ENTRY

    # Source transaction (populated for CATEGORIZE work type)
    qbo_txn_id = Column(String(100))
    qbo_txn_type = Column(String(50))    # Purchase | Check | Deposit | SalesReceipt
    txn_date = Column(DateTime)
    amount = Column(Float)
    payee_name = Column(String(255))
    memo = Column(String(500))

    # Current state in QBO
    current_account_id = Column(String(100))
    current_account_name = Column(String(255))

    # Proposed new state
    proposed_account_id = Column(String(100))
    proposed_account_name = Column(String(255))
    proposed_account_type = Column(String(50))

    # Four-factor AI assessment
    confidence = Column(String(10), default="low")   # high | medium | low | none
    risk = Column(String(10), default="low")          # low | medium | high
    # 0=AUTO (no human needed)  1=BATCH (approve with others)
    # 2=INDIVIDUAL (one-by-one) 3=HUMAN_REQUIRED (do not auto-propose)
    autonomy_level = Column(Integer, default=2)
    rule_matched = Column(String(255))
    reason = Column(Text)

    # Status lifecycle:
    # pending → approved → executing → applied
    # pending → rejected
    # (errors during detection are stored in error_message, status stays pending)
    status = Column(String(20), default="pending")

    # Approval
    approved_by = Column(String(100))
    approved_at = Column(DateTime)
    rejection_reason = Column(Text)

    # Execution (QBO write — gated by approved status + explicit controller call)
    executed_at = Column(DateTime)
    execution_result = Column(JSONType)
    qbo_update_response = Column(JSONType)
    verification_status = Column(String(20))   # verified | failed | unverified
    verified_at = Column(DateTime)

    # Engine error capture — surfaced to UI, never silently swallowed
    error_message = Column(Text)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="work_items")


# ── Company Rules (client-specific accounting rules) ──────────
class CompanyRule(Base):
    """
    Client-specific accounting rules evaluated by the categorization engine.
    realm_id is NON-NULLABLE: schema-level company isolation.
    A rule created for one company CANNOT be evaluated against another.

    Rule types:
      CATEGORIZE — match pattern → assign proposed_account (most common)
      SKIP       — match pattern → suppress proposal (txn already handled)
      FLAG       — match pattern → force autonomy_level=HUMAN_REQUIRED

    Rules are checked in priority order (desc) before global library rules.
    Only active/approved rules are evaluated by the engine.
    """
    __tablename__ = "company_rules"

    id = Column(String(36), primary_key=True, default=_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False)
    realm_id = Column(String(100), nullable=False, index=True)  # NON-NULLABLE: schema isolation

    # Identity
    rule_name = Column(String(255), nullable=False)       # human-readable name
    description = Column(Text)                             # what this rule does and why

    # Classification
    rule_type = Column(String(30), nullable=False, default="CATEGORIZE")
    # CATEGORIZE | SKIP | FLAG

    # Matching condition
    condition_type = Column(String(30), default="PAYEE_OR_MEMO")
    # PAYEE_MATCH | MEMO_MATCH | PAYEE_OR_MEMO | PAYEE_AND_MEMO | REGEX | AMOUNT_RANGE
    pattern = Column(String(500), nullable=False)          # keyword or regex string
    pattern_flags = Column(String(50), default="case_insensitive")  # e.g. "case_insensitive"

    # Proposed action (for CATEGORIZE rules)
    proposed_account_id = Column(String(100))
    proposed_account_name = Column(String(255))

    # Confidence contribution from this rule
    confidence_strength = Column(String(10), default="high")  # high | medium | low

    # Control
    priority = Column(Integer, default=100)    # higher number = checked first
    status = Column(String(20), default="active")  # active | suspended | draft | archived

    # Governance
    created_by = Column(String(100))           # who created this rule
    approved_by = Column(String(100))          # controller who approved
    approved_at = Column(DateTime)
    version = Column(Integer, default=1)       # incremented on each material change
    rule_history = Column(JSONType)            # [{version, changed_at, changed_by, change_summary}]

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    company = relationship("Company", back_populates="company_rules")


# ── Database Setup ────────────────────────────────────────────

def _build_engine():
    """Build a SQLAlchemy engine appropriate for the configured database."""
    url = settings.DATABASE_URL

    if "sqlite" in url:
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=settings.DEBUG,
        )

        @event.listens_for(eng, "connect")
        def set_sqlite_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return eng

    # PostgreSQL / Neon serverless ─────────────────────────────
    # 1. Strip channel_binding=require — Neon's PgBouncer pooler does not
    #    support SCRAM-SHA-256-PLUS channel binding and the negotiation hangs.
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.pop("channel_binding", None)
    new_query = urlencode({k: v[0] for k, v in params.items()})
    clean_url = urlunparse(parsed._replace(query=new_query))

    # 2. Use NullPool so connections are never held open between requests
    #    (required for serverless / connection-pooled Neon endpoints).
    from sqlalchemy.pool import NullPool

    return create_engine(
        clean_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 30},  # 30-second TCP timeout
        echo=settings.DEBUG,
    )


engine = _build_engine()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _run_migrations():
    """Add any missing columns to existing tables (safe to run on every startup)."""
    migrations = [
        # company_type added for restaurant feature
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS company_type VARCHAR(30) DEFAULT 'standard'",
        # restaurant sales table columns
        "ALTER TABLE restaurant_sales_data ADD COLUMN IF NOT EXISTS gross_sales FLOAT DEFAULT 0",
        "ALTER TABLE restaurant_sales_data ADD COLUMN IF NOT EXISTS refunds FLOAT DEFAULT 0",
        "ALTER TABLE restaurant_sales_data ADD COLUMN IF NOT EXISTS tax_collected FLOAT DEFAULT 0",
        "ALTER TABLE restaurant_sales_data ADD COLUMN IF NOT EXISTS tips FLOAT DEFAULT 0",
        "ALTER TABLE restaurant_sales_data ADD COLUMN IF NOT EXISTS je_status VARCHAR(30)",
        "ALTER TABLE restaurant_sales_data ADD COLUMN IF NOT EXISTS je_id VARCHAR(100)",
        # Phase 1 WorkItem: materiality_limit on company profiles
        "ALTER TABLE company_profiles ADD COLUMN IF NOT EXISTS materiality_limit FLOAT DEFAULT 2500",
        # Client linkage on companies (nullable — preserves existing connections)
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS client_id VARCHAR(36)",
        # Active realm tracking on clients
        "ALTER TABLE clients ADD COLUMN IF NOT EXISTS active_realm_id VARCHAR(100)",
        # Phase 2.6: active workspace flag on companies (multi-company support)
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT FALSE",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(sqlalchemy.text(sql))
            except Exception:
                pass  # column may already exist or table not yet created
        conn.commit()


def init_db():
    """Create all tables then run column migrations."""
    Base.metadata.create_all(bind=engine)
    try:
        _run_migrations()
    except Exception:
        pass  # non-fatal; SQLite doesn't support IF NOT EXISTS on ALTER


def get_db():
    """FastAPI dependency — yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
