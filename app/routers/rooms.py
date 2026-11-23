"""
routers/rooms.py - Group rooms, direct messages, and message history.

Routes that need to notify other people in real time are `async`, so they can
push WebSocket events through the connection manager.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import HISTORY_PAGE_SIZE
from app.connections import manager
from app.database import get_db
from app.models import Room, RoomMember, User
from app.schemas import RoomCreate
from app.security import get_current_user
from app.services import (add_member, find_direct_room, is_member, member_ids, message_history,
                          receipt_dict, room_dict, rooms_for_user, user_dict)

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


def _get_room(db: Session, room_id: int) -> Room:
    room = db.get(Room, room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That conversation doesn't exist.")
    return room


def _require_member(db: Session, room_id: int, user_id: int) -> None:
    if not is_member(db, room_id, user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You're not in this conversation.")


@router.get("")
def my_rooms(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return rooms_for_user(db, user.id)


@router.get("/discover")
def discover_groups(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Group rooms you haven't joined yet."""
    mine = select(RoomMember.room_id).where(RoomMember.user_id == user.id)
    counts = (
        select(Room.id, Room.name, func.count(RoomMember.user_id))
        .join(RoomMember, isouter=True)
        .where(Room.is_direct.is_(False), Room.id.not_in(mine))
        .group_by(Room.id, Room.name)
        .order_by(Room.name)
    )
    return [{"id": rid, "name": name, "member_count": count} for rid, name, count in db.execute(counts)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_group(body: RoomCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    room = Room(name=body.name, is_direct=False, created_by=user.id)
    db.add(room)
    db.commit()
    add_member(db, room, user.id)
    return room_dict(db, room, user.id)


@router.post("/{room_id}/join")
async def join_group(room_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    room = _get_room(db, room_id)
    if room.is_direct:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't join someone else's direct messages.")
    member = add_member(db, room, user.id)
    if member is not None:
        await manager.send_to_users(member_ids(db, room_id),
                                    {"type": "member_joined", "room_id": room_id, "user": user_dict(user),
                                     "receipt": receipt_dict(member)},
                                    skip_user=user.id)
    return room_dict(db, room, user.id)


@router.post("/{room_id}/leave")
async def leave_group(room_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    room = _get_room(db, room_id)
    _require_member(db, room_id, user.id)
    if room.is_direct:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Direct messages can't be left.")
    db.delete(db.get(RoomMember, (room_id, user.id)))
    db.commit()
    await manager.send_to_users(member_ids(db, room_id),
                                {"type": "member_left", "room_id": room_id, "user": user_dict(user)})
    return {"ok": True}


@router.post("/direct/{username}")
async def open_direct(username: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Open (or create) a 1-to-1 conversation with someone."""
    other = db.scalar(select(User).where(User.username == username.lower()))
    if other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No one is called {username}.")
    if other.id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't message yourself.")

    room = find_direct_room(db, user.id, other.id)
    if room is None:
        room = Room(is_direct=True, created_by=user.id)
        db.add(room)
        db.commit()
        add_member(db, room, user.id)
        add_member(db, room, other.id)
        await manager.send_to_user(other.id, {"type": "room_added", "room": room_dict(db, room, other.id)})
    return room_dict(db, room, user.id)


@router.get("/{room_id}/messages")
def get_messages(
    room_id: int,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(HISTORY_PAGE_SIZE, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_room(db, room_id)
    _require_member(db, room_id, user.id)
    messages, has_more = message_history(db, room_id, user.id, before_id, limit)
    return {"messages": messages, "has_more": has_more}
