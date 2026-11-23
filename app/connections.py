"""
connections.py - Keeps track of who is connected right now.

One user can be connected from several tabs/devices, so each user id maps to a
set of WebSocket connections. A user is "online" while they have at least one.

Note: this lives in memory, so it works for ONE server process. To run several
server instances, you would share these events through Redis pub/sub instead.
"""

from collections import defaultdict

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = defaultdict(set)

    def add(self, user_id: int, websocket: WebSocket) -> bool:
        """Register a connection. Returns True if the user just came online."""
        came_online = not self._connections.get(user_id)
        self._connections[user_id].add(websocket)
        return came_online

    def remove(self, user_id: int, websocket: WebSocket) -> bool:
        """Forget a connection. Returns True if the user just went offline."""
        sockets = self._connections.get(user_id)
        if not sockets:
            return False
        sockets.discard(websocket)
        if not sockets:
            del self._connections[user_id]
            return True
        return False

    def online_user_ids(self) -> list[int]:
        return sorted(self._connections)

    def is_online(self, user_id: int) -> bool:
        return bool(self._connections.get(user_id))

    async def send_to_user(self, user_id: int, data: dict, skip: WebSocket | None = None) -> None:
        for websocket in list(self._connections.get(user_id, ())):
            if websocket is skip:
                continue
            try:
                await websocket.send_json(data)
            except Exception:
                pass  # That connection is closing; its own handler will clean it up

    async def send_to_users(self, user_ids, data: dict, skip_user: int | None = None) -> None:
        for user_id in set(user_ids):
            if user_id != skip_user:
                await self.send_to_user(user_id, data)

    async def broadcast(self, data: dict, skip_user: int | None = None) -> None:
        await self.send_to_users(list(self._connections), data, skip_user)


manager = ConnectionManager()
