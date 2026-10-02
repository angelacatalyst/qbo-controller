"""
Auth routes — login, logout, current user.
POST /auth/login   → sets HttpOnly session cookie
POST /auth/logout  → clears cookie, invalidates session
GET  /auth/me      → returns current user info (JSON)
"""
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db, User
from app.auth.service import authenticate_user, create_session, invalidate_session
from app.auth.dependencies import SESSION_COOKIE, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db: Session = Depends(get_db),
):
    """
    Authenticate with email + password.
    On success: sets HttpOnly, SameSite=Lax session cookie and redirects.
    On failure: returns 401.
    """
    user = authenticate_user(db, email, password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "")
    user_agent = request.headers.get("User-Agent", "")

    raw_token, _ = create_session(db, user, ip_address=ip, user_agent=user_agent)

    response = RedirectResponse(url=next, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=False,   # set True in production via middleware or config
        max_age=12 * 3600,
    )
    return response


@router.post("/logout")
async def logout(
    qbo_session: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """Invalidate session and clear cookie."""
    if qbo_session:
        invalidate_session(db, qbo_session)

    response = RedirectResponse(url="/auth/login-page", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/me")
async def me(current_user: Optional[User] = Depends(get_current_user)):
    """Return current user info. Returns 401 if not authenticated."""
    if not current_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role,
        "is_active": current_user.is_active,
    }
