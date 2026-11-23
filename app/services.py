"""
services.py - Reusable database logic shared by the REST API and the WebSocket.

Keeping this separate from the routes means the same rules (who can see what,
how messages are saved) apply everywhere, and it is easy to test.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import DEFAULT_ROOM_NAME
from app.models import HiddenMessage, Message, Room, RoomMember, User


# ---------------------------------------------------------------------------
# Turning database rows into JSON-friendly dicts
# ---------------------------------------------------------------------------
def as_utc(moment: datetime) -> datetime:
    """SQLite forgets time zones, so treat stored times as UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def iso(moment) -> str | None:
    if moment is None:
        return None
    return as_utc(moment).isoformat().replace("+00:00", "Z")


def user_dict(user: User) -> dict:
    # "username" is the display version (e.g. "Alice"); sign-in uses the lowercase one
    return {"id": user.id, "username": user.display_name, "last_seen": iso(user.last_seen_at)}


def attachment_dict(attachment) -> dict:
    return {
        "id": attachment.id,
        "kind": attachment.kind,
        "name": attachment.original_name,
        "size": attachment.size,
        "content_type": attachment.content_type,
        "url": f"/api/attachments/{attachment.id}",
    }


def reply_summary(message: Message) -> dict:
    """The small quote shown above a reply."""
    deleted = message.deleted_at is not None
    attachment = None if deleted or not message.attachment else message.attachment
    return {
        "id": message.id,
        "sender": user_dict(message.sender),
        "content": "" if deleted else message.content[:200],
        "attachment_kind": attachment.kind if attachment else None,
        "attachment_name": attachment.original_name if attachment else None,
        "deleted": deleted,
    }


def message_dict(message: Message) -> dict:
    deleted = message.deleted_at is not None
    return {
        "id": message.id,
        "room_id": message.room_id,
        "sender": user_dict(message.sender),
        "content": "" if deleted else message.content,
        "attachment": attachment_dict(message.attachment) if message.attachment and not deleted else None,
        "reply_to": reply_summary(message.reply_to) if message.reply_to else None,
        "created_at": iso(message.created_at),
        "edited": message.edited_at is not None,
        "deleted": deleted,
    }


def receipt_dict(member: RoomMember) -> dict:
    read = member.last_read_message_id
    return {
        "user_id": member.user_id,
        "delivered_id": max(member.last_delivered_message_id, read),  # read implies delivered
        "read_id": read,
    }


def _hidden_ids(user_id: int):
    return select(HiddenMessage.message_id).where(HiddenMessage.user_id == user_id)


def room_dict(db: Session, room: Room, me_id: int) -> dict:
    """A room as seen by one user: unread count, last visible message, and read receipts."""
    membership = next((m for m in room.members if m.user_id == me_id), None)
    last_read = membership.last_read_message_id if membership else 0
    unread = db.scalar(
        select(func.count(Message.id)).where(
            Message.room_id == room.id, Message.id > last_read, Message.sender_id != me_id,
            Message.id.not_in(_hidden_ids(me_id)),
        )
    ) or 0
    last = db.scalar(
        select(Message).where(Message.room_id == room.id, Message.id.not_in(_hidden_ids(me_id)))
        .order_by(Message.id.desc()).limit(1)
    )
    return {
        "id": room.id,
        "name": room.name,
        "is_direct": room.is_direct,
        "members": sorted((user_dict(m.user) for m in room.members), key=lambda u: u["username"].lower()),
        "receipts": [receipt_dict(m) for m in room.members],
        "unread": unread,
        "last_message": message_dict(last) if last else None,
        "created_at": iso(room.created_at),
    }


# ---------------------------------------------------------------------------
# Rooms & membership
# ---------------------------------------------------------------------------
def get_or_create_default_room(db: Session) -> Room:
    room = db.scalar(select(Room).where(Room.name == DEFAULT_ROOM_NAME, Room.is_direct.is_(False)))
    if room is None:
        room = Room(name=DEFAULT_ROOM_NAME, is_direct=False)
        db.add(room)
        db.commit()
    return room


def is_member(db: Session, room_id: int, user_id: int) -> bool:
    return db.get(RoomMember, (room_id, user_id)) is not None


def member_ids(db: Session, room_id: int) -> list[int]:
    return list(db.scalars(select(RoomMember.user_id).where(RoomMember.room_id == room_id)))


