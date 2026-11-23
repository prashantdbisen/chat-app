"""
routers/messages.py - Edit and delete messages (like WhatsApp).

PATCH  /api/messages/{id}                 edit your own message (within EDIT_WINDOW_MINUTES)
DELETE /api/messages/{id}?scope=everyone  delete your own message for everyone
                                          (within DELETE_FOR_EVERYONE_HOURS)
DELETE /api/messages/{id}?scope=me        hide any message from your own chat only

Everyone in the conversation is told about edits and "delete for everyone"
instantly through the WebSocket. "Delete for me" only updates your own tabs.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import DELETE_FOR_EVERYONE_HOURS, EDIT_WINDOW_MINUTES
from app.connections import manager
from app.database import get_db
from app.models import HiddenMessage, Message, User
from app.schemas import MessageEdit
from app.security import get_current_user
from app.services import is_member, member_ids, message_dict, within
from app.storage import delete_file

router = APIRouter(prefix="/api/messages", tags=["messages"])


def _get_visible_message(db: Session, message_id: int, user: User) -> Message:
    message = db.get(Message, message_id)
    if message is None or not is_member(db, message.room_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found.")
    return message


@router.patch("/{message_id}")
async def edit_message(message_id: int, body: MessageEdit,
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    message = _get_visible_message(db, message_id, user)
    if message.sender_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only edit your own messages.")
    if message.deleted_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Deleted messages can't be edited.")
    if not within(message.created_at, timedelta(minutes=EDIT_WINDOW_MINUTES)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Messages can only be edited within {EDIT_WINDOW_MINUTES} minutes of sending.")
    content = body.content.strip()
    if not content and message.attachment is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A message can't be empty. Delete it instead.")
    if content != message.content:
        message.content = content
        message.edited_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(message)
        await manager.send_to_users(member_ids(db, message.room_id),
                                    {"type": "message_updated", "message": message_dict(message)})
    return message_dict(message)


@router.delete("/{message_id}")
async def delete_message(message_id: int, scope: Literal["everyone", "me"] = Query("me"),
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    message = _get_visible_message(db, message_id, user)

    if scope == "me":
        if db.get(HiddenMessage, (user.id, message.id)) is None:
            db.add(HiddenMessage(user_id=user.id, message_id=message.id))
            db.commit()
        await manager.send_to_user(user.id, {"type": "message_hidden", "room_id": message.room_id,
                                             "message_id": message.id})
        return {"ok": True}

    # scope == "everyone"
    if message.sender_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete your own messages for everyone.")
    if message.deleted_at is not None:
        return message_dict(message)
    if not within(message.created_at, timedelta(hours=DELETE_FOR_EVERYONE_HOURS)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This message is too old to delete for everyone. You can still delete it for yourself.")

    message.deleted_at = datetime.now(timezone.utc)
    message.content = ""
    if message.attachment is not None:  # Remove the file too, for privacy
        delete_file(message.attachment.stored_name)
        message.attachment = None
    db.commit()
    db.refresh(message)
    await manager.send_to_users(member_ids(db, message.room_id),
                                {"type": "message_updated", "message": message_dict(message)})
    return message_dict(message)
