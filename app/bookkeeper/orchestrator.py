"""Daily bookkeeping cycle for one realm.

CONNECT, then SYNC when the snapshot is missing or older than 24 hours,
then INSPECT, DETECT, ANALYZE, and PROPOSE. Approval and execution stay
with a person. This run does not post to QBO.
"""
from datetime import datetime

from app.accounting import run_accounting_assessment
from app.bookkeeper.freshness import describe_snapshot, live_read
from app.bookkeeper.knowledge import focus_for
from app.bookkeeper.review import (
    review_ap,
    review_bank_reconcile,
    review_credit_cards,
    review_payroll,
    review_sales_tax,
    review_statements,
    safe_review,
)
from app.bookkeeping import run_ar_matching, run_categorization_v2
from app.qbo_client import sync_company_data

MODULE_ORDER = (
    "banking",
    "credit_cards",
    "ar",
    "ap",
    "payroll",
    "sales_tax",
    "reconcile",
    "statements",
    "assessment",
)


def _selected(modules):
    if not modules:
        return set(MODULE_ORDER)
    wanted = {name.strip().lower() for name in modules if name and name.strip()}
    unknown = wanted - set(MODULE_ORDER)
    if unknown:
        raise ValueError("Unknown module: " + ", ".join(sorted(unknown)))
    return wanted


def _fail(name, message, source, freshness):
    return {
        "module": name,
        "status": "FAILED",
        "message": message,
        "work_items_created": 0,
        "source": source,
        "freshness": freshness,
    }


