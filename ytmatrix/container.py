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

import logging
import os
import sys

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


def _configure_logging() -> None:
    """INFO to stderr, which is what Cloudflare captures from a container.

    Without this, Python's handler of last resort emits WARNING and above only,
    so every INFO line would vanish -- including the per-phase timings that say
    where a slow query actually spent its time.
    """
    logging.basicConfig(
        level=os.environ.get("YTMATRIX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    _configure_logging()
    settings = Settings()
    store = R2Store(
        r2_client(
            account_id=_required("R2_ACCOUNT_ID"),
            access_key_id=_required("R2_ACCESS_KEY_ID"),
            secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
            # Optional, and only ever set when pointing this image at a local
            # S3-compatible store to exercise it without a Cloudflare account.
            # Unset -- the production case -- means real R2.
            endpoint_url=os.environ.get("R2_ENDPOINT_URL") or None,
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
