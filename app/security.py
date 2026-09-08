"""Token encryption — QBO credentials are never stored in plaintext."""
import os
import base64
from cryptography.fernet import Fernet
from app.config import settings


def _get_fernet() -> Fernet:
    key = settings.ENCRYPTION_KEY
    if not key:
        # Auto-generate a key for development (not production)
        key = Fernet.generate_key().decode()
        os.environ["ENCRYPTION_KEY"] = key
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        # Pad or fix key if needed
        padded = base64.urlsafe_b64encode(key.encode()[:32].ljust(32, b'\0'))
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
