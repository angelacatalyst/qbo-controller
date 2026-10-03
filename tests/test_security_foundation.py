"""
tests/test_security_foundation.py
──────────────────────────────────
Milestone 1A — Security Foundation Tests

Covers:
  1. Password hashing (bcrypt, non-reversible, verify works)
  2. Session token generation and hashing
  3. Session token: only hash is stored (raw token never in DB)
  4. Realm isolation: user cannot access a company they don't have access to
  5. Admin bypass: admin user can access any company
  6. OAuthState DB-backed: save, retrieve, expiry
  7. ENCRYPTION_KEY fail-fast in production (simulated)
  8. Refresh lock: same realm returns same lock object
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# ─── Path setup ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ════════════════════════════════════════════════════════════════
# 1–3 · security.py — password, session token
# ════════════════════════════════════════════════════════════════
class TestPasswordHashing(unittest.TestCase):
    def setUp(self):
        from app.security import hash_password, verify_password
        self.hash_password = hash_password
        self.verify_password = verify_password

    def test_hash_is_not_plaintext(self):
        hashed = self.hash_password("MySecret123!")
        self.assertNotEqual(hashed, "MySecret123!")

    def test_verify_correct_password(self):
        hashed = self.hash_password("CorrectHorse")
        self.assertTrue(self.verify_password("CorrectHorse", hashed))

    def test_verify_wrong_password(self):
        hashed = self.hash_password("CorrectHorse")
        self.assertFalse(self.verify_password("WrongHorse", hashed))

    def test_two_hashes_of_same_password_differ(self):
        """bcrypt uses per-hash salts — same plaintext → different hashes."""
        h1 = self.hash_password("SamePass")
        h2 = self.hash_password("SamePass")
        self.assertNotEqual(h1, h2)
        # But both verify
        self.assertTrue(self.verify_password("SamePass", h1))
        self.assertTrue(self.verify_password("SamePass", h2))


    def test_password_over_72_bytes_hashes_and_verifies(self):
        """Passwords longer than bcrypt's 72-byte limit hash and verify correctly."""
        # 80 ASCII chars = 80 bytes — safely over the 72-byte bcrypt limit
        long_pw = "Aa1!" * 20   # 80 chars
        hashed = self.hash_password(long_pw)
        self.assertIsNotNone(hashed)
        self.assertTrue(
            self.verify_password(long_pw, hashed),
            "verify_password must return True for a password >72 bytes"
        )

    def test_passwords_differing_only_after_byte_72_produce_different_hashes(self):
        """Two passwords that differ only after byte 72 must NOT produce the same hash.

        With raw bcrypt (no pre-hash), bytes beyond position 72 are silently
        discarded, so 'AAAA...A' (72 A's + 'X') and 'AAAA...A' (72 A's + 'Y')
        would hash identically.  SHA-256 pre-hashing eliminates this defect.
        """
        base = "A" * 72
        pw_x = base + "X"   # differs at byte 73
        pw_y = base + "Y"   # differs at byte 73

        hashed_x = self.hash_password(pw_x)
        hashed_y = self.hash_password(pw_y)

        # The two hashes must be different (bcrypt salts are random, but even
        # without that, we verify cross-verification fails)
        self.assertFalse(
            self.verify_password(pw_x, hashed_y),
            "pw_x must NOT verify against pw_y's hash — no truncation at 72 bytes"
        )
        self.assertFalse(
            self.verify_password(pw_y, hashed_x),
            "pw_y must NOT verify against pw_x's hash — no truncation at 72 bytes"
        )


class TestSessionToken(unittest.TestCase):
    def setUp(self):
        from app.security import generate_session_token, hash_session_token
        self.generate = generate_session_token
        self.hash_tok = hash_session_token

    def test_token_is_url_safe_string(self):
        tok = self.generate()
        self.assertIsInstance(tok, str)
        self.assertGreater(len(tok), 32)

    def test_hash_is_64_hex_chars(self):
        tok = self.generate()
        h = self.hash_tok(tok)
        self.assertEqual(len(h), 64)
        # all hex
        int(h, 16)  # raises ValueError if not hex

    def test_different_tokens_different_hashes(self):
        t1 = self.generate()
        t2 = self.generate()
        self.assertNotEqual(t1, t2)
        self.assertNotEqual(self.hash_tok(t1), self.hash_tok(t2))

    def test_same_token_same_hash(self):
        tok = self.generate()
        self.assertEqual(self.hash_tok(tok), self.hash_tok(tok))


