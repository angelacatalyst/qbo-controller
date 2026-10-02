"""
tests/test_route_fixes_m1a.py
──────────────────────────────
Milestone 1A — Route-Fix Verification Tests

Covers the two routes corrected during the Milestone 1A security review:
  A. GET  /qbo/reconnect/{realm_id}         → now requires require_admin
  B. POST /company/{realm_id}/set-active    → now requires require_admin

Test matrix per Fase 3 requirements:
  1. Unauthenticated  → 401
  2. Authenticated non-admin (controller) → 403
  3. Admin → allowed (3xx for reconnect, 3xx for set-active)
  4. set-active cross-company: verify only the target company becomes active;
     OTHER companies' is_active is cleared (that is the intended global-flag semantics)
  5. set-active: non-admin cannot change active state of any company (403)
  6. reconnect: OAuth state is written to DB only when admin is authenticated
  7. Write Guard: execute routes still guard via check_realm_access (not affected by fixes)

Uses the same in-memory SQLite + StaticPool pattern as test_auth_enforcement.py.
No mocking of auth logic — production dependency wiring used throughout.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import (
    Base, User, UserSession, Company, CompanyAccess,
    OAuthState, get_db,
)
from app.security import hash_password, generate_session_token, hash_session_token, session_expiry
from app.main import app

# ════════════════════════════════════════════════════════════════
# ─── ISOLATED IN-MEMORY DB ───────────────────────────────────
# ════════════════════════════════════════════════════════════════

# Engine, _Session, and get_db override are set up centrally in conftest.py.
from tests.conftest import _Session  # noqa: E402

# follow_redirects=False so we can assert 302 without actually hitting "/"
client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)


# ════════════════════════════════════════════════════════════════
# ─── FIXTURE HELPERS ────────────────────────────────────────
# ════════════════════════════════════════════════════════════════

def _db():
    return _Session()


def _create_user(db, email, role="controller"):
    u = User(
        email=email,
        hashed_password=hash_password("TestPass123!"),
        full_name=f"Test {role.title()}",
        role=role,
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _create_session(db, user):
    raw = generate_session_token()
    s = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(raw),
        expires_at=session_expiry(),
        is_active=True,
    )
    db.add(s)
    db.commit()
    return raw


def _create_company(db, realm_id, company_name, is_active=False):
    c = Company(
        realm_id=realm_id,
        company_name=company_name,
        qbo_environment="sandbox",
        connection_status="connected",
        is_active=is_active,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _grant_access(db, user, company):
    ca = CompanyAccess(
        user_id=user.id,
        company_id=company.id,
        realm_id=company.realm_id,
        role="controller",
        is_active=True,
    )
    db.add(ca)
    db.commit()


# ════════════════════════════════════════════════════════════════
# ─── SHARED FIXTURES (module-level, created once) ────────────
# ════════════════════════════════════════════════════════════════

_FIXTURES: dict = {}


def _ensure_fixtures():
    if _FIXTURES:
        return
    db = _db()

    admin = _create_user(db, "admin-fixes@test.local", "admin")
    controller = _create_user(db, "ctrl-fixes@test.local", "controller")

    # Two companies in separate realms
    company_a = _create_company(db, "REALM_FIX_A", "FixCo Alpha", is_active=True)
    company_b = _create_company(db, "REALM_FIX_B", "FixCo Beta", is_active=False)

    # Grant controller access ONLY to company_a
    _grant_access(db, controller, company_a)

    admin_token = _create_session(db, admin)
    ctrl_token = _create_session(db, controller)

    # Store plain values BEFORE closing db to avoid DetachedInstanceError
    admin_id = admin.id
    ctrl_id = controller.id
    realm_a_id = company_a.realm_id
    realm_b_id = company_b.realm_id
    co_a_id = company_a.id
    co_b_id = company_b.id

    db.close()

    _FIXTURES.update({
        "admin_id": admin_id,
        "ctrl_id": ctrl_id,
        "realm_a": realm_a_id,
        "realm_b": realm_b_id,
        "company_a_id": co_a_id,
        "company_b_id": co_b_id,
        "admin_token": admin_token,
        "ctrl_token": ctrl_token,
    })


# ════════════════════════════════════════════════════════════════
# A. GET /qbo/reconnect/{realm_id}
# ════════════════════════════════════════════════════════════════

class TestQboReconnectAuth(unittest.TestCase):
    """Fix A — require_admin on GET /qbo/reconnect/{realm_id}"""

    def setUp(self):
        _ensure_fixtures()

    def test_reconnect_unauthenticated_returns_401(self):
        """No session cookie → 401 before any business logic."""
        realm = _FIXTURES["realm_a"]
        resp = client.get(f"/qbo/reconnect/{realm}")
        self.assertEqual(resp.status_code, 401,
            "Unauthenticated reconnect must return 401")

    def test_reconnect_non_admin_returns_403(self):
        """Authenticated controller (non-admin) → 403."""
        realm = _FIXTURES["realm_a"]
        tok = _FIXTURES["ctrl_token"]
        resp = client.get(
            f"/qbo/reconnect/{realm}",
            cookies={"qbo_session": tok},
        )
        self.assertEqual(resp.status_code, 403,
            "Controller must be denied reconnect (admin only)")

    def test_reconnect_admin_allowed(self):
        """Admin → allowed; response is a redirect (3xx) to Intuit."""
        realm = _FIXTURES["realm_a"]
        tok = _FIXTURES["admin_token"]
        resp = client.get(
            f"/qbo/reconnect/{realm}",
            cookies={"qbo_session": tok},
        )
        self.assertIn(resp.status_code, (301, 302, 307, 308),
            f"Admin reconnect must redirect; got {resp.status_code}")

    def test_reconnect_admin_writes_oauth_state(self):
        """Admin reconnect must persist an OAuthState row — not skip auth and write nothing."""
        realm = _FIXTURES["realm_a"]
        tok = _FIXTURES["admin_token"]

        db = _db()
        count_before = db.query(OAuthState).count()
        db.close()

        resp = client.get(
            f"/qbo/reconnect/{realm}",
            cookies={"qbo_session": tok},
        )
        self.assertIn(resp.status_code, (301, 302, 307, 308))

        db = _db()
        count_after = db.query(OAuthState).count()
        db.close()

        self.assertGreater(count_after, count_before,
            "Successful admin reconnect must write an OAuthState row")

    def test_reconnect_controller_writes_no_oauth_state(self):
        """Rejected non-admin must NOT write an OAuthState row."""
        realm = _FIXTURES["realm_a"]
        tok = _FIXTURES["ctrl_token"]

        db = _db()
        count_before = db.query(OAuthState).count()
        db.close()

        client.get(f"/qbo/reconnect/{realm}", cookies={"qbo_session": tok})

        db = _db()
        count_after = db.query(OAuthState).count()
        db.close()

        self.assertEqual(count_before, count_after,
            "Rejected reconnect must NOT write an OAuthState row")

    def test_reconnect_unauthenticated_writes_no_oauth_state(self):
        """Rejected unauthenticated call must NOT write an OAuthState row."""
        realm = _FIXTURES["realm_a"]

        db = _db()
        count_before = db.query(OAuthState).count()
        db.close()

        client.get(f"/qbo/reconnect/{realm}")

        db = _db()
        count_after = db.query(OAuthState).count()
        db.close()

        self.assertEqual(count_before, count_after,
            "Unauthenticated reconnect must NOT write an OAuthState row")


# ════════════════════════════════════════════════════════════════
# B. POST /company/{realm_id}/set-active
# ════════════════════════════════════════════════════════════════

class TestSetActiveAuth(unittest.TestCase):
    """Fix B — require_admin on POST /company/{realm_id}/set-active"""

    def setUp(self):
        _ensure_fixtures()

    def test_set_active_unauthenticated_returns_401(self):
        """No session cookie → 401."""
        realm = _FIXTURES["realm_a"]
        resp = client.post(f"/company/{realm}/set-active")
        self.assertEqual(resp.status_code, 401,
            "Unauthenticated set-active must return 401")

    def test_set_active_non_admin_returns_403(self):
        """Authenticated controller → 403 (cannot change global active flag)."""
        realm = _FIXTURES["realm_a"]
        tok = _FIXTURES["ctrl_token"]
        resp = client.post(
            f"/company/{realm}/set-active",
            cookies={"qbo_session": tok},
        )
        self.assertEqual(resp.status_code, 403,
            "Controller must not be able to set the global active company")

    def test_set_active_admin_allowed(self):
        """Admin → allowed; response is a redirect to /."""
        realm = _FIXTURES["realm_b"]
        tok = _FIXTURES["admin_token"]
        resp = client.post(
            f"/company/{realm}/set-active",
            cookies={"qbo_session": tok},
        )
        self.assertIn(resp.status_code, (301, 302, 307, 308),
            f"Admin set-active must redirect; got {resp.status_code}")

    def test_set_active_only_sets_target_active(self):
        """
        Admin sets Realm B active:
        - company_b.is_active must become True
        - company_a.is_active must become False (global-flag semantics)
        Verifies the intended behavior: at most one active company at a time.
        """
        realm_a = _FIXTURES["realm_a"]
        realm_b = _FIXTURES["realm_b"]
        tok = _FIXTURES["admin_token"]
        company_a_id = _FIXTURES["company_a_id"]
        company_b_id = _FIXTURES["company_b_id"]

        # First ensure company_a is currently active, company_b is not
        db = _db()
        db.query(Company).filter(Company.id == company_a_id).update({Company.is_active: True})
        db.query(Company).filter(Company.id == company_b_id).update({Company.is_active: False})
        db.commit()
        db.close()

        # Admin sets realm_b as active
        resp = client.post(
            f"/company/{realm_b}/set-active",
            cookies={"qbo_session": tok},
        )
        self.assertIn(resp.status_code, (301, 302, 307, 308))

        db = _db()
        ca = db.query(Company).filter(Company.id == company_a_id).first()
        cb = db.query(Company).filter(Company.id == company_b_id).first()
        db.close()

        self.assertFalse(ca.is_active,
            "company_a.is_active must be False after set-active on company_b")
        self.assertTrue(cb.is_active,
            "company_b.is_active must be True after set-active on company_b")

    def test_set_active_non_admin_does_not_mutate_db(self):
        """
        A rejected non-admin call must not change any company's is_active flag.
        """
        realm_a = _FIXTURES["realm_a"]
        company_a_id = _FIXTURES["company_a_id"]
        company_b_id = _FIXTURES["company_b_id"]
        tok = _FIXTURES["ctrl_token"]

        # Set known state: a=True, b=False
        db = _db()
        db.query(Company).filter(Company.id == company_a_id).update({Company.is_active: True})
        db.query(Company).filter(Company.id == company_b_id).update({Company.is_active: False})
        db.commit()
        before_a = db.query(Company).filter(Company.id == company_a_id).first().is_active
        before_b = db.query(Company).filter(Company.id == company_b_id).first().is_active
        db.close()

        # Non-admin tries to set company_a as active
        resp = client.post(
            f"/company/{realm_a}/set-active",
            cookies={"qbo_session": tok},
        )
        self.assertEqual(resp.status_code, 403)

        db = _db()
        after_a = db.query(Company).filter(Company.id == company_a_id).first().is_active
        after_b = db.query(Company).filter(Company.id == company_b_id).first().is_active
        db.close()

        self.assertEqual(before_a, after_a,
            "Rejected call must not change company_a.is_active")
        self.assertEqual(before_b, after_b,
            "Rejected call must not change company_b.is_active")


if __name__ == "__main__":
    unittest.main()


# ════════════════════════════════════════════════════════════════
# C. POST /clients/{client_id}/set-active/{realm_id}
# ════════════════════════════════════════════════════════════════

from app.database import Client  # noqa: E402

_CLIENT_FIXTURES: dict = {}


def _ensure_client_fixtures():
    """
    Build fixtures for the per-client active-realm tests.
    Relies on _ensure_fixtures() having already run (same in-memory DB).
    Creates one Client and links both existing companies to it.
    """
    if _CLIENT_FIXTURES:
        return
    _ensure_fixtures()  # guarantees realm_a / realm_b companies exist

    realm_a = _FIXTURES["realm_a"]
    realm_b = _FIXTURES["realm_b"]

    db = _db()

    # Create a Client
    test_client = Client(client_name="FixCo Test Client", status="active")
    db.add(test_client)
    db.flush()   # get the id without committing

    # Link company_a → client (realm_a)
    db.query(Company).filter_by(realm_id=realm_a).update({"client_id": test_client.id})
    # Link company_b → client (realm_b)
    db.query(Company).filter_by(realm_id=realm_b).update({"client_id": test_client.id})

    db.commit()
    client_id = test_client.id
    db.close()

    _CLIENT_FIXTURES.update({"client_id": client_id})


class TestSetActiveRealmAuth(unittest.TestCase):
    """
    Fix C — check_realm_access on POST /clients/{client_id}/set-active/{realm_id}

    Test matrix (per Angela's final request):
      1. Unauthenticated               → 401
      2. Authenticated, no CompanyAccess to realm → 403
      3. Authorized user (has CompanyAccess to realm) → 302
      4. Authorized user, but realm_id belongs to a company they cannot access → 403
    """

    def setUp(self):
        _ensure_fixtures()
        _ensure_client_fixtures()

    # ── 1. Unauthenticated → 401 ────────────────────────────────

    def test_set_active_realm_unauthenticated_returns_401(self):
        """No session cookie → 401 before any business logic."""
        cid = _CLIENT_FIXTURES["client_id"]
        realm = _FIXTURES["realm_a"]
        resp = client.post(f"/clients/{cid}/set-active/{realm}")
        self.assertEqual(resp.status_code, 401,
            "Unauthenticated request must return 401")

    # ── 2. Authenticated, no access to that realm → 403 ────────

    def test_set_active_realm_no_access_returns_403(self):
        """
        Controller is authenticated but has NO CompanyAccess to realm_b.
        check_realm_access must reject with 403 before business logic runs.
        """
        cid = _CLIENT_FIXTURES["client_id"]
        realm = _FIXTURES["realm_b"]          # ctrl has no access to realm_b
        tok = _FIXTURES["ctrl_token"]
        resp = client.post(
            f"/clients/{cid}/set-active/{realm}",
            cookies={"qbo_session": tok},
        )
        self.assertEqual(resp.status_code, 403,
            "User without CompanyAccess to the realm must receive 403")

    # ── 3. Authorized user → 302 ────────────────────────────────

    def test_set_active_realm_authorized_returns_302(self):
        """
        Controller has CompanyAccess to realm_a.
        Setting realm_a active for their client must succeed (redirect 302).
        The functional semantics of client.active_realm_id are unchanged.
        """
        cid = _CLIENT_FIXTURES["client_id"]
        realm = _FIXTURES["realm_a"]          # ctrl HAS access to realm_a
        tok = _FIXTURES["ctrl_token"]
        resp = client.post(
            f"/clients/{cid}/set-active/{realm}",
            cookies={"qbo_session": tok},
        )
        self.assertIn(resp.status_code, (301, 302, 307, 308),
            f"Authorized request must redirect; got {resp.status_code}")

        # Verify the DB was actually updated
        db = _db()
        updated_client = db.query(Client).filter_by(id=cid).first()
        active = updated_client.active_realm_id
        db.close()
        self.assertEqual(active, realm,
            "client.active_realm_id must be updated to realm_a after authorized request")

    # ── 4. Authorized user, unauthorized realm_id → 403 ─────────

    def test_set_active_realm_unauthorized_realm_returns_403(self):
        """
        Controller has CompanyAccess to realm_a but NOT to realm_b.
        Attempting to activate realm_b (which belongs to the same client)
        must be rejected with 403 — check_realm_access enforces per-realm
        access control regardless of client ownership.
        """
        cid = _CLIENT_FIXTURES["client_id"]
        realm_b = _FIXTURES["realm_b"]        # ctrl has NO access to realm_b
        tok = _FIXTURES["ctrl_token"]

        # Record current active_realm_id to verify no mutation occurs
        db = _db()
        before = db.query(Client).filter_by(id=cid).first().active_realm_id
        db.close()

        resp = client.post(
            f"/clients/{cid}/set-active/{realm_b}",
            cookies={"qbo_session": tok},
        )
        self.assertEqual(resp.status_code, 403,
            "User without CompanyAccess to realm_b must receive 403 even if realm belongs to their client")

        # Verify active_realm_id was NOT mutated
        db = _db()
        after = db.query(Client).filter_by(id=cid).first().active_realm_id
        db.close()
        self.assertEqual(before, after,
            "Rejected request must not mutate client.active_realm_id")
