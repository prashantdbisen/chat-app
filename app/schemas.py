"""
schemas.py - Shapes of the data the API accepts, with clear, human error messages.

The same rules are repeated in the browser (app.js) so people see problems while
typing - but the server ALWAYS checks again, because browser checks can be skipped.
"""

import re

from pydantic import BaseModel, field_validator

from app.config import MESSAGE_MAX_LENGTH

_USERNAME_CHARS = re.compile(r"^[a-z0-9_]+$")


def check_username(value: str) -> str:
    """Return the cleaned (lowercase) username, or raise ValueError with a clear reason."""
    value = (value or "").strip().lower()
    if not value:
        raise ValueError("Enter a username.")
    if " " in value:
        raise ValueError("Username can't contain spaces.")
    if len(value) < 3:
        raise ValueError("Username must be at least 3 characters.")
    if len(value) > 20:
        raise ValueError("Username can be at most 20 characters.")
    if not _USERNAME_CHARS.match(value):
        raise ValueError("Username can only use letters, numbers and underscores (_).")
    if not value[0].isalpha():
        raise ValueError("Username must start with a letter.")
    return value


PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 16


def check_password(value: str) -> str:
    """Rules for NEW passwords (sign-up). Raises ValueError with a clear reason."""
    if not value:
        raise ValueError("Enter a password.")
    if any(character.isspace() for character in value):
        raise ValueError("Password can't contain spaces.")
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password can be at most {PASSWORD_MAX_LENGTH} characters.")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError("Password must include at least one letter.")
    if not re.search(r"\d", value):
        raise ValueError("Password must include at least one number.")
    return value


class RegisterRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str) -> str:
        return check_username(value)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value: str) -> str:
        return check_password(value)


class LoginRequest(BaseModel):
    """Sign-in only checks that something was typed: older accounts may have been
    created before the current password rules, and they must still be able to sign in."""

    username: str
    password: str

    @field_validator("username")
    @classmethod
    def username_given(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("Enter your username.")
        if len(value) > 50:
            raise ValueError("That username is too long.")
        return value

    @field_validator("password")
    @classmethod
    def password_given(cls, value: str) -> str:
        if not value:
            raise ValueError("Enter your password.")
        if len(value) > 200:
            raise ValueError("That password is too long.")
        return value


class RoomCreate(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def tidy(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Enter a group name.")
        if len(value) > 50:
            raise ValueError("Group name can be at most 50 characters.")
        return value


class MessageEdit(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def not_too_long(cls, value: str) -> str:
        if len(value) > MESSAGE_MAX_LENGTH:
            raise ValueError(f"Messages can be up to {MESSAGE_MAX_LENGTH} characters.")
        return value