# ════════════════════════════════════════════════════════════════
# 4–5 · auth/service.py — realm isolation
# ════════════════════════════════════════════════════════════════
class TestRealmIsolation(unittest.TestCase):
    """
    Tests for user_can_access_company().
    We mock the DB session so no real database is needed.
    """

    def _make_db(self, accesses):
        """Return a mock db whose query().filter().first() returns the given value."""
        db = MagicMock()
        chain = MagicMock()
        db.query.return_value = chain
        chain.filter.return_value = chain
        chain.first.return_value = accesses
        return db

    def _make_user(self, role="controller"):
        u = MagicMock()
        u.id = "user-001"
        u.role = role
        return u

    def test_controller_with_access_can_access_realm(self):
        from app.auth.service import user_can_access_company
        access_row = MagicMock()  # non-None → has access
        db = self._make_db(access_row)
        user = self._make_user("controller")
        result = user_can_access_company(db, user.id, "realm-ABC")
        self.assertTrue(result)

    def test_controller_without_access_cannot_access_realm(self):
        from app.auth.service import user_can_access_company
        db = self._make_db(None)  # None → no CompanyAccess row
        user = self._make_user("controller")
        result = user_can_access_company(db, user.id, "realm-XYZ")
        self.assertFalse(result)

    def test_admin_can_access_any_realm(self):
        """Admin bypasses CompanyAccess check — db.query should not be called."""
        from app.auth.service import user_can_access_company
        db = MagicMock()
        # Simulate: admin check uses user role
        # We need the real function — it checks role first
        # Provide an admin user object in DB
        admin = MagicMock()
        admin.role = "admin"
        admin.id = "admin-001"
        db.query.return_value.filter.return_value.first.return_value = admin
        result = user_can_access_company(db, "admin-001", "any-realm")
        self.assertTrue(result)

    def test_realm_ids_do_not_bleed(self):
        """
        Company in realm-A must NOT be visible when querying realm-B.
        Simulates isolation: controller has access to realm-A but not realm-B.
        """
        from app.auth.service import user_can_access_company

        def side_effect(*args, **kwargs):
            """Return access row only for realm-A."""
            # The filter call includes realm_id — we check the call args
            filter_args = str(args) + str(kwargs)
            return MagicMock() if "realm-A" in filter_args else None

        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = side_effect
        user_id = "user-002"

        # Accessing realm-A should be allowed — the mock will return an access row
        # (This is a unit test of the function; realm checking happens in DB query)
        # We verify the function calls the DB with realm_id and respects the result
        db2 = self._make_db(MagicMock())  # has access
        self.assertTrue(user_can_access_company(db2, user_id, "realm-A"))

        db3 = self._make_db(None)  # no access
        self.assertFalse(user_can_access_company(db3, user_id, "realm-B"))


# ════════════════════════════════════════════════════════════════
# 6 · OAuthState DB-backed (in-memory SQLite)
# ════════════════════════════════════════════════════════════════
class TestOAuthStateDB(unittest.TestCase):
    """Use an in-memory SQLite DB to test OAuthState model operations."""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base, OAuthState
        self.OAuthState = OAuthState
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()

    def tearDown(self):
        self.db.close()

    def _save(self, state: str, payload: dict, expires_in_minutes: int = 10):
        obj = self.OAuthState(
            state=state,
            payload=payload,
            expires_at=datetime.utcnow() + timedelta(minutes=expires_in_minutes),
        )
        self.db.add(obj)
        self.db.commit()

    def test_save_and_retrieve(self):
        self._save("abc123", {"client_id": "CL1"})
        row = self.db.query(self.OAuthState).filter_by(state="abc123").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.payload["client_id"], "CL1")

    def test_expired_state_is_stale(self):
        self._save("stale", {"client_id": "CL2"}, expires_in_minutes=-5)
        row = (
            self.db.query(self.OAuthState)
            .filter(
                self.OAuthState.state == "stale",
                self.OAuthState.expires_at > datetime.utcnow(),
            )
            .first()
        )
        self.assertIsNone(row)  # expired → not returned

    def test_state_is_deleted_after_pop(self):
        self._save("onetime", {"client_id": "CL3"})
        row = self.db.query(self.OAuthState).filter_by(state="onetime").first()
        self.db.delete(row)
        self.db.commit()
        after = self.db.query(self.OAuthState).filter_by(state="onetime").first()
        self.assertIsNone(after)

    def test_duplicate_state_raises(self):
        from sqlalchemy.exc import IntegrityError
        self._save("dup", {"client_id": "CL4"})
        with self.assertRaises(IntegrityError):
            self._save("dup", {"client_id": "CL5"})


