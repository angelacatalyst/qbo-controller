"""
QBO AI Controller — FastAPI Application
────────────────────────────────────────
All routes enforce realm_id isolation.
Default mode: READ-ONLY. Write actions require approval.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    Depends, FastAPI, Form, HTTPException, Query, Request, status
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import (
    AccountingIssue, ChangeLog, Company, CompanyProfile,
    MonthEndClose, ProposedJournalEntry, SyncHistory,
    ProposedCategorization, ProposedARMatch, RestaurantSalesData,
    ExternalCredentials,
    _now, get_db, init_db
)
from app.security import decrypt_token, encrypt_token
from app.qbo_client import (
    QBOClient, build_auth_url, exchange_code_for_tokens,
    revoke_token, sync_company_data
)
from app.accounting import (
    calculate_health_score, create_month_end_close, generate_audit_readiness,
    get_portfolio_summary, run_accounting_assessment, update_close_step,
    analyze_revenue, analyze_bank_reconciliation,
    _parse_balance_sheet, _parse_pl, _parse_ar_aging, _parse_ap_aging,
)
from app.bookkeeping import (
    run_categorization, run_ar_matching, run_bank_reconciliation,
    apply_categorization, apply_ar_match,
)
from app.restaurant import (
    fetch_square_sales_for_date, generate_daily_sales_je, save_platform_sales,
    parse_grubhub_csv, parse_ubereats_csv, parse_doordash_csv,
    parse_otter_csv, parse_picnic_csv,
    build_square_auth_url, exchange_square_code, save_square_credentials,
)

import os

# ─── App Setup ────────────────────────────────────────────────

app = FastAPI(
    title="QBO AI Controller",
    description="AI-powered QuickBooks Online accounting controller for multiple companies.",
    version="1.0.0",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.cache = None  # Disable LRU cache to avoid unhashable type errors

# In-memory OAuth state store (use Redis in production)
_oauth_states: dict = {}


@app.on_event("startup")
def startup():
    try:
        init_db()
        print("✓ Database initialized successfully")
    except Exception as exc:
        import traceback
        print(f"⚠ Database init failed — app will start anyway: {exc}")
        traceback.print_exc()

    # ── Column migrations (safe to run every startup) ──────────
    from app.database import engine
    import sqlalchemy as _sa
    _migrations = [
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS company_type VARCHAR(30) DEFAULT 'standard'",
    ]
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as _conn:
            for _sql in _migrations:
                try:
                    _conn.execute(_sa.text(_sql))
                    print(f"✓ Migration OK: {_sql[:60]}")
                except Exception as _e:
                    print(f"⚠ Migration skip: {_e}")
    except Exception as _e:
        print(f"⚠ Migration connection error: {_e}")


# ─── Jinja2 Filters ───────────────────────────────────────────

def fmt_currency(value):
    if value is None:
        return "$0.00"
    try:
        v = float(value)
        neg = v < 0
        return f"({'$' + f'{abs(v):,.2f}'})" if neg else f"${v:,.2f}"
    except Exception:
        return str(value)


def fmt_date(value):
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    return value.strftime("%b %d, %Y")


def severity_badge(sev: str) -> str:
    colors = {"critical": "danger", "high": "warning", "medium": "info", "low": "secondary"}
    return colors.get(sev, "secondary")


import json as _json

templates.env.filters["currency"] = fmt_currency
templates.env.filters["fmt_date"] = fmt_date
templates.env.filters["severity_badge"] = severity_badge
templates.env.filters["tojson"] = lambda v, indent=None: _json.dumps(v, indent=indent, default=str)


# ─── Helpers ──────────────────────────────────────────────────

def get_company_or_404(db: Session, realm_id: str) -> Company:
    company = db.query(Company).filter_by(realm_id=realm_id).first()
    if not company:
        raise HTTPException(status_code=404, detail=f"Company with realm_id {realm_id} not found")
    return company


def get_profile(db: Session, realm_id: str) -> Optional[CompanyProfile]:
    return db.query(CompanyProfile).filter_by(realm_id=realm_id).first()


# ─── CONTROLLER PORTFOLIO DASHBOARD ──────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    companies = db.query(Company).order_by(Company.company_name).all()
    portfolio = get_portfolio_summary(db, companies)
    return templates.TemplateResponse(request, "dashboard.html", {
        "portfolio": portfolio,
        "total_companies": len(companies),
        "connected": sum(1 for c in companies if c.connection_status == "connected"),
        "qbo_environment": settings.QBO_ENVIRONMENT,
        "app_name": settings.APP_NAME,
    })


# ─── COMPANY WORKSPACE ───────────────────────────────────────

@app.get("/company/{realm_id}", response_class=HTMLResponse)
def company_workspace(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    issues = db.query(AccountingIssue).filter_by(
        realm_id=realm_id, status="open"
    ).order_by(AccountingIssue.created_at.desc()).all()

    health = calculate_health_score(profile, issues) if profile else {
        "score": None, "label": "NOT YET ASSESSED", "sublabel": "Sync QBO data, then run Assessment",
        "color": "secondary", "category_scores": {}, "assessed": False,
    }
    # NEVER auto-save health score on page load — only save when assessment explicitly runs

    pending_jes = db.query(ProposedJournalEntry).filter_by(
        realm_id=realm_id, approval_status="pending"
    ).count()

    recent_sync = db.query(SyncHistory).filter_by(
        realm_id=realm_id
    ).order_by(SyncHistory.started_at.desc()).first()

    month_close = db.query(MonthEndClose).filter_by(
        realm_id=realm_id
    ).order_by(MonthEndClose.period.desc()).first()

    bs = _parse_balance_sheet(profile.balance_sheet_data or {}) if profile else {}
    pl = _parse_pl(profile.pl_data or {}) if profile else {}
    ar = _parse_ar_aging(profile.ar_aging_data or {}) if profile else {}
    ap = _parse_ap_aging(profile.ap_aging_data or {}) if profile else {}

    severity_counts = {
        "critical": sum(1 for i in issues if i.severity == "critical"),
        "high": sum(1 for i in issues if i.severity == "high"),
        "medium": sum(1 for i in issues if i.severity == "medium"),
        "low": sum(1 for i in issues if i.severity == "low"),
    }

    return templates.TemplateResponse(request, "company.html", {
        "company": company,
        "profile": profile,
        "health": health,
        "issues": issues[:20],
        "severity_counts": severity_counts,
        "pending_jes": pending_jes,
        "recent_sync": recent_sync,
        "month_close": month_close,
        "bs": bs,
        "pl": pl,
        "ar": ar,
        "ap": ap,
        "app_name": settings.APP_NAME,
        "data_period_start": datetime.now().strftime("%Y-01-01"),
        "data_period_end": datetime.now().strftime("%Y-%m-%d"),
    })


# ─── QBO OAUTH FLOW ──────────────────────────────────────────

@app.get("/qbo/connect", response_class=HTMLResponse)
def qbo_connect_form(request: Request):
    return templates.TemplateResponse(request, "connect.html", {
        "app_name": settings.APP_NAME,
        "environment": settings.QBO_ENVIRONMENT,
        "has_credentials": bool(settings.QBO_CLIENT_ID and settings.QBO_CLIENT_SECRET),
    })


@app.post("/qbo/connect")
def qbo_initiate_oauth(request: Request, company_name: str = Form(...)):
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"company_name": company_name, "created_at": datetime.utcnow()}
    auth_url = build_auth_url(state)
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/qbo/callback")
def qbo_oauth_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    realmId: str = Query(None),
    error: str = Query(None),
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse(url=f"/?error={error}", status_code=302)

    if not state or state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state — possible CSRF attack")

    state_data = _oauth_states.pop(state)
    company_name = state_data.get("company_name", "Unknown Company")

    if not code or not realmId:
        raise HTTPException(status_code=400, detail="Missing authorization code or realm ID from QBO")

    # Exchange code for tokens
    try:
        tokens = exchange_code_for_tokens(code, realmId)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {str(e)}")

    expires_at = datetime.utcnow() + timedelta(seconds=tokens.get("expires_in", 3600))

    # Check if this company is already connected
    existing = db.query(Company).filter_by(realm_id=realmId).first()
    if existing:
        # Update tokens
        existing.access_token_enc = encrypt_token(tokens["access_token"])
        existing.refresh_token_enc = encrypt_token(tokens["refresh_token"])
        existing.token_expires_at = expires_at
        existing.connection_status = "connected"
        existing.qbo_environment = settings.QBO_ENVIRONMENT
        db.commit()
        company = existing
    else:
        # Create new company workspace
        company = Company(
            realm_id=realmId,
            company_name=company_name,
            qbo_environment=settings.QBO_ENVIRONMENT,
            access_token_enc=encrypt_token(tokens["access_token"]),
            refresh_token_enc=encrypt_token(tokens["refresh_token"]),
            token_expires_at=expires_at,
            connection_status="connected",
        )
        db.add(company)
        db.commit()

        # Create empty profile
        profile = CompanyProfile(
            company_id=company.id,
            realm_id=realmId,
        )
        db.add(profile)
        db.commit()

    # Log the connection
    log = ChangeLog(
        company_id=company.id,
        realm_id=realmId,
        action_type="qbo_connect",
        description=f"QBO company connected: {company_name} (Realm: {realmId})",
        human_approval=True,
    )
    db.add(log)
    db.commit()

    return RedirectResponse(url=f"/company/{realmId}?connected=1", status_code=302)


@app.post("/qbo/disconnect/{realm_id}")
def qbo_disconnect(realm_id: str, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    refresh_tok = decrypt_token(company.refresh_token_enc or "")
    if refresh_tok:
        revoke_token(refresh_tok)

    company.access_token_enc = None
    company.refresh_token_enc = None
    company.connection_status = "disconnected"

    log = ChangeLog(
        company_id=company.id,
        realm_id=realm_id,
        action_type="qbo_disconnect",
        description=f"QBO company disconnected: {company.company_name}",
        human_approval=True,
    )
    db.add(log)
    db.commit()
    return RedirectResponse(url="/", status_code=302)


# ─── DATA SYNC ───────────────────────────────────────────────

@app.post("/company/{realm_id}/sync")
def trigger_sync(
    realm_id: str,
    sync_type: str = Form(default="full"),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    if company.connection_status != "connected":
        raise HTTPException(status_code=400, detail="Company is not connected to QBO")

    sync = sync_company_data(db, company, sync_type)
    return RedirectResponse(url=f"/company/{realm_id}?synced=1&status={sync.status}", status_code=302)


@app.get("/api/sync-status/{realm_id}")
def sync_status(realm_id: str, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    syncs = db.query(SyncHistory).filter_by(realm_id=realm_id).order_by(
        SyncHistory.started_at.desc()
    ).limit(5).all()
    return {
        "realm_id": realm_id,
        "company_name": company.company_name,
        "connection_status": company.connection_status,
        "last_sync": company.last_sync.isoformat() if company.last_sync else None,
        "last_sync_status": company.last_sync_status,
        "recent_syncs": [
            {
                "type": s.sync_type,
                "status": s.status,
                "started_at": s.started_at.isoformat(),
                "records": s.records_retrieved,
                "errors": s.errors,
            }
            for s in syncs
        ],
    }


# ─── ACCOUNTING ASSESSMENT ───────────────────────────────────

@app.post("/company/{realm_id}/assess")
def run_assessment(realm_id: str, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    if not profile:
        raise HTTPException(status_code=400, detail="No data available. Run a sync first.")

    # Clear existing open issues before re-assessment
    db.query(AccountingIssue).filter_by(
        realm_id=realm_id, status="open"
    ).delete()
    db.commit()

    new_issues = run_accounting_assessment(db, company, profile)
    health = calculate_health_score(profile, new_issues)
    profile.health_score = health["score"]
    profile.health_score_updated = _now()
    profile.health_details = health
    db.commit()

    return RedirectResponse(
        url=f"/company/{realm_id}?assessed=1&issues={len(new_issues)}&score={health['score']}",
        status_code=302,
    )


@app.get("/company/{realm_id}/issues", response_class=HTMLResponse)
def view_issues(
    realm_id: str,
    request: Request,
    severity: str = Query(default=None),
    category: str = Query(default=None),
    status: str = Query(default="open"),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    query = db.query(AccountingIssue).filter_by(realm_id=realm_id)
    if status:
        query = query.filter(AccountingIssue.status == status)
    if severity:
        query = query.filter(AccountingIssue.severity == severity)
    if category:
        query = query.filter(AccountingIssue.category == category)

    issues = query.order_by(
        AccountingIssue.severity.desc(),
        AccountingIssue.created_at.desc()
    ).all()

    return templates.TemplateResponse(request, "issues.html", {
        "company": company,
        "issues": issues,
        "filter_severity": severity,
        "filter_category": category,
        "filter_status": status,
        "app_name": settings.APP_NAME,
    })


@app.post("/company/{realm_id}/issues/{issue_id}/resolve")
def resolve_issue(
    realm_id: str,
    issue_id: str,
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    issue = db.query(AccountingIssue).filter_by(
        realm_id=realm_id, issue_id=issue_id
    ).first()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    issue.status = "resolved"
    issue.resolved_at = _now()
    issue.resolution_notes = notes
    db.commit()
    return JSONResponse({"success": True, "issue_id": issue_id})


# ─── JOURNAL ENTRIES (Approval Workflow) ─────────────────────

@app.get("/company/{realm_id}/journal-entries", response_class=HTMLResponse)
def view_journal_entries(
    realm_id: str,
    request: Request,
    status_filter: str = Query(default=None),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    query = db.query(ProposedJournalEntry).filter_by(realm_id=realm_id)
    if status_filter:
        query = query.filter(ProposedJournalEntry.approval_status == status_filter)
    jes = query.order_by(ProposedJournalEntry.created_at.desc()).all()

    return templates.TemplateResponse(request, "journal_entries.html", {
        "company": company,
        "journal_entries": jes,
        "status_filter": status_filter,
        "app_name": settings.APP_NAME,
    })


@app.post("/company/{realm_id}/journal-entries/propose")
def propose_journal_entry(
    realm_id: str,
    je_date: str = Form(...),
    description: str = Form(...),
    reason: str = Form(...),
    materiality: str = Form(default="low"),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    count = db.query(ProposedJournalEntry).filter_by(realm_id=realm_id).count()
    je_number = f"JE-{count + 1:04d}"

    je = ProposedJournalEntry(
        je_number=je_number,
        company_id=company.id,
        realm_id=realm_id,
        je_date=datetime.fromisoformat(je_date),
        description=description,
        reason=reason,
        materiality=materiality,
        approval_status="pending",
        submitted_at=_now(),
        lines=[],
    )
    db.add(je)
    db.commit()
    return RedirectResponse(url=f"/company/{realm_id}/journal-entries", status_code=302)


@app.post("/api/company/{realm_id}/journal-entries/{je_id}/approve")
def approve_journal_entry(
    realm_id: str,
    je_id: str,
    approved_by: str = Form(default="Controller"),
    db: Session = Depends(get_db),
):
    je = db.query(ProposedJournalEntry).filter_by(
        realm_id=realm_id, id=je_id
    ).first()
    if not je:
        raise HTTPException(status_code=404, detail="Journal entry not found")

    je.approval_status = "approved"
    je.approved_by = approved_by
    je.approved_at = _now()

    log = ChangeLog(
        company_id=je.company_id,
        realm_id=realm_id,
        action_type="approve_journal_entry",
        description=f"Journal entry {je.je_number} approved by {approved_by}",
        human_approval=True,
        approving_user=approved_by,
        approval_datetime=_now(),
    )
    db.add(log)
    db.commit()
    return JSONResponse({"success": True, "je_number": je.je_number, "status": "approved"})


@app.post("/api/company/{realm_id}/journal-entries/{je_id}/reject")
def reject_journal_entry(
    realm_id: str,
    je_id: str,
    reason: str = Form(default=""),
    db: Session = Depends(get_db),
):
    je = db.query(ProposedJournalEntry).filter_by(
        realm_id=realm_id, id=je_id
    ).first()
    if not je:
        raise HTTPException(status_code=404, detail="Journal entry not found")

    je.approval_status = "rejected"
    je.rejection_reason = reason
    db.commit()
    return JSONResponse({"success": True, "je_number": je.je_number, "status": "rejected"})


@app.post("/api/company/{realm_id}/journal-entries/{je_id}/execute")
def execute_journal_entry(
    realm_id: str,
    je_id: str,
    db: Session = Depends(get_db),
):
    """Execute an approved journal entry in QBO."""
    je = db.query(ProposedJournalEntry).filter_by(
        realm_id=realm_id, id=je_id
    ).first()
    if not je:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    if je.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Journal entry must be approved before execution")

    company = get_company_or_404(db, realm_id)
    client = QBOClient(company)

    # Build QBO JE payload
    lines = je.lines or []
    qbo_lines = []
    for i, line in enumerate(lines):
        qbo_lines.append({
            "Description": line.get("description", ""),
            "Amount": abs(float(line.get("debit", 0) or line.get("credit", 0))),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": "Debit" if line.get("debit") else "Credit",
                "AccountRef": {"value": line.get("account_id"), "name": line.get("account_name")},
            },
        })

    qbo_payload = {
        "TxnDate": je.je_date.strftime("%Y-%m-%d") if je.je_date else datetime.now().strftime("%Y-%m-%d"),
        "PrivateNote": je.description,
        "Line": qbo_lines,
    }

    try:
        result = client.create_journal_entry(db, qbo_payload)
        qbo_txn = result.get("JournalEntry", {})
        je.qbo_transaction_id = qbo_txn.get("Id")
        je.qbo_transaction_number = qbo_txn.get("DocNumber")
        je.executed_at = _now()
        je.approval_status = "executed"
        je.execution_result = result

        # Verify
        if je.qbo_transaction_id:
            try:
                verify = client.get_transaction(db, "journalentry", je.qbo_transaction_id)
                if verify:
                    je.verification_status = "verified"
                    je.verified_at = _now()
                    je.approval_status = "verified"
            except Exception:
                je.verification_status = "unverified"

        log = ChangeLog(
            company_id=company.id,
            realm_id=realm_id,
            action_type="execute_journal_entry",
            description=f"Journal entry {je.je_number} posted to QBO",
            new_value={"qbo_id": je.qbo_transaction_id},
            api_response=result,
            human_approval=True,
            verification_status=je.verification_status,
        )
        db.add(log)
        db.commit()
        return JSONResponse({"success": True, "qbo_id": je.qbo_transaction_id, "status": je.approval_status})

    except Exception as e:
        je.execution_result = {"error": str(e)}
        db.commit()
        raise HTTPException(status_code=500, detail=f"QBO API error: {str(e)}")


# ─── MONTH-END CLOSE ─────────────────────────────────────────

@app.get("/company/{realm_id}/month-close", response_class=HTMLResponse)
def month_close_view(
    realm_id: str,
    request: Request,
    period: str = Query(default=None),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    if not period:
        period = datetime.now().strftime("%Y-%m")

    close = create_month_end_close(db, company, period)

    all_closes = db.query(MonthEndClose).filter_by(
        realm_id=realm_id
    ).order_by(MonthEndClose.period.desc()).limit(12).all()

    return templates.TemplateResponse(request, "month_close.html", {
        "company": company,
        "close": close,
        "period": period,
        "all_closes": all_closes,
        "app_name": settings.APP_NAME,
    })


@app.post("/api/company/{realm_id}/month-close/{close_id}/step")
def update_close_step_api(
    realm_id: str,
    close_id: str,
    step_num: int = Form(...),
    step_status: str = Form(...),
    notes: str = Form(default=""),
    db: Session = Depends(get_db),
):
    get_company_or_404(db, realm_id)
    close = db.query(MonthEndClose).filter_by(
        id=close_id, realm_id=realm_id
    ).first()
    if not close:
        raise HTTPException(status_code=404, detail="Month-end close not found")

    update_close_step(db, close, step_num, step_status, notes)
    return JSONResponse({
        "success": True,
        "step_num": step_num,
        "status": step_status,
        "close_status": close.status,
    })


# ─── AUDIT READINESS ─────────────────────────────────────────

@app.get("/company/{realm_id}/audit", response_class=HTMLResponse)
def audit_view(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    report = generate_audit_readiness(db, company, profile)

    return templates.TemplateResponse(request, "audit.html", {
        "company": company,
        "report": report,
        "app_name": settings.APP_NAME,
    })


# ─── FINANCIAL REPORTS ───────────────────────────────────────

@app.get("/company/{realm_id}/reports", response_class=HTMLResponse)
def reports_view(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    bs = _parse_balance_sheet(profile.balance_sheet_data or {}) if profile else {}
    pl = _parse_pl(profile.pl_data or {}) if profile else {}
    ar = _parse_ar_aging(profile.ar_aging_data or {}) if profile else {}
    ap = _parse_ap_aging(profile.ap_aging_data or {}) if profile else {}
    revenue = analyze_revenue(profile) if profile else {}

    return templates.TemplateResponse(request, "reports.html", {
        "company": company,
        "profile": profile,
        "bs": bs,
        "pl": pl,
        "ar": ar,
        "ap": ap,
        "revenue": revenue,
        "app_name": settings.APP_NAME,
        "data_as_of": profile.data_as_of if profile else None,
    })


# ─── CHANGE LOG / AUDIT TRAIL ────────────────────────────────

@app.get("/company/{realm_id}/changelog", response_class=HTMLResponse)
def changelog_view(
    realm_id: str,
    request: Request,
    action_type: str = Query(default=None),
    date_from: str = Query(default=None),
    date_to: str = Query(default=None),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    query = db.query(ChangeLog).filter_by(realm_id=realm_id)
    if action_type:
        query = query.filter(ChangeLog.action_type == action_type)
    if date_from:
        try:
            query = query.filter(ChangeLog.action_datetime >= datetime.fromisoformat(date_from))
        except Exception:
            pass
    if date_to:
        try:
            query = query.filter(ChangeLog.action_datetime <= datetime.fromisoformat(date_to + "T23:59:59"))
        except Exception:
            pass

    logs = query.order_by(ChangeLog.action_datetime.desc()).limit(200).all()

    return templates.TemplateResponse(request, "changelog.html", {
        "company": company,
        "logs": logs,
        "filter_action": action_type,
        "filter_date_from": date_from,
        "filter_date_to": date_to,
        "app_name": settings.APP_NAME,
    })


# ─── COMPANY PROFILE ─────────────────────────────────────────

@app.get("/company/{realm_id}/profile", response_class=HTMLResponse)
def profile_view(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    return templates.TemplateResponse(request, "profile.html", {
        "company": company,
        "profile": profile,
        "app_name": settings.APP_NAME,
    })


@app.post("/company/{realm_id}/profile")
def update_profile(
    request: Request,
    realm_id: str,
    industry: str = Form(default=""),
    entity_type: str = Form(default=""),
    accounting_method: str = Form(default=""),
    fiscal_year_start_month: str = Form(default="1"),
    currency: str = Form(default="USD"),
    capitalization_threshold: str = Form(default="2500"),
    payroll_system: str = Form(default=""),
    sales_tax_agency: str = Form(default=""),
    tax_year_end: str = Form(default="December 31"),
    controller_notes: str = Form(default=""),
    approved_decisions: str = Form(default=""),
    db: Session = Depends(get_db),
):
    from fastapi import Request as FRequest
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    if not profile:
        profile = CompanyProfile(company_id=company.id, realm_id=realm_id)
        db.add(profile)

    # Get multi-value merchant_processors from form
    form_data = None
    merchant_processors = []
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        form_data = loop.run_until_complete(request.form())
        merchant_processors = form_data.getlist("merchant_processors")
    except Exception:
        pass

    profile.industry = industry
    profile.entity_type = entity_type
    profile.accounting_method = accounting_method
    profile.payroll_system = payroll_system
    profile.sales_tax_agency = sales_tax_agency
    profile.currency = currency
    profile.merchant_processors = merchant_processors
    profile.controller_notes = controller_notes
    profile.approved_decisions = approved_decisions
    profile.tax_year_end = tax_year_end
    try:
        profile.fiscal_year_start_month = int(fiscal_year_start_month)
    except Exception:
        pass
    try:
        profile.capitalization_threshold = float(capitalization_threshold)
    except Exception:
        pass

    db.commit()
    return RedirectResponse(url=f"/company/{realm_id}/profile?saved=1", status_code=302)


# ─── API: Health Check ────────────────────────────────────────

@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "environment": settings.QBO_ENVIRONMENT,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/company/{realm_id}/summary")
def company_summary_api(realm_id: str, db: Session = Depends(get_db)):
    """JSON summary for a company — for external integrations."""
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    issues = db.query(AccountingIssue).filter_by(realm_id=realm_id, status="open").all()
    health = calculate_health_score(profile, issues) if profile else {
        "score": None, "label": "NOT YET ASSESSED", "assessed": False
    }

    return {
        "company_name": company.company_name,
        "realm_id": company.realm_id,
        "connection_status": company.connection_status,
        "last_sync": company.last_sync.isoformat() if company.last_sync else None,
        "health_score": health.get("score"),           # None when not assessed
        "health_label": health.get("label"),
        "assessed": health.get("assessed", False),
        "open_issues": len(issues),
        "critical_issues": sum(1 for i in issues if i.severity == "critical"),
    }


# ════════════════════════════════════════════════════════════════
# ─── BOOKKEEPING: CATEGORIZATION ────────────────────────────────
# ════════════════════════════════════════════════════════════════

@app.get("/company/{realm_id}/bookkeeping", response_class=HTMLResponse)
def bookkeeping_dashboard(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    cats = db.query(ProposedCategorization).filter_by(
        realm_id=realm_id
    ).order_by(ProposedCategorization.created_at.desc()).limit(200).all()

    pending = [c for c in cats if c.status == "pending"]
    approved = [c for c in cats if c.status == "approved"]
    applied = [c for c in cats if c.status == "applied"]
    rejected = [c for c in cats if c.status == "rejected"]

    return templates.TemplateResponse(request, "bookkeeping.html", {
        "company": company,
        "profile": profile,
        "pending": pending,
        "approved": approved,
        "applied": applied,
        "rejected": rejected,
        "total": len(cats),
        "saved": request.query_params.get("saved"),
    })


@app.post("/company/{realm_id}/categorize")
def run_categorize(
    realm_id: str,
    lookback_days: int = Form(90),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    if not profile:
        raise HTTPException(status_code=400, detail="Sync QBO data first (no profile found)")

    result = run_categorization(db, company, profile, lookback_days=lookback_days)
    db.commit()
    return RedirectResponse(
        url=f"/company/{realm_id}/bookkeeping?saved=Categorization+run:+{result.get('proposed', 0)}+proposals+created",
        status_code=302,
    )


@app.post("/company/{realm_id}/categorization/{cat_id}/approve")
def approve_categorization(realm_id: str, cat_id: int, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    cat = db.query(ProposedCategorization).filter_by(id=cat_id, realm_id=realm_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categorization proposal not found")
    cat.status = "approved"
    cat.approved_at = _now()
    cat.approved_by = "controller"
    db.commit()
    return JSONResponse({"ok": True, "status": "approved"})


@app.post("/company/{realm_id}/categorization/{cat_id}/reject")
def reject_categorization(
    realm_id: str,
    cat_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    cat = db.query(ProposedCategorization).filter_by(id=cat_id, realm_id=realm_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categorization proposal not found")
    cat.status = "rejected"
    cat.rejection_reason = reason
    db.commit()
    return JSONResponse({"ok": True, "status": "rejected"})


@app.post("/company/{realm_id}/categorization/{cat_id}/apply")
def apply_categorization_route(realm_id: str, cat_id: int, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    cat = db.query(ProposedCategorization).filter_by(id=cat_id, realm_id=realm_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Categorization proposal not found")
    if cat.status != "approved":
        raise HTTPException(status_code=400, detail="Proposal must be approved before applying")

    result = apply_categorization(db, company, cat)
    if result.get("success"):
        db.commit()
        return JSONResponse({"ok": True, "status": "applied"})
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))


# ════════════════════════════════════════════════════════════════
# ─── BOOKKEEPING: AR MATCHING ───────────────────────────────────
# ════════════════════════════════════════════════════════════════

@app.get("/company/{realm_id}/ar-matching", response_class=HTMLResponse)
def ar_matching_dashboard(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    matches = db.query(ProposedARMatch).filter_by(
        realm_id=realm_id
    ).order_by(ProposedARMatch.created_at.desc()).limit(200).all()

    pending = [m for m in matches if m.status == "pending"]
    approved = [m for m in matches if m.status == "approved"]
    applied = [m for m in matches if m.status == "applied"]
    rejected = [m for m in matches if m.status == "rejected"]

    return templates.TemplateResponse(request, "ar_matching.html", {
        "company": company,
        "profile": profile,
        "pending": pending,
        "approved": approved,
        "applied": applied,
        "rejected": rejected,
        "total": len(matches),
        "saved": request.query_params.get("saved"),
    })


@app.post("/company/{realm_id}/ar-match")
def run_ar_match(
    realm_id: str,
    lookback_days: int = Form(180),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    if not profile:
        raise HTTPException(status_code=400, detail="Sync QBO data first (no profile found)")

    result = run_ar_matching(db, company, profile, lookback_days=lookback_days)
    db.commit()
    return RedirectResponse(
        url=f"/company/{realm_id}/ar-matching?saved=AR+Match+run:+{result.get('proposed', 0)}+proposals",
        status_code=302,
    )


@app.post("/company/{realm_id}/ar-match/{match_id}/approve")
def approve_ar_match(realm_id: str, match_id: int, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    match = db.query(ProposedARMatch).filter_by(id=match_id, realm_id=realm_id).first()
    if not match:
        raise HTTPException(status_code=404, detail="AR match proposal not found")
    match.status = "approved"
    match.approved_at = _now()
    match.approved_by = "controller"
    db.commit()
    return JSONResponse({"ok": True, "status": "approved"})


@app.post("/company/{realm_id}/ar-match/{match_id}/reject")
def reject_ar_match(
    realm_id: str,
    match_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    match = db.query(ProposedARMatch).filter_by(id=match_id, realm_id=realm_id).first()
    if not match:
        raise HTTPException(status_code=404, detail="AR match proposal not found")
    match.status = "rejected"
    match.rejection_reason = reason
    db.commit()
    return JSONResponse({"ok": True, "status": "rejected"})


@app.post("/company/{realm_id}/ar-match/{match_id}/apply")
def apply_ar_match_route(realm_id: str, match_id: int, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    match = db.query(ProposedARMatch).filter_by(id=match_id, realm_id=realm_id).first()
    if not match:
        raise HTTPException(status_code=404, detail="AR match proposal not found")
    if match.status != "approved":
        raise HTTPException(status_code=400, detail="Match must be approved before applying")

    result = apply_ar_match(db, company, match)
    if result.get("success"):
        db.commit()
        return JSONResponse({"ok": True, "status": "applied"})
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))


# ════════════════════════════════════════════════════════════════
# ─── BOOKKEEPING: BANK RECONCILIATION ───────────────────────────
# ════════════════════════════════════════════════════════════════

@app.get("/company/{realm_id}/bank-rec", response_class=HTMLResponse)
def bank_rec_page(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    # Pull list of bank/credit-card accounts from profile CoA if available
    accounts = []
    if profile and profile.chart_of_accounts_data:
        coa = profile.chart_of_accounts_data.get("QueryResponse", {}).get("Account", [])
        accounts = [
            a for a in coa
            if a.get("AccountType") in ("Bank", "Credit Card")
            and a.get("Active", True)
        ]

    return templates.TemplateResponse(request, "bank_rec.html", {
        "company": company,
        "profile": profile,
        "accounts": accounts,
        "result": None,
        "saved": request.query_params.get("saved"),
    })


@app.post("/company/{realm_id}/bank-rec", response_class=HTMLResponse)
def run_bank_rec(
    realm_id: str,
    request: Request,
    account_id: str = Form(...),
    account_name: str = Form(""),
    statement_end_date: str = Form(...),
    statement_ending_balance: float = Form(...),
    db: Session = Depends(get_db),
):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    if not profile:
        raise HTTPException(status_code=400, detail="Sync QBO data first")

    result = run_bank_reconciliation(
        db, company, profile,
        account_id=account_id,
        statement_end_date=statement_end_date,
        statement_ending_balance=statement_ending_balance,
    )

    accounts = []
    if profile.chart_of_accounts_data:
        coa = profile.chart_of_accounts_data.get("QueryResponse", {}).get("Account", [])
        accounts = [
            a for a in coa
            if a.get("AccountType") in ("Bank", "Credit Card")
            and a.get("Active", True)
        ]

    return templates.TemplateResponse(request, "bank_rec.html", {
        "company": company,
        "profile": profile,
        "accounts": accounts,
        "result": result,
        "selected_account_id": account_id,
        "selected_account_name": account_name,
        "statement_end_date": statement_end_date,
        "statement_ending_balance": statement_ending_balance,
        "saved": None,
    })


# ════════════════════════════════════════════════════════════════
# ─── RESTAURANT: DAILY SALES & JOURNAL ENTRIES ──────────────────
# ════════════════════════════════════════════════════════════════

@app.get("/company/{realm_id}/restaurant", response_class=HTMLResponse)
def restaurant_dashboard(realm_id: str, request: Request, db: Session = Depends(get_db)):
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)

    # Recent 30 days of sales data
    from datetime import date as _date
    cutoff = datetime.utcnow() - timedelta(days=30)
    sales = db.query(RestaurantSalesData).filter(
        RestaurantSalesData.realm_id == realm_id,
        RestaurantSalesData.created_at >= cutoff,
    ).order_by(RestaurantSalesData.sales_date.desc(), RestaurantSalesData.platform).all()

    # Group by date for summary view
    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for s in sales:
        by_date[str(s.sales_date)].append(s)

    # External credentials (connected platforms)
    creds = db.query(ExternalCredentials).filter_by(realm_id=realm_id).all()
    connected_platforms = {c.platform: c for c in creds}

    # Square OAuth URL
    square_auth_url = build_square_auth_url(realm_id) if "square" not in connected_platforms else None

    return templates.TemplateResponse(request, "restaurant.html", {
        "company": company,
        "profile": profile,
        "by_date": dict(by_date),
        "connected_platforms": connected_platforms,
        "square_auth_url": square_auth_url,
        "saved": request.query_params.get("saved"),
        "error": request.query_params.get("error"),
    })


@app.post("/company/{realm_id}/restaurant/fetch-square")
def fetch_square_sales(
    realm_id: str,
    sales_date: str = Form(...),
    db: Session = Depends(get_db),
):
    """Fetch Square sales for a specific date via API."""
    company = get_company_or_404(db, realm_id)
    cred = db.query(ExternalCredentials).filter_by(
        realm_id=realm_id, platform="square"
    ).first()
    if not cred:
        raise HTTPException(status_code=400, detail="Square not connected. Connect Square first.")

    try:
        result = fetch_square_sales_for_date(db, company, cred, sales_date)
        db.commit()
        return RedirectResponse(
            url=f"/company/{realm_id}/restaurant?saved=Square+data+fetched+for+{sales_date}",
            status_code=302,
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/company/{realm_id}/restaurant?error={str(exc)[:120]}",
            status_code=302,
        )


@app.post("/company/{realm_id}/restaurant/import-csv")
async def import_platform_csv(
    realm_id: str,
    request: Request,
    platform: str = Form(...),
    sales_date: str = Form(...),
    db: Session = Depends(get_db),
):
    """Upload a CSV report from a delivery platform."""
    from fastapi import UploadFile, File
    company = get_company_or_404(db, realm_id)

    form = await request.form()
    csv_file = form.get("csv_file")
    if not csv_file or not hasattr(csv_file, "read"):
        raise HTTPException(status_code=400, detail="No CSV file provided")

    content = (await csv_file.read()).decode("utf-8", errors="replace")

    parsers = {
        "grubhub": parse_grubhub_csv,
        "uber_eats": parse_ubereats_csv,
        "doordash": parse_doordash_csv,
        "otter": parse_otter_csv,
        "picnic": parse_picnic_csv,
    }
    parser = parsers.get(platform)
    if not parser:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    try:
        data = parser(content, sales_date)
    except Exception as exc:
        return RedirectResponse(
            url=f"/company/{realm_id}/restaurant?error=CSV+parse+error:+{str(exc)[:100]}",
            status_code=302,
        )

    save_platform_sales(db, company, data)
    db.commit()
    return RedirectResponse(
        url=f"/company/{realm_id}/restaurant?saved={platform}+CSV+imported+for+{sales_date}",
        status_code=302,
    )


@app.post("/company/{realm_id}/restaurant/generate-je/{sales_date}")
def generate_restaurant_je(
    realm_id: str,
    sales_date: str,
    db: Session = Depends(get_db),
):
    """Generate a proposed journal entry for the specified sales date."""
    company = get_company_or_404(db, realm_id)
    profile = get_profile(db, realm_id)
    if not profile:
        raise HTTPException(status_code=400, detail="Sync QBO data first")

    result = generate_daily_sales_je(db, company, profile, sales_date)
    if result.get("error"):
        return RedirectResponse(
            url=f"/company/{realm_id}/restaurant?error={result['error'][:120]}",
            status_code=302,
        )
    db.commit()
    return RedirectResponse(
        url=f"/company/{realm_id}/restaurant?saved=Journal+entry+{result.get('je_number', '')}+proposed+for+{sales_date}",
        status_code=302,
    )


# ─── Square OAuth ──────────────────────────────────────────────

@app.get("/auth/square/callback")
def square_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    if error:
        return HTMLResponse(f"<h3>Square authorization failed: {error}</h3>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h3>Missing code or state parameter</h3>", status_code=400)

    # state encodes realm_id
    realm_id = state
    company = db.query(Company).filter_by(realm_id=realm_id).first()
    if not company:
        return HTMLResponse("<h3>Company not found</h3>", status_code=404)

    try:
        token_data = exchange_square_code(code)
        save_square_credentials(db, company, token_data)
        db.commit()
        return RedirectResponse(
            url=f"/company/{realm_id}/restaurant?saved=Square+connected+successfully",
            status_code=302,
        )
    except Exception as exc:
        return HTMLResponse(f"<h3>Error connecting Square: {exc}</h3>", status_code=500)
