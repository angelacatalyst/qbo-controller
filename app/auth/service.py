"""
Authentication service — login, logout, session management.
All sessions are stored in the database (UserSession table).
The raw token is sent to the client as a cookie; only its hash is stored.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.database import User, UserSession, CompanyAccess
from app.security import (
    verify_password, hash_password,
    generate_session_token, hash_session_token, session_expiry,
)


# ── User lookup ───────────────────────────────────────────────

def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.query(User).filter(User.email == email.lower().strip()).first()


def get_user_by_id(db: Session, user_id: str) -> Optional[User]:
    return db.query(User).filter(User.id == user_id, User.is_active == True).first()


# ── Authentication ────────────────────────────────────────────

def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    """Return user if credentials are valid, None otherwise."""
    user = get_user_by_email(db, email)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def create_session(
    db: Session, user: User, ip_address: str = "", user_agent: str = ""
) -> tuple[str, UserSession]:
    """
    Create a new UserSession. Returns (raw_token, session_record).
    raw_token is sent to the client — it is NOT stored in the DB.
    Only its hash is stored.
    """
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)

    session = UserSession(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=session_expiry(),
        ip_address=ip_address[:64] if ip_address else "",
        user_agent=user_agent[:512] if user_agent else "",
        is_active=True,
    )
    db.add(session)

    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(session)
    return raw_token, session


def get_session(db: Session, raw_token: str) -> Optional[UserSession]:
    """Return active, non-expired session by raw token, or None."""
    token_hash = hash_session_token(raw_token)
    session = (
        db.query(UserSession)
        .filter(
            UserSession.token_hash == token_hash,
            UserSession.is_active == True,
            UserSession.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if session:
        # Touch last_seen without bumping updated_at unnecessarily
        session.last_seen = datetime.utcnow()
        db.commit()
    return session


def invalidate_session(db: Session, raw_token: str) -> None:
    """Logout — mark the session inactive."""
    token_hash = hash_session_token(raw_token)
    session = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
    if session:
        session.is_active = False
        db.commit()


def invalidate_all_sessions(db: Session, user_id: str) -> int:
    """Revoke all sessions for a user (force re-login everywhere)."""
    updated = (
        db.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.is_active == True)
        .update({"is_active": False})
    )
    db.commit()
    return updated


# ── Company access checks ─────────────────────────────────────

def user_can_access_company(db: Session, user_id: str, realm_id: str) -> bool:
    """
    Return True if the user has active access to the given realm.
    Admin users can access all companies.
    """
    user = get_user_by_id(db, user_id)
    if not user:
        return False
    if user.role == "admin":
        return True
    access = (
        db.query(CompanyAccess)
        .filter(
            CompanyAccess.user_id == user_id,
            CompanyAccess.realm_id == realm_id,
            CompanyAccess.is_active == True,
        )
        .first()
    )
    return access is not None


def grant_company_access(
    db: Session, user_id: str, company_id: str, realm_id: str,
    role: str = "controller", granted_by: str = ""
) -> CompanyAccess:
    """Grant a user access to a company. Idempotent."""
    existing = (
        db.query(CompanyAccess)
        .filter(CompanyAccess.user_id == user_id, CompanyAccess.realm_id == realm_id)
        .first()
    )
    if existing:
        existing.is_active = True
        existing.role = role
        db.commit()
        return existing

    access = CompanyAccess(
        user_id=user_id,
        company_id=company_id,
        realm_id=realm_id,
        role=role,
        granted_by=granted_by,
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    return access


# ── Admin bootstrap ───────────────────────────────────────────

def create_initial_admin(db: Session, email: str, password: str, full_name: str = "Admin") -> User:
    """
    Create the first admin user. Idempotent — returns existing user if email exists.
    Called during startup when no users exist yet.
    """
    existing = get_user_by_email(db, email)
    if existing:
        return existing
    user = User(
        email=email.lower().strip(),
        hashed_password=hash_password(password),
        full_name=full_name,
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