def run_daily_bookkeeping(db, company, profile, lookback_days=90, modules=None):
    """Run the cycle for this company only. The caller commits."""
    selected = _selected(modules)
    industry = getattr(profile, "industry", None) if profile else None
    business_type, focus = focus_for(industry)
    report = {
        "realm_id": company.realm_id,
        "company_name": company.company_name,
        "business_type": business_type,
        "focus": list(focus),
        "qbo_writes": 0,
        "auto_executed": 0,
        "auto_note": "AUTO execution is off. Proposals stay pending until a person approves them.",
        "stages": [],
        "modules": {},
        "work_items_created": 0,
    }

    if company.connection_status != "connected":
        report["status"] = "BLOCKED"
        report["stages"].append({
            "stage": "CONNECT",
            "status": "FAILED",
            "message": "This company is not connected to QBO.",
        })
        return report

    report["stages"].append({
        "stage": "CONNECT",
        "status": "COMPLETED",
        "message": "Connected. Realm " + company.realm_id + ".",
    })

    snapshot = describe_snapshot(company, profile)
    synced_now = False
    if snapshot["freshness"] == "MISSING" or snapshot["stale"]:
        try:
            sync_company_data(db, company, sync_type="daily")
            db.refresh(company)
            if profile is not None:
                db.refresh(profile)
            snapshot = describe_snapshot(company, profile)
            synced_now = True
            sync_message = "Snapshot refreshed from QBO before review."
        except Exception as exc:
            sync_message = "Sync failed: " + str(exc) + ". Review continues on the last snapshot where one exists."
    else:
        sync_message = "Snapshot is under 24 hours old. Transaction modules still read QBO live."

    report["snapshot"] = snapshot
    report["stages"].append({
        "stage": "SYNC",
        "status": "COMPLETED" if snapshot["freshness"] != "MISSING" else "FAILED",
        "message": sync_message,
        "refreshed": synced_now,
        "freshness": snapshot["freshness"],
        "source": snapshot["source"],
    })

    if "banking" in selected and profile is not None:
        try:
            _items, diagnostic = run_categorization_v2(db, company, profile, lookback_days=lookback_days)
            banking = (diagnostic.get("module_results") or {}).get("BANKING", {})
            created = int(diagnostic.get("work_items_created") or 0)
            report["modules"]["banking"] = {
                "module": "BANKING",
                "status": banking.get("status") or diagnostic.get("engine_status") or "COMPLETED",
                "message": banking.get("message") or "",
                "work_items_created": created,
                "source": "LIVE_QBO",
                "freshness": "LIVE",
                "freshness_label": live_read("Purchase, Deposit, SalesReceipt")["label"],
                "diagnostic": {
                    "reviewed": diagnostic.get("posted_transactions_reviewed", 0),
                    "uncategorized": diagnostic.get("items_uncategorized", 0),
                    "needs_review": diagnostic.get("items_needing_review", 0),
                    "qbo_writes": diagnostic.get("qbo_writes", 0),
                    "autonomy": diagnostic.get("autonomy_breakdown", {}),
                },
            }
            report["work_items_created"] += created
        except Exception as exc:
            report["modules"]["banking"] = _fail("BANKING", str(exc), "LIVE_QBO", "LIVE")

    if "ar" in selected and profile is not None:
        try:
            matches = run_ar_matching(db, company, profile, lookback_days=max(lookback_days, 180))
            report["modules"]["ar"] = {
                "module": "AR",
                "status": "COMPLETED",
                "message": "Live QBO: " + str(len(matches)) + " payment-to-invoice proposal(s). None were applied.",
                "work_items_created": len(matches),
                "source": "LIVE_QBO",
                "freshness": "LIVE",
                "freshness_label": live_read("Invoice and Payment")["label"],
            }
            report["work_items_created"] += len(matches)
        except Exception as exc:
            report["modules"]["ar"] = _fail("AR", str(exc), "LIVE_QBO", "LIVE")

    runners = (
        ("credit_cards", lambda: review_credit_cards(db, company, profile)),
        ("ap", lambda: review_ap(db, company)),
        ("payroll", lambda: review_payroll(db, company, profile)),
        ("sales_tax", lambda: review_sales_tax(db, company, profile)),
        ("reconcile", lambda: review_bank_reconcile(db, company, profile)),
        ("statements", lambda: review_statements(db, company, profile)),
    )
    for name, runner in runners:
        if name not in selected:
            continue
        outcome = safe_review(runner)
        if outcome.get("module") in (None, "", "<lambda>"):
            outcome["module"] = name.upper()
        report["modules"][name] = outcome
        report["work_items_created"] += int(outcome.get("work_items_created") or 0)

    if "assessment" in selected and profile is not None and getattr(profile, "data_as_of", None):
        try:
            issues = run_accounting_assessment(db, company, profile)
            report["modules"]["assessment"] = {
                "module": "ASSESSMENT",
                "status": "COMPLETED",
                "message": str(len(issues)) + " open finding(s) from the cached snapshot.",
                "work_items_created": 0,
                "issue_count": len(issues),
                "source": "CACHED_SYNC",
                "freshness": describe_snapshot(company, profile)["freshness"],
                "freshness_label": "Assessment reads the last sync. It does not call QBO.",
            }
        except Exception as exc:
            report["modules"]["assessment"] = _fail("ASSESSMENT", str(exc), "CACHED_SYNC", "CACHED")

    report["stages"].append({
        "stage": "INSPECT",
        "status": "COMPLETED",
        "message": "Selected modules read this company's books.",
    })
    report["stages"].append({
        "stage": "PROPOSE",
        "status": "COMPLETED",
        "message": str(report["work_items_created"]) + " proposal(s) waiting. No QBO write.",
    })
    report["stages"].append({
        "stage": "APPROVE",
        "status": "WAITING",
        "message": "A person approves from the work queue.",
    })

    statuses = [item.get("status") for item in report["modules"].values()]
    if any(item == "FAILED" for item in statuses) and any(
        item and str(item).startswith("COMPLETED") for item in statuses
    ):
        report["status"] = "PARTIAL"
    elif statuses and all(item == "FAILED" for item in statuses):
        report["status"] = "FAILED"
    else:
        report["status"] = "COMPLETED"
    report["finished_at"] = datetime.utcnow().isoformat() + "Z"
    return report
