"""
security.py — Password hashing, session tokens, and token encryption.
All credential handling is in this one module.
"""
import hashlib
import os
import secrets
import base64
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import Fernet
from passlib.context import CryptContext

from app.config import settings


# ─── Password hashing (bcrypt) ────────────────────────────────

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (work factor 12)."""
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the bcrypt hash."""
    return _pwd_context.verify(plain, hashed)


# ─── Session tokens ────────────────────────────────────────────

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


# ─── Token encryption (Fernet) ───────────────────────────────


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