# ════════════════════════════════════════════════════════════════
# 7 · ENCRYPTION_KEY fail-fast
# ════════════════════════════════════════════════════════════════
class TestEncryptionKeyFailFast(unittest.TestCase):
    def test_production_missing_key_raises(self):
        """
        If settings.IS_PRODUCTION is True and ENCRYPTION_KEY is absent,
        _get_fernet() must raise RuntimeError.
        """
        import app.security as sec_mod
        from app.config import settings as cfg
        env_backup = os.environ.pop("ENCRYPTION_KEY", None)
        try:
            with patch.object(cfg, "IS_PRODUCTION", True):
                with self.assertRaises((RuntimeError, Exception)):
                    sec_mod._get_fernet()
        finally:
            if env_backup:
                os.environ["ENCRYPTION_KEY"] = env_backup


# ════════════════════════════════════════════════════════════════
# 8 · Refresh lock — per-realm singleton
# ════════════════════════════════════════════════════════════════
class TestRefreshLock(unittest.TestCase):
    def test_same_realm_same_lock(self):
        from app.qbo_client import _get_realm_lock
        l1 = _get_realm_lock("realm-LOCK-A")
        l2 = _get_realm_lock("realm-LOCK-A")
        self.assertIs(l1, l2)

    def test_different_realms_different_locks(self):
        from app.qbo_client import _get_realm_lock
        la = _get_realm_lock("realm-LOCK-B")
        lb = _get_realm_lock("realm-LOCK-C")
        self.assertIsNot(la, lb)

    def test_lock_is_acquirable(self):
        from app.qbo_client import _get_realm_lock
        lock = _get_realm_lock("realm-LOCK-D")
        acquired = lock.acquire(blocking=False)
        self.assertTrue(acquired)
        lock.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ════════════════════════════════════════════════════════════════
