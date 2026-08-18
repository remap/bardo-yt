from __future__ import annotations

import json

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        stale: list[WebSocket] = []
        # Iterate a snapshot, not the live set. Every `await` below is a
        # suspension point, so with several users connected a tab opening or
        # closing mid-broadcast mutated the set underneath this loop and raised
        # `RuntimeError: Set changed size during iteration` -- which abandoned
        # the broadcast partway, leaving some walls un-updated, and propagated
        # out of `put_config` as a 500. Somebody else opening a tab could fail
        # your save.
        for connection in list(self._connections):
            try:
                await connection.send_text(payload)
            except Exception:  # noqa: BLE001 - any per-connection failure marks it stale
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
