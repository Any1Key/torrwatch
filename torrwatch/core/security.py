"""Authentication and CSRF primitives used by the Phase 0 web application."""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request, status

from torrwatch.db.database import Database
from torrwatch.db.models import User

PASSWORD_HASHER = PasswordHasher()
CSRF_SESSION_KEY = "csrf_token"
USER_SESSION_KEY = "user_id"
SESSION_EXPIRES_KEY = "session_expires_at"


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except VerificationError:
        return False


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def require_csrf(request: Request, submitted_token: str) -> None:
    expected_token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(expected_token, str) or not hmac.compare_digest(
        expected_token, submitted_token
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token.")


def require_admin(request: Request) -> User:
    user_id = request.session.get(USER_SESSION_KEY)
    if not isinstance(user_id, int):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
        )
    expires = request.session.get(SESSION_EXPIRES_KEY)
    if isinstance(expires, str):
        try:
            if datetime.fromisoformat(expires) < datetime.now(UTC):
                request.session.clear()
                raise HTTPException(status_code=401, detail="Authentication required.")
        except ValueError:
            request.session.clear()
            raise HTTPException(status_code=401, detail="Authentication required.") from None
    database: Database = request.app.state.database
    with database.session() as database_session:
        user = database_session.get(User, user_id)
        if user is None or user.disabled:
            request.session.clear()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required."
            )
        return user
