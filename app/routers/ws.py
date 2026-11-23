"""
routers/ws.py - The real-time part: one WebSocket per open browser tab.

Connect:  ws://<host>/ws?token=<login token>
(Browsers can't add headers to WebSockets, so the token goes in the URL.)

Events the browser sends               Events the server sends
--------------------------------       ---------------------------------------------
message   {room_id, content,           message          a new message in one of your rooms
           client_id, reply_to_id}     message_updated  a message was edited or deleted for everyone
typing    {room_id, is_typing}         message_hidden   you deleted a message "for me" (other tab)
delivered {room_id, message_id}        receipt          someone received/read messages (ticks)
read      {room_id, message_id}        typing           someone is (not) typing
ping      {}                           presence         someone came online / went offline
                                       online_users     everyone online, sent when you connect
                                       room_added       someone started a direct chat with you
                                       member_joined / member_left
                                       error / pong

Ticks work like WhatsApp: the receiving browser sends "delivered" as soon as a
message arrives and "read" when you actually look at the chat.
"""

import json
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import MESSAGE_MAX_LENGTH, RATE_LIMIT_MESSAGES, RATE_LIMIT_SECONDS
from app.connections import manager
from app.database import SessionLocal
from app.models import User
from app.security import decode_token
from app.services import (is_member, iso, mark_delivered, mark_read, member_ids, message_dict,
                          receipt_dict, save_message, valid_reply_target)

router = APIRouter()

CLOSE_UNAUTHORIZED = 4401  # Custom close code: the browser should sign the user out


class RateLimiter:
    """Allows at most `limit` actions per `seconds` (a sliding window)."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit, self.seconds = limit, seconds
        self.times: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.times and now - self.times[0] > self.seconds:
            self.times.popleft()
        if len(self.times) >= self.limit:
            return False
        self.times.append(now)
        return True


def _set_last_seen(user_id: int) -> str | None:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            return None
        user.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        return iso(user.last_seen_at)


@router.websocket("/ws")
async def chat_socket(websocket: WebSocket, token: str = ""):
    await websocket.accept()

    user_id = decode_token(token)
    with SessionLocal() as db:
        user = db.get(User, user_id) if user_id else None
    if user is None:
        await websocket.send_json({"type": "error", "detail": "Your session has expired. Sign in again."})
        await websocket.close(code=CLOSE_UNAUTHORIZED)
        return

    came_online = manager.add(user.id, websocket)
    limiter = RateLimiter(RATE_LIMIT_MESSAGES, RATE_LIMIT_SECONDS)
    try:
        await websocket.send_json({"type": "online_users", "user_ids": manager.online_user_ids()})
        if came_online:
            await manager.broadcast(
                {"type": "presence", "user_id": user.id, "username": user.display_name, "online": True,
                 "last_seen": _set_last_seen(user.id)},
                skip_user=user.id,
            )

        while True:
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError
            except ValueError:
                await websocket.send_json({"type": "error", "detail": "Messages must be JSON objects."})
                continue
            await _handle_event(websocket, user, event, limiter)

    except WebSocketDisconnect:
        pass
    finally:
        if manager.remove(user.id, websocket):
            await manager.broadcast(
                {"type": "presence", "user_id": user.id, "username": user.display_name, "online": False,
                 "last_seen": _set_last_seen(user.id)}
            )


async def _send_receipt(db, room_id: int, membership) -> None:
    """Tell everyone in the room how far this member has received/read (updates ticks)."""
    await manager.send_to_users(member_ids(db, room_id),
                                {"type": "receipt", "room_id": room_id, **receipt_dict(membership)})


async def _handle_event(websocket: WebSocket, user: User, event: dict, limiter: RateLimiter) -> None:
    kind = event.get("type")

    if kind == "ping":
        await websocket.send_json({"type": "pong"})
        return

    room_id = event.get("room_id")
    if not isinstance(room_id, int):
        await websocket.send_json({"type": "error", "detail": "room_id must be a number."})
        return

    with SessionLocal() as db:
        if not is_member(db, room_id, user.id):
            await websocket.send_json({"type": "error", "detail": "You're not in this conversation."})
            return

        if kind == "message":
            content = str(event.get("content", "")).strip()
            client_id = str(event.get("client_id", ""))[:64]
            if not content:
                return
            if len(content) > MESSAGE_MAX_LENGTH:
                await websocket.send_json({"type": "error", "client_id": client_id,
                                           "detail": f"Messages can be up to {MESSAGE_MAX_LENGTH} characters."})
                return
            if not limiter.allow():
                await websocket.send_json({"type": "error", "client_id": client_id,
                                           "detail": "You're sending messages too fast. Wait a few seconds."})
                return
            reply_to_id = valid_reply_target(db, room_id, event.get("reply_to_id"))
            message = save_message(db, room_id, user.id, content, reply_to_id=reply_to_id)
            payload = {"type": "message", "message": message_dict(message), "client_id": client_id}
            await manager.send_to_users(member_ids(db, room_id), payload)

        elif kind == "typing":
            await manager.send_to_users(
                member_ids(db, room_id),
                {"type": "typing", "room_id": room_id, "user": {"id": user.id, "username": user.display_name},
                 "is_typing": bool(event.get("is_typing"))},
                skip_user=user.id,
            )

        elif kind in ("delivered", "read"):
            message_id = event.get("message_id")
            if isinstance(message_id, int):
                update = mark_delivered if kind == "delivered" else mark_read
                membership = update(db, room_id, user.id, message_id)
                if membership is not None:
                    await _send_receipt(db, room_id, membership)

        else:
            await websocket.send_json({"type": "error", "detail": f"Unknown event type: {kind}"})
