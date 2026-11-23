"""
routers/attachments.py - Sending and downloading photos, videos, audio and files.

Upload:    POST /api/rooms/{room_id}/attachments   (multipart form: file, caption, client_id)
Download:  GET  /api/attachments/{id}?token=...

Images and videos are loaded by <img>/<video> tags, which can't send an
Authorization header, so downloads also accept the login token in the URL.
Either way, only members of the conversation can open its files.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import MESSAGE_MAX_LENGTH
from app.connections import manager
from app.database import get_db
from app.models import Attachment, Message, Room, User
from app.security import decode_token, get_current_user
from app.services import is_member, member_ids, message_dict, save_message, valid_reply_target
from app.storage import INLINE_CONTENT_TYPES, file_path, save_upload

router = APIRouter(prefix="/api", tags=["attachments"])
_bearer = HTTPBearer(auto_error=False)


@router.post("/rooms/{room_id}/attachments", status_code=status.HTTP_201_CREATED)
async def send_attachment(
    room_id: int,
    file: UploadFile = File(...),
    caption: str = Form("", max_length=MESSAGE_MAX_LENGTH),
    client_id: str = Form("", max_length=64),
    reply_to_id: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if db.get(Room, room_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That conversation doesn't exist.")
    if not is_member(db, room_id, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You're not in this conversation.")

    details = await save_upload(file)
    message = save_message(db, room_id, user.id, caption.strip(), attachment=Attachment(**details),
                           reply_to_id=valid_reply_target(db, room_id, reply_to_id))
    payload = message_dict(message)
    await manager.send_to_users(member_ids(db, room_id),
                                {"type": "message", "message": payload, "client_id": client_id})
    return payload


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
):
    user_id = decode_token(credentials.credentials if credentials else token)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to open this file.")

    attachment = db.get(Attachment, attachment_id)  # deleted-for-everyone files are gone
    message = db.get(Message, attachment.message_id) if attachment else None
    if message is None or not is_member(db, message.room_id, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")  # Don't reveal it exists

    path = file_path(attachment.stored_name)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This file is no longer available.")

    inline = attachment.content_type in INLINE_CONTENT_TYPES
    return FileResponse(
        path,
        media_type=attachment.content_type if inline else "application/octet-stream",
        filename=attachment.original_name,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"},
    )
