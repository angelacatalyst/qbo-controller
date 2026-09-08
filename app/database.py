"""
Database models — every model enforces isolation via realm_id.
ONE COMPANY = ONE ISOLATED ACCOUNTING WORKSPACE.
"""
import uuid
from datetime import datetime

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

    # Relationships
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


# ── Database Setup ────────────────────────────────────────────
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
    echo=settings.DEBUG,
)

# Enable WAL mode for better SQLite concurrency
if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