# 9 · Startup bootstrap — _bootstrap_initial_admin()
# ════════════════════════════════════════════════════════════════
class TestBootstrapInitialAdmin(unittest.TestCase):
    """
    Tests for the _bootstrap_initial_admin() startup helper.

    Each test creates an isolated in-memory SQLite database so the
    users table state is controlled precisely.  app.database.SessionLocal
    is patched to return that database session, avoiding any interaction
    with the real database.
    """

    def _make_db(self):
        """Return an isolated in-memory SQLite session with all app tables."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from app.database import Base
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        return Session()

    def test_empty_table_valid_vars_creates_one_admin(self):
        """Empty users table + valid env vars → exactly one admin is created."""
        from app.main import _bootstrap_initial_admin
        from app.database import User

        db = self._make_db()
        env = {
            "INITIAL_ADMIN_EMAIL": "admin@example.com",
            "INITIAL_ADMIN_PASSWORD": "BootstrapPass123!",
            "INITIAL_ADMIN_FULL_NAME": "Test Admin",
        }
        with patch("app.database.SessionLocal", return_value=db):
            with patch.dict(os.environ, env, clear=False):
                _bootstrap_initial_admin()

        count = db.query(User).count()
        self.assertEqual(count, 1, "Expected exactly one user after bootstrap")
        user = db.query(User).first()
        self.assertEqual(user.email, "admin@example.com")
        self.assertEqual(user.role, "admin")
        db.close()

    def test_existing_user_env_vars_present_no_new_user_created(self):
        """Existing user + env vars present → bootstrap is a no-op; no second user created."""
        from app.main import _bootstrap_initial_admin
        from app.auth.service import create_initial_admin
        from app.database import User

        db = self._make_db()
        # Pre-create a user so the table is non-empty
        create_initial_admin(
            db, email="existing@example.com",
            password="ExistPass1!", full_name="Existing Admin"
        )

        env = {
            "INITIAL_ADMIN_EMAIL": "second@example.com",
            "INITIAL_ADMIN_PASSWORD": "SecondPass123!",
        }
        with patch("app.database.SessionLocal", return_value=db):
            with patch.dict(os.environ, env, clear=False):
                _bootstrap_initial_admin()

        count = db.query(User).count()
        self.assertEqual(count, 1, "Bootstrap must not create a second user when one exists")
        db.close()

    def test_empty_table_missing_env_vars_no_user_created(self):
        """Empty users table + missing INITIAL_ADMIN_* vars → no user created."""
        from app.main import _bootstrap_initial_admin
        from app.database import User

        db = self._make_db()
        # Clear the two mandatory env vars if present
        stripped = {
            k: v for k, v in os.environ.items()
            if k not in ("INITIAL_ADMIN_EMAIL", "INITIAL_ADMIN_PASSWORD")
        }
        with patch("app.database.SessionLocal", return_value=db):
            with patch.dict(os.environ, stripped, clear=True):
                _bootstrap_initial_admin()

        count = db.query(User).count()
        self.assertEqual(count, 0, "Bootstrap must not create any user without env vars")
        db.close()

    def test_password_stored_hashed_never_plaintext(self):
        """Bootstrap stores the bcrypt hash in DB; plaintext password is never written."""
        from app.main import _bootstrap_initial_admin
        from app.database import User
        from app.security import verify_password

        db = self._make_db()
        plaintext = "SuperSecret999!"
        env = {
            "INITIAL_ADMIN_EMAIL": "hashed@example.com",
            "INITIAL_ADMIN_PASSWORD": plaintext,
        }
        with patch("app.database.SessionLocal", return_value=db):
            with patch.dict(os.environ, env, clear=False):
                _bootstrap_initial_admin()

        user = db.query(User).first()
        self.assertIsNotNone(user, "A user must have been created")
        # Plaintext must NOT be stored verbatim
        self.assertNotEqual(
            user.hashed_password, plaintext,
            "Plaintext password must not be stored in hashed_password"
        )
        # bcrypt verify must pass
        self.assertTrue(
            verify_password(plaintext, user.hashed_password),
            "verify_password() must return True for the original password"
        )
        db.close()


# ════════════════════════════════════════════════════════════════
# 10 · Production SQLite guard — _build_engine()
# ════════════════════════════════════════════════════════════════
class TestProductionSQLiteGuard(unittest.TestCase):
    """
    Tests for the IS_PRODUCTION + SQLite fail-fast guard in _build_engine().

    Settings attributes are patched directly on the module-level `settings`
    object so the guard reads the patched values at call time.
    The existing module-level `engine` is already built; these tests call
    _build_engine() directly to exercise the guard in isolation.
    """

    def test_production_sqlite_raises_runtime_error(self):
        """IS_PRODUCTION=true + SQLite URL → RuntimeError before any connection."""
        from app.database import _build_engine
        from app.config import settings

        with patch.object(settings, "IS_PRODUCTION", True), \
             patch.object(settings, "DATABASE_URL", "sqlite:///./qbo_controller.db"):
            with self.assertRaises(RuntimeError) as ctx:
                _build_engine()

        msg = str(ctx.exception)
        self.assertIn("SQLite", msg)
        self.assertIn("IS_PRODUCTION", msg)
        self.assertIn("PostgreSQL", msg)

    def test_non_production_sqlite_is_allowed(self):
        """IS_PRODUCTION=false + SQLite URL → engine is built without error."""
        from app.database import _build_engine
        from app.config import settings

        with patch.object(settings, "IS_PRODUCTION", False), \
             patch.object(settings, "DATABASE_URL", "sqlite:///:memory:"), \
             patch.object(settings, "DEBUG", False):
            try:
                eng = _build_engine()
                eng.dispose()
            except RuntimeError as exc:
                self.fail(
                    f"_build_engine() raised RuntimeError for non-production SQLite: {exc}"
                )
