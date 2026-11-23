"""
config.py - All settings in one place.

Values come from environment variables or a local .env file, with safe defaults
for development. In production, always set SECRET_KEY.
"""

import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Relay")

# Used to sign login tokens (JWT). Anyone with this key can forge logins - keep it secret!
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = "dev-only-insecure-key-change-me"
    print("[warning] SECRET_KEY is not set - using an insecure development key.")

# SQLite file by default. For PostgreSQL use e.g.
#   postgresql+psycopg://user:password@localhost:5432/chat
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./chat.db")

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))  # 1 day

DEFAULT_ROOM_NAME = "General"      # Everyone joins this room when they sign up
MESSAGE_MAX_LENGTH = 2000
HISTORY_PAGE_SIZE = 50             # Messages loaded at a time

# Like WhatsApp: how long after sending you can edit, or delete for everyone
EDIT_WINDOW_MINUTES = int(os.getenv("EDIT_WINDOW_MINUTES", "15"))
DELETE_FOR_EVERYONE_HOURS = int(os.getenv("DELETE_FOR_EVERYONE_HOURS", "48"))

# File sharing
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")          # Where shared files are stored
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))     # Largest file people can send

# Sign-in protection: after this many wrong attempts, wait LOGIN_LOCK_MINUTES
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "10"))

# Anti-spam: at most RATE_LIMIT_MESSAGES per RATE_LIMIT_SECONDS for each connection
RATE_LIMIT_MESSAGES = 10
RATE_LIMIT_SECONDS = 5