def latest_message_id(db: Session, room_id: int) -> int:
    return db.scalar(select(func.max(Message.id)).where(Message.room_id == room_id)) or 0


def add_member(db: Session, room: Room, user_id: int) -> RoomMember | None:
    """Add someone to a room. Older messages count as already received and read,
    so new members don't get a pile of unread badges (and old ticks stay correct)."""
    if is_member(db, room.id, user_id):
        return None
    latest = latest_message_id(db, room.id)
    member = RoomMember(room_id=room.id, user_id=user_id,
                        last_delivered_message_id=latest, last_read_message_id=latest)
    db.add(member)
    db.commit()
    db.refresh(room)
    return member


def rooms_for_user(db: Session, user_id: int) -> list[dict]:
    """All rooms a user is in, most recently active first."""
    rooms = db.scalars(select(Room).join(RoomMember).where(RoomMember.user_id == user_id)).all()
    result = [room_dict(db, room, user_id) for room in rooms]
    result.sort(key=lambda r: (r["last_message"] or {}).get("created_at") or r["created_at"], reverse=True)
    return result


def find_direct_room(db: Session, user_a: int, user_b: int):
    """The existing 1-to-1 room between two users, or None."""
    rooms_a = select(RoomMember.room_id).where(RoomMember.user_id == user_a)
    rooms_b = select(RoomMember.room_id).where(RoomMember.user_id == user_b)
    return db.scalar(
        select(Room).where(Room.is_direct.is_(True), Room.id.in_(rooms_a), Room.id.in_(rooms_b))
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def valid_reply_target(db: Session, room_id: int, reply_to_id) -> int | None:
    """Only allow replying to a non-deleted message in the same room."""
    if not isinstance(reply_to_id, int):
        return None
    target = db.get(Message, reply_to_id)
    if target is None or target.room_id != room_id or target.deleted_at is not None:
        return None
    return target.id


def save_message(db: Session, room_id: int, sender_id: int, content: str,
                 attachment=None, reply_to_id: int | None = None) -> Message:
    message = Message(room_id=room_id, sender_id=sender_id, content=content, reply_to_id=reply_to_id)
    if attachment is not None:
        message.attachment = attachment
    db.add(message)
    db.flush()  # Gives the message its id
    mark_read(db, room_id, sender_id, message.id, commit=False)  # You've "read" your own message
    db.commit()
    db.refresh(message)
    return message


def mark_delivered(db: Session, room_id: int, user_id: int, message_id: int) -> RoomMember | None:
    """Move the 'received' marker forward. Returns the membership if it changed."""
    membership = db.get(RoomMember, (room_id, user_id))
    if membership is None:
        return None
    message_id = min(message_id, latest_message_id(db, room_id))  # can't receive the future
    if message_id > membership.last_delivered_message_id:
        membership.last_delivered_message_id = message_id
        db.commit()
        return membership
    return None


def mark_read(db: Session, room_id: int, user_id: int, message_id: int, commit: bool = True) -> RoomMember | None:
    """Move the 'read' marker (and 'received') forward. Returns the membership if it changed."""
    membership = db.get(RoomMember, (room_id, user_id))
    if membership is None:
        return None
    if commit:
        message_id = min(message_id, latest_message_id(db, room_id))
    changed = False
    if message_id > membership.last_read_message_id:
        membership.last_read_message_id = message_id
        changed = True
    if message_id > membership.last_delivered_message_id:
        membership.last_delivered_message_id = message_id
        changed = True
    if changed and commit:
        db.commit()
    return membership if changed else None


def message_history(db: Session, room_id: int, user_id: int, before_id: int | None,
                    limit: int) -> tuple[list[dict], bool]:
    """Newest `limit` messages this user hasn't hidden (older than before_id), oldest-first."""
    query = select(Message).where(Message.room_id == room_id, Message.id.not_in(_hidden_ids(user_id)))
    if before_id:
        query = query.where(Message.id < before_id)
    rows = list(db.scalars(query.order_by(Message.id.desc()).limit(limit + 1)))
    has_more = len(rows) > limit
    return [message_dict(m) for m in reversed(rows[:limit])], has_more


def within(moment: datetime, window: timedelta) -> bool:
    return datetime.now(timezone.utc) - as_utc(moment) <= window
