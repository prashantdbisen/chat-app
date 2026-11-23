"""
models.py - Database tables.

    users            one row per account (with "last seen" time)
    rooms            group chats and direct (1-to-1) chats
    room_members     who is in which room + how far they've received/read (for ticks & unread counts)
    messages         every message (can be a reply, edited, or deleted for everyone)
    attachments      a photo, video, audio clip or file attached to a message
    hidden_messages  messages someone deleted "for me" only
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)  # None for direct chats
    is_direct: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    members: Mapped[list["RoomMember"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", lazy="selectin"
    )


class RoomMember(Base):
    __tablename__ = "room_members"

    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # Newest message id this member has RECEIVED (grey ✓✓) and READ (blue ✓✓)
    last_delivered_message_id: Mapped[int | None] = mapped_column(Integer, default=0, nullable=True)
    last_read_message_id: Mapped[int] = mapped_column(Integer, default=0)

    room: Mapped[Room] = relationship(back_populates="members")
    user: Mapped[User] = relationship(lazy="joined")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    reply_to_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sender: Mapped[User] = relationship(lazy="joined")
    reply_to: Mapped["Message | None"] = relationship(remote_side=[id])
    attachment: Mapped["Attachment | None"] = relationship(
        back_populates="message", uselist=False, lazy="selectin", cascade="all, delete-orphan"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True, index=True)
    stored_name: Mapped[str] = mapped_column(String(64))       # random name on disk
    original_name: Mapped[str] = mapped_column(String(255))    # name shown to people
    content_type: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))              # image, video, audio or file
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    message: Mapped[Message] = relationship(back_populates="attachment")


class HiddenMessage(Base):
    """A message one person deleted 'for me' - everyone else still sees it."""

    __tablename__ = "hidden_messages"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), primary_key=True)
