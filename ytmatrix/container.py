"""Entry point inside the Cloudflare container.

Nothing here generates or presents a certificate: Cloudflare terminates TLS at
the edge and the Worker reaches this process over plain HTTP on a private
port. `main.py` remains the local-development entry point and keeps its
self-signed cert -- gotcha 11 still applies there, and only there.

Persistence is R2 rather than disk. The container's filesystem is a fresh copy
of the image on every start, so anything written to it is gone by the next
request that wakes the instance.
"""

from __future__ import annotations

import os

import uvicorn

from ytmatrix.server import create_app
from ytmatrix.settings import Settings
from ytmatrix.store import R2Store, r2_client


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        # Fail at startup, not at the first search -- the same rule the
        # YouTube key follows in settings.py.
        raise RuntimeError(f"{name} is not set")
    return value


def main() -> None:
    settings = Settings()
    store = R2Store(
        r2_client(
            account_id=_required("R2_ACCOUNT_ID"),
            access_key_id=_required("R2_ACCESS_KEY_ID"),
            secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
        ),
        bucket=_required("R2_BUCKET"),
    )
    uvicorn.run(
        create_app(store=store, settings=settings),
        host="0.0.0.0",  # the container's port is not public; only the Worker can reach it
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
