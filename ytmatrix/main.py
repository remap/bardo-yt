from __future__ import annotations

from pathlib import Path

import uvicorn

from ytmatrix.certs import ensure_self_signed_cert
from ytmatrix.server import create_app
from ytmatrix.settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    # Constructing Settings raises immediately if YOUTUBE_API_KEY is absent,
    # which is the point: fail at startup, not at the first search.
    settings = Settings()

    cert_path = REPO_ROOT / "runtime" / "cert.pem"
    key_path = REPO_ROOT / "runtime" / "key.pem"
    ensure_self_signed_cert(cert_path, key_path)

    app = create_app(
        config_path=REPO_ROOT / "config.yaml",
        cache_dir=REPO_ROOT / "cache",
        settings=settings,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )


if __name__ == "__main__":
    main()
