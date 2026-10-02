"""
FastAPI dependency — resolves the current authenticated user from the session cookie.
Used as: current_user: User = Depends(require_auth)
         realm access: Depends(require_company_access(realm_id))
"""
from typing import Optional

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import User, get_db
from app.auth.service import get_session, get_user_by_id, user_can_access_company

SESSION_COOKIE = "qbo_session"


def get_current_user(
    qbo_session: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Return the authenticated User, or None if unauthenticated."""
    if not qbo_session:
        return None
    session = get_session(db, qbo_session)
    if not session:
        return None
    return get_user_by_id(db, session.user_id)


def require_auth(
    current_user: Optional[User] = Depends(get_current_user),
) -> User:
    """FastAPI dependency that raises 401 if not authenticated."""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return current_user


def require_admin(current_user: User = Depends(require_auth)) -> User:
    """Require admin role."""
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return current_user


def require_company_access(realm_id: str):
    """
    Returns a FastAPI dependency factory that validates user access to a specific realm.
    Usage: user = Depends(require_company_access(realm_id))
    """
    def _check(
        db: Session = Depends(get_db),
        current_user: User = Depends(require_auth),
    ) -> User:
        if not user_can_access_company(db, current_user.id, realm_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to company {realm_id} is not authorised for this user",
            )
        return current_user
    return _check


def check_realm_access(
    realm_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
) -> User:
    """
    Path-parameter realm guard for FastAPI routes.
    Receives realm_id from the route's path parameter (FastAPI injects it).
    Validates that the authenticated user has CompanyAccess to that realm.
    Admins bypass the CompanyAccess check (user_can_access_company returns True for admin).

    Usage:
        @app.post("/company/{realm_id}/sync")
        def my_route(realm_id: str, current_user: User = Depends(check_realm_access), db=Depends(get_db)):
            ...
    """
    if not user_can_access_company(db, current_user.id, realm_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access to company realm '{realm_id}' is not authorised for this user",
        )
    return current_user
