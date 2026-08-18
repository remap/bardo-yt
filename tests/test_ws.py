"""The WebSocket fan-out, which every connected wall shares."""

import asyncio

from ytmatrix.ws import ConnectionManager


class _Socket:
    """A stand-in WebSocket. `on_send` fires while the broadcast is suspended,
    which is exactly when a real tab would open or close."""

    def __init__(self, on_send=None):
        self.sent: list[str] = []
        self.accepted = False
        self._on_send = on_send

    async def accept(self):
        self.accepted = True

    async def send_text(self, payload: str) -> None:
        # Yield first, so this behaves like a real await inside the loop.
        await asyncio.sleep(0)
        if self._on_send is not None:
            self._on_send()
        self.sent.append(payload)


async def test_a_tab_opening_mid_broadcast_does_not_break_it():
    """Regression: `broadcast` iterated the live set with an await inside the
    loop, so a connect or disconnect from another user during the send raised
    `RuntimeError: Set changed size during iteration`. That abandoned the
    broadcast partway -- leaving some walls un-updated -- and surfaced as a 500
    from whichever config save triggered it.

    The arrival is registered directly rather than through `connect`, which is a
    coroutine and would therefore land *after* the loop rather than during it.
    Reaching into the set is the point: this test is about iteration safety, and
    scheduling the connect politely is what made an earlier version of it pass
    against the bug it was supposed to catch.
    """
    manager = ConnectionManager()
    latecomer = _Socket()

    first = _Socket(on_send=lambda: manager._connections.add(latecomer))
    second = _Socket()
    await manager.connect(first)
    await manager.connect(second)

    await manager.broadcast({"type": "config"})

    # Both sockets present at the start were served, and nothing raised.
    assert len(first.sent) == 1
    assert len(second.sent) == 1


async def test_a_tab_closing_mid_broadcast_does_not_break_it():
    manager = ConnectionManager()
    doomed = _Socket()
    survivor = _Socket()

    def close_the_other():
        manager.disconnect(doomed)

    first = _Socket(on_send=close_the_other)
    await manager.connect(first)
    await manager.connect(doomed)
    await manager.connect(survivor)

    await manager.broadcast({"type": "config"})

    assert len(first.sent) == 1
    assert len(survivor.sent) == 1


async def test_a_broken_connection_is_dropped_and_the_rest_still_get_it():
    class _Broken(_Socket):
        async def send_text(self, payload):
            raise RuntimeError("this socket is gone")

    manager = ConnectionManager()
    broken = _Broken()
    healthy = _Socket()
    await manager.connect(broken)
    await manager.connect(healthy)

    await manager.broadcast({"type": "config"})

    assert len(healthy.sent) == 1
    assert manager.connection_count == 1
