"""
routers/auth.py - Sign up, sign in, username availability, and "who am I".

Error messages are specific ("No account found...", "Incorrect password") because
the sign-up page already shows whether a username is taken, so hiding it here
would add little protection. Instead, repeated wrong passwords lock that
username for a few minutes, which stops password guessing.
"""

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import LOGIN_LOCK_MINUTES, LOGIN_MAX_ATTEMPTS
from app.connections import manager
from app.database import get_db
from app.errors import FieldError
from app.models import User
from app.schemas import LoginRequest, RegisterRequest, check_username
from app.security import create_access_token, get_current_user, hash_password, verify_password
from app.services import add_member, get_or_create_default_room, member_ids, receipt_dict, user_dict

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Failed sign-in attempts per (IP address, username), kept in memory
_failures: dict[str, deque] = defaultdict(deque)


def _token_response(user: User) -> dict:
    return {"access_token": create_access_token(user.id), "token_type": "bearer", "user": user_dict(user)}


def _taken_message(username: str) -> str:
    return f'The username "{username}" is already taken. Try another one.'


# ---------------------------------------------------------------------------
# Brute-force protection
# ---------------------------------------------------------------------------
def _attempt_key(request: Request, username: str) -> str:
    return f"{request.client.host if request.client else 'unknown'}:{username}"


def _recent_failures(key: str) -> deque:
    window = LOGIN_LOCK_MINUTES * 60
    attempts = _failures[key]
    now = time.monotonic()
    while attempts and now - attempts[0] > window:
        attempts.popleft()
    return attempts


def _check_not_locked(key: str) -> None:
    attempts = _recent_failures(key)
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        seconds_left = LOGIN_LOCK_MINUTES * 60 - (time.monotonic() - attempts[0])
        minutes = max(1, round(seconds_left / 60))
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")


def _record_failure(key: str) -> int:
    """Remember a failed attempt. Returns how many attempts are left."""
    attempts = _recent_failures(key)
    attempts.append(time.monotonic())
    return max(0, LOGIN_MAX_ATTEMPTS - len(attempts))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/check-username")
def check_username_availability(username: str = "", db: Session = Depends(get_db)):
    """Used while typing on the sign-up page: is this username valid and free?"""
    try:
        clean = check_username(username)
    except ValueError as error:
        return {"username": username.strip().lower(), "valid": False, "available": False, "message": str(error)}
    taken = db.scalar(select(User.id).where(User.username == clean)) is not None
    return {
        "username": clean,
        "valid": True,
        "available": not taken,
        "message": _taken_message(clean) if taken else f'"{clean}" is available.',
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if body.username in body.password.lower():
        raise FieldError(422, "password", "Password can't contain your username.")
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise FieldError(status.HTTP_409_CONFLICT, "username", _taken_message(body.username))

    user = User(username=body.username, display_name=body.display_name,
                password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # Someone took it at the very same moment
        db.rollback()
        raise FieldError(status.HTTP_409_CONFLICT, "username", _taken_message(body.username))

    general = get_or_create_default_room(db)
    member = add_member(db, general, user.id)  # Everyone starts in "General"
    await manager.send_to_users(member_ids(db, general.id),
                                {"type": "member_joined", "room_id": general.id, "user": user_dict(user),
                                 "receipt": receipt_dict(member)},
                                skip_user=user.id)
    return _token_response(user)


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    key = _attempt_key(request, body.username)
    _check_not_locked(key)

    user = db.scalar(select(User).where(User.username == body.username))
    if user is None:
        _record_failure(key)
        raise FieldError(status.HTTP_401_UNAUTHORIZED, "username",
                         f'No account found with the username "{body.username}".')
    if not verify_password(body.password, user.password_hash):
        left = _record_failure(key)
        if left == 0:
            _check_not_locked(key)  # Raises the "try again in N minutes" error
        hint = f" {left} attempt{'s' if left != 1 else ''} left before a short lock." if left <= 2 else ""
        raise FieldError(status.HTTP_401_UNAUTHORIZED, "password", f"Incorrect password.{hint}")

    _failures.pop(key, None)  # Success: forget earlier mistakes
    return _token_response(user)


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_dict(user)
