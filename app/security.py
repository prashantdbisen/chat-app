"""
security.py - Password hashing and login tokens (JWT).

Passwords are never stored - only a bcrypt hash, which can't be reversed.
After login, the user gets a signed token that proves who they are.
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import ACCESS_TOKEN_EXPIRE_MINUTES, SECRET_KEY
from app.database import get_db
from app.models import User

_ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes of a password
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], password_hash.encode())
    except ValueError:
        return False


def create_access_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": str(user_id), "exp": expires}, SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str):
    """Return the user id inside a valid token, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[_ALGORITHM])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: the logged-in user, or a 401 error."""
    user_id = decode_token(credentials.credentials) if credentials else None
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again.")
    return user
