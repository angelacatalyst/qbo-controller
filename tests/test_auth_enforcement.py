"""
tests/test_auth_enforcement.py
───────────────────────────────
Milestone 1A — Auth Enforcement + Realm Isolation Tests

Tests (A–F per spec):
  A. Controller autorizado para Realm A → puede acceder (200/302)
  B. Controller sin acceso a Realm B → 403
  C. Cross-realm resource: item de Realm B presentado al endpoint de Realm A → 404 (no data leak)
  D. Usuario no autenticado → 401
  E. Autenticado pero sin CompanyAccess al realm → 403
  F. realm_id en path nunca es suficiente autorización por sí solo;
     la autorización viene del usuario autenticado + CompanyAccess

Cada test usa:
  • FastAPI TestClient (sin servidor real)
  • SQLite in-memory (independiente del DB de producción en Windows NTFS)
  • La misma lógica de autorización de producción (sin bypasses de dev/test/localhost)
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

# ─── Path setup ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import (
    Base, User, UserSession, Company, CompanyAccess,
    WorkItem, ProposedJournalEntry, MonthEndClose,
    get_db,
)
from app.security import hash_password, generate_session_token, hash_session_token, session_expiry
from app.main import app

# ════════════════════════════════════════════════════════════════
# ─── DB FIXTURE (in-memory SQLite) ───────────────────────────
# ════════════════════════════════════════════════════════════════

# Engine, _Session, and get_db override are set up centrally in conftest.py.
# Importing from there to avoid per-file overrides that conflict when running
# all tests together.
from tests.conftest import _Session  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


# ════════════════════════════════════════════════════════════════
# ─── HELPER FUNCTIONS ────────────────────────────────────────
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


def _create_company(db, realm_id, name):
    c = Company(
        realm_id=realm_id,
        company_name=name,
        connection_status="connected",
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


def _create_session_cookie(db, user):
    """Create a real session in DB and return the raw token (to use as cookie)."""
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    sess = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=session_expiry(),
        is_active=True,
    )
    db.add(sess)
    db.commit()
    return raw_token


def _create_work_item(db, realm_id, company_id, work_type="CATEGORIZE", status="approved"):
    wi = WorkItem(
        realm_id=realm_id,
        company_id=company_id,
        work_type=work_type,
        status=status,
        reason="Created by test",
    )
    db.add(wi)
    db.commit()
    db.refresh(wi)
    return wi


def _create_je(db, realm_id, company_id, status="approved"):
    je = ProposedJournalEntry(
        realm_id=realm_id,
        company_id=company_id,
        je_number="JE-TEST-001",
        description="Test JE",
        je_date=datetime.utcnow(),
        approval_status=status,
        lines=[],
    )
    db.add(je)
    db.commit()
    db.refresh(je)
    return je


# ════════════════════════════════════════════════════════════════
# ─── TEST SETUP: shared fixtures (module-level, created once) ─
# ════════════════════════════════════════════════════════════════

_FIXTURES: dict = {}


def _ensure_fixtures():
    """Create all test fixtures once; subsequent calls are no-ops.
    Stores only plain Python values (IDs, strings) — never ORM instances —
    so there is no DetachedInstanceError after the session closes.
    """
    if _FIXTURES:
        return
    db = _db()
    try:
        company_a = _create_company(db, realm_id="REALM-ENFORCE-A", name="Company Alpha")
        company_b = _create_company(db, realm_id="REALM-ENFORCE-B", name="Company Beta")

        user_a = _create_user(db, "user_a@test.local", role="controller")
        _grant_access(db, user_a, company_a)

        user_b = _create_user(db, "user_b@test.local", role="controller")
        _grant_access(db, user_b, company_b)

        admin = _create_user(db, "admin@test.local", role="admin")
        # Include user_neither here so TestE.setUpClass does not re-insert
        user_neither = _create_user(db, "neither@test.local", role="controller")

        cookie_a = _create_session_cookie(db, user_a)
        cookie_b = _create_session_cookie(db, user_b)
        cookie_admin = _create_session_cookie(db, admin)
        cookie_neither = _create_session_cookie(db, user_neither)

        wi_realm_a = _create_work_item(db, company_a.realm_id, company_a.id)
        je_realm_a = _create_je(db, company_a.realm_id, company_a.id)

        wi_realm_b = _create_work_item(db, company_b.realm_id, company_b.id)
        je_realm_b = _create_je(db, company_b.realm_id, company_b.id)

        # Extract plain values BEFORE closing the session (avoids DetachedInstanceError)
        _FIXTURES.update({
            "realm_a": company_a.realm_id,
            "realm_b": company_b.realm_id,
            "company_a_name": company_a.company_name,
            "company_b_name": company_b.company_name,
            "je_realm_a_id": str(je_realm_a.id),
            "je_realm_b_id": str(je_realm_b.id),
            "wi_realm_a_id": str(wi_realm_a.id),
            "wi_realm_b_id": str(wi_realm_b.id),
            "cookie_a": cookie_a,
            "cookie_b": cookie_b,
            "cookie_admin": cookie_admin,
            "cookie_neither": cookie_neither,
        })
    finally:
        db.close()


class _FixtureBase(unittest.TestCase):
    """Base class that exposes shared fixtures as class attributes."""

    @classmethod
    def setUpClass(cls):
        _ensure_fixtures()
        for key, val in _FIXTURES.items():
            setattr(cls, key, val)


# ════════════════════════════════════════════════════════════════
# ─── TEST A: AUTHORIZED CONTROLLER CAN ACCESS OWN REALM ──────
# ════════════════════════════════════════════════════════════════

class TestA_AuthorizedAccess(_FixtureBase):
    """
    TEST A: User A tiene acceso a Realm A.
    User A puede operar sobre un recurso de Realm A.
    """

    def test_A1_authenticated_controller_can_approve_je_in_own_realm(self):
        """POST /api/company/{realm_id}/journal-entries/{je_id}/approve → 200 para usuario autorizado."""
        resp = client.post(
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_a_id}/approve",
            data={"approved_by": "TestController"},
            cookies={"qbo_session": self.cookie_a},
        )
        # Should succeed (200) — user_a has CompanyAccess to realm_a
        self.assertEqual(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        self.assertTrue(data.get("success"), f"Expected success=True: {data}")

    def test_A2_authorized_controller_can_update_month_close(self):
        """POST /api/company/{realm_id}/month-close/{close_id}/step → allowed for authorized user."""
        # month_close requires a close record — use a fake close_id and expect 404 (not 401/403)
        # 404 means: auth passed, realm check passed, resource not found — correct behavior
        resp = client.post(
            f"/api/company/{self.realm_a}/month-close/nonexistent-close/step",
            data={"step_num": 1, "step_status": "done", "notes": ""},
            cookies={"qbo_session": self.cookie_a},
        )
        # 404 is the correct outcome: auth succeeded, no close record found
        self.assertIn(resp.status_code, (404, 200), f"Expected 404 or 200, got {resp.status_code}: {resp.text[:200]}")
        self.assertNotEqual(resp.status_code, 401, "Must not be 401 for authenticated authorized user")
        self.assertNotEqual(resp.status_code, 403, "Must not be 403 for authorized realm")

    def test_A3_admin_can_access_any_realm(self):
        """Admin user bypasses CompanyAccess check and can access any realm."""
        resp = client.post(
            f"/api/company/{self.realm_b}/journal-entries/{self.je_realm_b_id}/reject",
            data={"reason": "Admin test"},
            cookies={"qbo_session": self.cookie_admin},
        )
        self.assertNotEqual(resp.status_code, 401, "Admin must not receive 401")
        self.assertNotEqual(resp.status_code, 403, "Admin must not receive 403")
        self.assertIn(resp.status_code, (200, 400), f"Admin got {resp.status_code}: {resp.text[:200]}")


# ════════════════════════════════════════════════════════════════
# ─── TEST B: UNAUTHORIZED REALM ACCESS → 403 ─────────────────
# ════════════════════════════════════════════════════════════════

class TestB_UnauthorizedRealm(_FixtureBase):
    """
    TEST B: User A NO tiene acceso a Realm B.
    User A intenta acceder explícitamente usando Realm B → debe recibir 403.
    """

    def test_B1_controller_rejected_from_wrong_realm_je_approve(self):
        """user_a (realm_a only) → POST /api/company/REALM-B/journal-entries/.../approve → 403."""
        resp = client.post(
            f"/api/company/{self.realm_b}/journal-entries/{self.je_realm_b_id}/approve",
            data={"approved_by": "attacker"},
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403, f"Expected 403, got {resp.status_code}: {resp.text[:200]}")

    def test_B2_controller_rejected_from_wrong_realm_je_execute(self):
        """user_a → execute JE in realm_b → 403."""
        resp = client.post(
            f"/api/company/{self.realm_b}/journal-entries/{self.je_realm_b_id}/execute",
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403, f"Expected 403, got {resp.status_code}: {resp.text[:200]}")

    def test_B3_controller_rejected_from_wrong_realm_month_close(self):
        """user_a → month-close step in realm_b → 403."""
        resp = client.post(
            f"/api/company/{self.realm_b}/month-close/any-close-id/step",
            data={"step_num": 1, "step_status": "done", "notes": ""},
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403, f"Expected 403, got {resp.status_code}: {resp.text[:200]}")

    def test_B4_controller_rejected_from_wrong_realm_work_item_execute(self):
        """user_a → execute work item in realm_b → 403."""
        resp = client.post(
            f"/company/{self.realm_b}/work-items/{self.wi_realm_b_id}/execute",
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403, f"Expected 403, got {resp.status_code}: {resp.text[:200]}")

    def test_B5_controller_rejected_from_wrong_realm_sync(self):
        """user_a → sync realm_b → 403."""
        resp = client.post(
            f"/company/{self.realm_b}/sync",
            data={"sync_type": "full"},
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403, f"Expected 403, got {resp.status_code}: {resp.text[:200]}")

    def test_B6_controller_rejected_from_wrong_realm_remove(self):
        """user_a → remove realm_b → 403."""
        resp = client.post(
            f"/company/{self.realm_b}/remove",
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403, f"Expected 403, got {resp.status_code}: {resp.text[:200]}")


# ════════════════════════════════════════════════════════════════
# ─── TEST C: CROSS-REALM RESOURCE → 404, NO DATA LEAK ────────
# ════════════════════════════════════════════════════════════════

class TestC_CrossRealmResource(_FixtureBase):
    """
    TEST C: User A tiene acceso a Realm A e intenta usar un item_id/je_id
    perteneciente a Realm B a través del endpoint de Realm A.
    Debe ser rechazado. No debe existir data leak.
    """

    def test_C1_je_from_realm_b_not_visible_via_realm_a_endpoint(self):
        """
        user_a (has access to realm_a) sends je_realm_b.id to realm_a endpoint.
        The DB query filters by realm_a AND je_id → 404 (no data from realm_b is returned).
        """
        resp = client.post(
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_b_id}/approve",
            data={"approved_by": "controller"},
            cookies={"qbo_session": self.cookie_a},
        )
        # 404 = auth passed (user has access to realm_a), but resource not found in realm_a
        # This is the correct outcome: the JE belongs to realm_b, not realm_a
        self.assertEqual(resp.status_code, 404, 
            f"Expected 404 (cross-realm resource not found), got {resp.status_code}: {resp.text[:200]}")
        # Verify response does NOT contain realm_b data
        response_text = resp.text.lower()
        self.assertNotIn(self.realm_b.lower(), response_text.replace("REALM-ENFORCE-B", "").lower() or "",
            "Response must not contain realm_b identifier as data")

    def test_C2_work_item_from_realm_b_not_visible_via_realm_a_endpoint(self):
        """
        user_a presents wi_realm_b.id to realm_a endpoint → 404.
        WorkItem query includes realm_id filter → no bleed.
        """
        resp = client.post(
            f"/company/{self.realm_a}/work-items/{self.wi_realm_b_id}/execute",
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 404,
            f"Expected 404 (cross-realm work item not visible), got {resp.status_code}: {resp.text[:200]}")

    def test_C3_je_reject_cross_realm_returns_404_not_realm_b_data(self):
        """Rejection of cross-realm JE must return 404, not expose realm_b data."""
        resp = client.post(
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_b_id}/reject",
            data={"reason": "test"},
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 404,
            f"Expected 404, got {resp.status_code}: {resp.text[:200]}")


# ════════════════════════════════════════════════════════════════
# ─── TEST D: UNAUTHENTICATED → 401 ───────────────────────────
# ════════════════════════════════════════════════════════════════

class TestD_Unauthenticated(_FixtureBase):
    """
    TEST D: Un usuario no autenticado intenta acceder a una ruta crítica → 401.
    """

    def _assert_401(self, method, path, **kwargs):
        resp = getattr(client, method)(path, **kwargs)
        self.assertEqual(resp.status_code, 401,
            f"Expected 401 for unauthenticated {method.upper()} {path}, got {resp.status_code}: {resp.text[:200]}")

    def test_D1_no_cookie_je_approve_returns_401(self):
        self._assert_401("post", f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_a_id}/approve",
                         data={"approved_by": "anon"})

    def test_D2_no_cookie_je_execute_returns_401(self):
        self._assert_401("post", f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_a_id}/execute")

    def test_D3_no_cookie_je_reject_returns_401(self):
        self._assert_401("post", f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_a_id}/reject",
                         data={"reason": ""})

    def test_D4_no_cookie_month_close_returns_401(self):
        self._assert_401("post", f"/api/company/{self.realm_a}/month-close/any-id/step",
                         data={"step_num": 1, "step_status": "done", "notes": ""})

    def test_D5_no_cookie_sync_returns_401(self):
        self._assert_401("post", f"/company/{self.realm_a}/sync", data={"sync_type": "full"})

    def test_D6_no_cookie_remove_returns_401(self):
        self._assert_401("post", f"/company/{self.realm_a}/remove")

    def test_D7_no_cookie_execute_work_item_returns_401(self):
        self._assert_401("post", f"/company/{self.realm_a}/work-items/{self.wi_realm_a_id}/execute")

    def test_D8_no_cookie_qbo_connect_returns_401(self):
        """POST /qbo/connect is admin-only → unauthenticated must receive 401."""
        self._assert_401("post", "/qbo/connect", data={"client_id": ""})


# ════════════════════════════════════════════════════════════════
# ─── TEST E: AUTHENTICATED BUT NO COMPANY ACCESS → 403 ───────
# ════════════════════════════════════════════════════════════════

class TestE_AuthenticatedNoAccess(_FixtureBase):
    """
    TEST E: Usuario autenticado pero sin CompanyAccess al realm → 403.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # user_neither and cookie_neither are created in _ensure_fixtures
        # and copied to class attributes via _FixtureBase.setUpClass

    def _assert_403(self, method, path, **kwargs):
        resp = getattr(client, method)(path, cookies={"qbo_session": self.cookie_neither}, **kwargs)
        self.assertEqual(resp.status_code, 403,
            f"Expected 403 for no-access user {method.upper()} {path}, got {resp.status_code}: {resp.text[:200]}")

    def test_E1_authenticated_no_access_je_approve_returns_403(self):
        self._assert_403("post",
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_a_id}/approve",
            data={"approved_by": "impostor"})

    def test_E2_authenticated_no_access_je_execute_returns_403(self):
        self._assert_403("post",
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_a_id}/execute")

    def test_E3_authenticated_no_access_sync_returns_403(self):
        self._assert_403("post", f"/company/{self.realm_a}/sync", data={"sync_type": "full"})

    def test_E4_authenticated_no_access_work_item_returns_403(self):
        self._assert_403("post",
            f"/company/{self.realm_a}/work-items/{self.wi_realm_a_id}/execute")

    def test_E5_non_admin_qbo_connect_returns_403(self):
        """Controller (not admin) attempting POST /qbo/connect → 403."""
        resp = client.post(
            "/qbo/connect",
            data={"client_id": ""},
            cookies={"qbo_session": self.cookie_a},  # user_a is controller, not admin
        )
        self.assertEqual(resp.status_code, 403,
            f"Expected 403 for non-admin /qbo/connect, got {resp.status_code}: {resp.text[:200]}")


