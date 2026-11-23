"""
main.py - Creates the FastAPI app and connects all the pieces.

Run it with:   python run.py        (or: uvicorn app.main:app --reload)
API docs at:   http://127.0.0.1:8000/docs
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import (APP_NAME, DELETE_FOR_EVERYONE_HOURS, EDIT_WINDOW_MINUTES, MAX_UPLOAD_MB,
                        MESSAGE_MAX_LENGTH)
from app.database import Base, SessionLocal, engine
from app.errors import install_error_handlers
from app.migrations import add_missing_columns
from app.routers import attachments, auth, messages, rooms, users, ws
from app.services import get_or_create_default_room

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once at startup: create tables and the default room if missing
    Base.metadata.create_all(bind=engine)
    add_missing_columns(engine, Base.metadata)  # Upgrade older databases
    with SessionLocal() as db:
        get_or_create_default_room(db)
    yield


app = FastAPI(title=f"{APP_NAME} chat API", lifespan=lifespan)
install_error_handlers(app)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(rooms.router)
app.include_router(messages.router)
app.include_router(attachments.router)
app.include_router(ws.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config", tags=["health"])
def public_config():
    """Limits the browser needs to know (to show or hide Edit / Delete for everyone)."""
    return {
        "edit_window_minutes": EDIT_WINDOW_MINUTES,
        "delete_for_everyone_hours": DELETE_FOR_EVERYONE_HOURS,
        "max_upload_mb": MAX_UPLOAD_MB,
        "message_max_length": MESSAGE_MAX_LENGTH,
    }


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok"}
