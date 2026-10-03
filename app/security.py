"""
security.py — Password hashing, session tokens, and token encryption.
All credential handling is in this one module.

Password hashing scheme: bcrypt + SHA-256 pre-hash
───────────────────────────────────────────────────
bcrypt truncates inputs at 72 bytes.  Passwords longer than that are either
silently mangled (bcrypt < 4.0) or rejected with ValueError (bcrypt >= 4.0).
To support arbitrarily long passwords without either defect, every password is
pre-hashed with SHA-256 before being passed to bcrypt:

    hash_password(pw)   → sha256(pw).digest() [32 bytes] → bcrypt → $2b$12$…
    verify_password(pw) → sha256(pw).digest() [32 bytes] → bcrypt.checkpw

The stored value is a standard bcrypt hash ($2b$12$…).  32 bytes is always
safely below bcrypt's 72-byte limit regardless of the original password length.

⚠ HASH MIGRATION NOTE
Hashes created with the former passlib/bcrypt scheme — where the plaintext
password was passed directly to bcrypt without SHA-256 pre-hashing — are NOT
verifiable with this scheme.  Any user whose password was hashed under the old
scheme must reset their password.  As of the commit that introduced this
module, there are zero users in production (bootstrap was failing before the
first commit could be made), so no migration is required.  This note is kept
here so future maintainers are aware of the incompatibility.
"""
import hashlib
import os
import secrets
import base64
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet

from app.config import settings


# ─── Password hashing (bcrypt + SHA-256 pre-hash) ────────────────────────────

_BCRYPT_ROUNDS = 12


def _prehash(password: str) -> bytes:
    """Return the SHA-256 digest of *password* as raw bytes (always 32 bytes).

    bcrypt's 72-byte input limit is irrelevant to 32-byte digests, so this
    pre-hash lets us support passwords of any length without truncation.
    The digest is never logged or stored; it exists only as a transient
    intermediate value inside hash_password / verify_password.
    """
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    """Hash *password* with bcrypt (rounds=12) after SHA-256 pre-hashing.

    Returns a standard bcrypt hash string ($2b$12$…).
    The plaintext password and its SHA-256 digest are never stored or logged.
    """
    digest = _prehash(password)
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the stored bcrypt hash.

    Applies the same SHA-256 pre-hash used by hash_password before calling
    bcrypt.checkpw.  Returns False on any error rather than raising.
    """
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("utf-8"))
    except Exception:
        return False


# ─── Session tokens ───────────────────────────────────────────────────────────

SESSION_TTL_DAYS = 30


def generate_session_token() -> str:
    """Generate a cryptographically random, URL-safe session token."""
    return secrets.token_urlsafe(48)   # ≥ 64 printable chars


def hash_session_token(token: str) -> str:
    """Return the SHA-256 hex digest of a session token (64 hex chars)."""
    return hashlib.sha256(token.encode()).hexdigest()


def session_expiry() -> datetime:
    """Return the UTC expiry time for a new session."""
    return datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS)


# ─── Token encryption (Fernet) ───────────────────────────────────────────────


def _get_fernet() -> Fernet:
    key = os.environ.get("ENCRYPTION_KEY") or settings.ENCRYPTION_KEY
    if not key:
        if getattr(settings, "IS_PRODUCTION", False):
            raise RuntimeError(
                "ENCRYPTION_KEY must be set in production. "
                "Generate one with: python -c \"import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"",
            )
        # Development: auto-generate and cache in env
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        padded = base64.urlsafe_b64encode(key.encode()[:32].ljust(32, b"\0"))
        return Fernet(padded)


def encrypt_token(token: str) -> str:
    """Encrypt an OAuth token for safe database storage."""
    if not token:
        return ""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a stored OAuth token."""
    if not encrypted:
        return ""
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception:
        return ""