# ════════════════════════════════════════════════════════════════
# ─── TEST F: CLIENT-SUPPLIED realm_id NOT TRUSTED ────────────
# ════════════════════════════════════════════════════════════════

class TestF_RealmIdNotTrustedFromClient(_FixtureBase):
    """
    TEST F: Si existe un endpoint donde realm_id puede venir del request,
    verificar que el servidor NO utilice ese valor como autorización.
    La autorización deriva del usuario autenticado + CompanyAccess,
    y se valida contra el recurso en DB.

    Mecanismo real:
    • realm_id en el path → `check_realm_access(realm_id)` consulta CompanyAccess
      WHERE user_id = <current_user.id> AND realm_id = <path_realm_id>
    • Si no existe row → 403 (independiente del valor que el cliente ponga)
    • El recurso (je_id, item_id) se busca en DB filtrado por realm_id del PATH,
      no del body/query del cliente → 404 si no coincide

    Pruebas: usuario autenticado para realm_a intenta distintas formas de
    reclamar realm_b presentando recursos/datos de realm_b.
    """

    def test_F1_realm_b_path_with_realm_a_token_is_rejected_403(self):
        """
        user_a (cookie for realm_a) tries path /company/REALM-B/...
        The path realm_id = REALM-B, CompanyAccess check → 403.
        The client cannot bypass auth by simply changing the path realm.
        """
        resp = client.post(
            f"/company/{self.realm_b}/work-items/{self.wi_realm_b_id}/execute",
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 403,
            f"Changing path realm_id must not grant access: {resp.status_code} {resp.text[:200]}")

    def test_F2_same_realm_path_different_resource_yields_404_not_data_leak(self):
        """
        user_a (realm_a) presents realm_a path but realm_b resource ID.
        Auth passes (correct realm), resource not found in realm_a → 404.
        No data from realm_b leaks through the response.
        """
        resp = client.post(
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_b_id}/approve",
            data={"approved_by": "test"},
            cookies={"qbo_session": self.cookie_a},
        )
        self.assertEqual(resp.status_code, 404,
            f"Cross-realm resource via correct-realm path must return 404: {resp.status_code} {resp.text[:200]}")
        # Confirm response does not contain realm_b data
        self.assertNotIn(self.company_b_name, resp.text,
            "Response must not expose realm_b company name")

    def test_F3_admin_access_still_requires_resource_to_exist_in_target_realm(self):
        """
        Even admin cannot magically produce realm_a data by presenting realm_b resource ID
        through realm_a endpoint. Resource ownership is enforced by DB filter, not just auth.
        """
        resp = client.post(
            f"/api/company/{self.realm_a}/journal-entries/{self.je_realm_b_id}/reject",
            data={"reason": "admin test cross realm"},
            cookies={"qbo_session": self.cookie_admin},
        )
        # Admin passes auth (no 401, no 403), but resource belongs to realm_b → 404
        self.assertNotEqual(resp.status_code, 401, "Admin must not receive 401")
        self.assertNotEqual(resp.status_code, 403, "Admin must not receive 403")
        self.assertEqual(resp.status_code, 404,
            f"Admin + cross-realm resource must return 404: {resp.status_code} {resp.text[:200]}")


# ════════════════════════════════════════════════════════════════
# ─── ENTRY POINT ─────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
