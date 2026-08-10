from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ytmatrix import cache, youtube
from ytmatrix.config import Config, load_config, save_config
from ytmatrix.settings import Settings
from ytmatrix.ws import ConnectionManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def search_params_for(config: Config) -> dict[str, str]:
    return youtube.build_params(
        config.query,
        config.search.order.value,
        config.search.video_duration.value,
        config.search.safe_search.value,
        config.search.relevance_language,
    )


async def resolve_videos(config: Config, cache_dir: Path, api_key: str) -> dict:
    """Return the video list for this config, spending quota only when required."""
    params = search_params_for(config)

    items = cache.read(cache_dir, params, config.cache.ttl_hours)
    if items is not None:
        return {"items": items, "from_cache": True, "note": None}

    try:
        items = await youtube.search(params, api_key)
    except youtube.QuotaExceededError:
        # Expired-but-present beats blank: keep the wall showing something.
        stale = cache.read(cache_dir, params, config.cache.ttl_hours, allow_stale=True)
        if stale is None:
            raise
        return {"items": stale, "from_cache": True, "note": "quota_exceeded_stale"}

    cache.write(cache_dir, params, items)
    return {"items": items, "from_cache": False, "note": None}


def videos_message(config: Config, resolved: dict) -> dict:
    items = resolved["items"]
    cells = config.grid.cells
    video_ids = [item["video_id"] for item in items]
    note = resolved["note"] or ("no_results" if not video_ids else None)
    return {
        "type": "videos",
        "video_ids": video_ids[:cells],
        "reserves": video_ids[cells:],
        "titles": {item["video_id"]: item["title"] for item in items},
        "from_cache": resolved["from_cache"],
        "note": note,
    }


def create_app(config_path: Path, cache_dir: Path, settings: Settings) -> FastAPI:
    app = FastAPI(title="yt matrix")
    manager = ConnectionManager()

    async def current_videos() -> dict:
        config = load_config(config_path)
        try:
            resolved = await resolve_videos(config, cache_dir, settings.youtube_api_key)
        except youtube.QuotaExceededError as exc:
            raise HTTPException(
                status_code=503,
                detail="YouTube API daily quota exceeded and no cached results are available.",
            ) from exc
        except youtube.SearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return videos_message(config, resolved)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/")
    async def player_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "player.html")

    @app.get("/config")
    async def config_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "config.html")

    @app.get("/api/config")
    async def get_config() -> dict:
        return load_config(config_path).model_dump(mode="json")

    @app.put("/api/config")
    async def put_config(payload: dict) -> dict:
        try:
            new_config = Config.model_validate(payload)
        except ValidationError as exc:
            # include_context=False matters: the context of a custom validator
            # error holds the original ValueError object, which is not JSON
            # serializable and makes FastAPI raise while encoding the 422.
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_url=False, include_context=False),
            ) from exc

        previous = load_config(config_path)
        save_config(new_config, config_path)

        await manager.broadcast({"type": "config", "config": new_config.model_dump(mode="json")})

        # Only a change to the search parameters can change the video set.
        # Cosmetic edits must not tear down eight running players.
        if search_params_for(previous) != search_params_for(new_config) or (
            previous.grid.cells != new_config.grid.cells
        ):
            await manager.broadcast(await current_videos())

        return {"status": "ok"}

    @app.post("/api/cache-status")
    async def cache_status(payload: dict) -> dict:
        try:
            candidate = Config.model_validate(payload)
        except ValidationError as exc:
            # include_context=False matters: the context of a custom validator
            # error holds the original ValueError object, which is not JSON
            # serializable and makes FastAPI raise while encoding the 422.
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_url=False, include_context=False),
            ) from exc
        params = search_params_for(candidate)
        hit = cache.read(cache_dir, params, candidate.cache.ttl_hours) is not None
        return {"would_hit": hit, "quota_cost": 0 if hit else 100}

    @app.get("/api/videos")
    async def get_videos() -> dict:
        return await current_videos()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            while True:
                # Server->client channel only; this read exists to detect close.
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
