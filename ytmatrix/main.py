from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ytmatrix.certs import ensure_cert, mkcert_ca_installed
from ytmatrix.server import create_app
from ytmatrix.settings import Settings
from ytmatrix.store import FileStore

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    # Constructing Settings raises immediately if YOUTUBE_API_KEY is absent,
    # which is the point: fail at startup, not at the first search.
    settings = Settings()

    runtime_dir = Path(os.environ.get("YTMATRIX_RUNTIME_DIR", REPO_ROOT / "runtime"))
    cert_path = runtime_dir / "cert.pem"
    key_path = runtime_dir / "key.pem"
    kind = ensure_cert(cert_path, key_path)
    if kind == "self-signed" or (kind == "existing" and not mkcert_ca_installed()):
        print(
            "\n  Certificate is self-signed, so the browser will warn.\n"
            "  To get a trusted one:  mkcert -install  (then delete "
            f"{cert_path.parent}/ and restart)\n"
            "  Firefox additionally needs:  brew install nss\n",
            flush=True,
        )
    print(
        f"  Open https://localhost:{settings.port}/  "
        "(use localhost, not 127.0.0.1 -- YouTube refuses to embed into the IP)\n",
        flush=True,
    )

    app = create_app(
        store=FileStore(Path(os.environ.get("YTMATRIX_CACHE_DIR", REPO_ROOT / "cache"))),
        settings=settings,
        default_config_path=Path(os.environ.get("YTMATRIX_CONFIG_PATH", REPO_ROOT / "config.yaml")),
    )

    # Local development only. In production the Worker's asset binding serves
    # these and the container never sees the request.
    dist = REPO_ROOT / "dist"
    if dist.is_dir():
        # Cloudflare's static-asset serving strips ".html" from clean URLs, so
        # /config resolves to config.html there. Starlette's StaticFiles(html=
        # True) only does the equivalent for "/" -> index.html, not arbitrary
        # paths, so /config would 404 locally without this. This is the one
        # extra route the existing <a href="/config"> link already expects.
        @app.get("/config", include_in_schema=False)
        async def _config_page() -> FileResponse:
            return FileResponse(dist / "config.html")

        app.mount("/", StaticFiles(directory=dist, html=True), name="dist")

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )


if __name__ == "__main__":
    main()
