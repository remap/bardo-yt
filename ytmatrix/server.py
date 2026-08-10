from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ytmatrix import budget, cache, gemini, letterbox, motion, querylog, wallstate, youtube
from ytmatrix.config import Config, load_config, save_config
from ytmatrix.settings import Settings
from ytmatrix.ws import ConnectionManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class BudgetExceededError(RuntimeError):
    """The self-imposed daily ceiling would be crossed by another search."""


def search_params_for(config: Config, query: str | None = None) -> dict[str, str]:
    return youtube.build_params(
        query or config.query,
        config.search.order.value,
        config.search.video_duration.value,
        config.search.safe_search.value,
        config.search.relevance_language,
        config.search.video_license.value,
    )


async def resolve_videos(
    config: Config, cache_dir: Path, api_key: str, query: str | None = None
) -> dict:
    """Return the video list for this config, spending quota only when required."""
    params = search_params_for(config, query)

    items = cache.read(cache_dir, params, config.cache.ttl_hours)
    if items is not None:
        return {"items": items, "from_cache": True, "note": None}

    # Checked before the call, not after: the point is to not spend the unit.
    if budget.would_exceed(cache_dir, config.quota.daily_limit_units):
        stale = cache.read(cache_dir, params, config.cache.ttl_hours, allow_stale=True)
        if stale is not None:
            return {"items": stale, "from_cache": True, "note": "budget_exceeded_stale"}
        raise BudgetExceededError(
            f"daily search budget of {config.quota.daily_limit_units} units is spent "
            f"({budget.spent(cache_dir)} used). Raise quota.daily_limit_units to continue."
        )

    try:
        items = await youtube.search(params, api_key)
    except youtube.QuotaExceededError:
        # Expired-but-present beats blank: keep the wall showing something.
        stale = cache.read(cache_dir, params, config.cache.ttl_hours, allow_stale=True)
        if stale is None:
            raise
        return {"items": stale, "from_cache": True, "note": "quota_exceeded_stale"}

    budget.record_search(cache_dir)
    cache.write(cache_dir, params, items)
    return {"items": items, "from_cache": False, "note": None}


async def motion_score(video_id: str, cache_dir: Path, client: httpx.AsyncClient) -> float:
    """Storyboard-frame motion score for one video, cached forever on disk.

    A video's shape never changes, and these thumbnails are not the Data API,
    so this costs no quota -- only three small image fetches, once per video.
    """
    store = cache_dir / "motion"
    entry = store / f"{video_id}.json"
    if entry.exists():
        try:
            return float(json.loads(entry.read_text())["score"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            pass  # Re-measure rather than fail.

    async def frame(index: int) -> bytes | None:
        try:
            response = await client.get(
                motion.STORYBOARD_URL.format(video_id=video_id, index=index)
            )
        except httpx.HTTPError:
            return None
        return response.content if response.status_code == 200 else None

    fetched = await asyncio.gather(*(frame(i) for i in motion.STORYBOARD_INDICES))
    score = motion.score_frames([f for f in fetched if f])

    store.mkdir(parents=True, exist_ok=True)
    tmp = entry.with_name(entry.name + ".tmp")
    tmp.write_text(json.dumps({"score": score if score != motion.UNKNOWN_SCORE else None}))
    tmp.replace(entry)
    return score


async def select_videos(config: Config, video_ids: list[str], cache_dir: Path) -> dict:
    """Order the results so the wall shows things that actually move."""
    if not config.filtering.skip_static or not video_ids:
        cells = config.grid.cells
        return {"slots": video_ids[:cells], "reserves": video_ids[cells:], "relaxed": 0}

    # Measure only as deep as needed: the grid plus enough spares to substitute
    # from. Scoring all 50 would mean 150 fetches for a wall of eight.
    depth = min(len(video_ids), config.grid.cells + config.filtering.scan_depth)
    head, tail = video_ids[:depth], video_ids[depth:]

    async with httpx.AsyncClient(timeout=10.0) as client:
        scores = await asyncio.gather(*(motion_score(v, cache_dir, client) for v in head))

    result = motion.rank(
        list(zip(head, scores, strict=True)),
        needed=config.grid.cells,
        threshold=config.filtering.static_threshold,
    )
    result["reserves"] = result["reserves"] + tail
    return result


def videos_message(
    config: Config, resolved: dict, query: str, cache_dir: Path, selection: dict
) -> dict:
    items = resolved["items"]
    note = resolved["note"] or ("no_results" if not items else None)
    return {
        "type": "videos",
        "query": query,
        "video_ids": selection["slots"],
        "reserves": selection["reserves"],
        "titles": {item["video_id"]: item["title"] for item in items},
        "from_cache": resolved["from_cache"],
        "note": note,
        "static_relaxed": selection["relaxed"],
        "units_spent_today": budget.spent(cache_dir),
        "daily_limit_units": config.quota.daily_limit_units,
    }


def create_app(
    config_path: Path, cache_dir: Path, settings: Settings, log_dir: Path | None = None
) -> FastAPI:
    app = FastAPI(title="yt matrix")
    manager = ConnectionManager()
    log_dir = log_dir if log_dir is not None else config_path.parent / "logs"

    # The query actually on the wall, restored from disk so a restart or a
    # reload keeps showing what it was showing -- reloads are free, and only an
    # explicit "New" spends quota. Kept out of config.yaml: writing every
    # generated query to a committed file would churn git constantly, and the
    # file's `query` field stays meaningful as the manual fallback.
    state: dict = wallstate.load(cache_dir)

    def effective_query(config: Config) -> str:
        return state["query"] or config.query

    async def current_videos(source: str | None = None, prompt: str | None = None) -> dict:
        config = load_config(config_path)
        query = effective_query(config)
        # Every resolution is logged, including plain reloads: the log is a
        # record of what was on the wall and when, not just of what was newly
        # searched. `from_cache` distinguishes the two.
        source = source or ("generated" if state["query"] else "config")
        try:
            resolved = await resolve_videos(config, cache_dir, settings.youtube_api_key, query)
        except BudgetExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except youtube.QuotaExceededError as exc:
            raise HTTPException(
                status_code=503,
                detail="YouTube API daily quota exceeded and no cached results are available.",
            ) from exc
        except youtube.SearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        video_ids = [item["video_id"] for item in resolved["items"]]
        selection = await select_videos(config, video_ids, cache_dir)
        message = videos_message(config, resolved, query, cache_dir, selection)

        querylog.append(
            log_dir,
            querylog.build_entry(
                query=query,
                source=source,
                video_ids=message["video_ids"],
                titles=message["titles"],
                from_cache=message["from_cache"],
                units_spent_today=message["units_spent_today"],
                reserves=len(message["reserves"]),
                static_relaxed=message["static_relaxed"],
                prompt=prompt,
                note=message["note"],
            ),
        )
        return message

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

        # Typing a query by hand is an override: it must beat whatever Gemini
        # last generated, or the config page would appear to do nothing.
        if previous.query != new_config.query:
            state["query"] = None
            wallstate.save(cache_dir, state)

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
        return {
            "would_hit": hit,
            "quota_cost": 0 if hit else budget.SEARCH_COST_UNITS,
            "units_spent_today": budget.spent(cache_dir),
            "daily_limit_units": candidate.quota.daily_limit_units,
        }

    @app.get("/api/videos")
    async def get_videos() -> dict:
        return await current_videos()

    @app.get("/api/content-box/{video_id}")
    async def content_box(video_id: str) -> dict:
        """Where the actual picture sits inside this video's 16:9 frame.

        Thumbnails come from i.ytimg.com, which is not the Data API and costs
        no quota. Results are cached on disk: a video's shape never changes.
        """
        box_cache = cache_dir / "content-boxes"
        cached = box_cache / f"{video_id}.json"
        if cached.exists():
            try:
                return json.loads(cached.read_text())
            except (json.JSONDecodeError, OSError):
                pass  # Re-fetch rather than fail.

        url = letterbox.THUMBNAIL_URL.format(video_id=video_id)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
            box = (
                letterbox.detect_content_box(response.content)
                if response.status_code == 200
                else dict(letterbox.FULL_FRAME)
            )
        except httpx.HTTPError:
            # A missing thumbnail is not worth failing a cell over.
            return dict(letterbox.FULL_FRAME)

        box_cache.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(cached.name + ".tmp")
        tmp.write_text(json.dumps(box))
        tmp.replace(cached)
        return box

    @app.post("/api/new-query")
    async def new_query(payload: dict | None = None) -> dict:
        """Invent a fresh query with Gemini and put it on the wall.

        An optional `prompt` is the operator's steer -- a metaprompt, not a
        raw query. It goes to Gemini together with the app's standing guidance
        (return things that move, avoid static-upload words, must return dozens
        of results), so "sad piano" comes back as a usable search rather than
        being pasted straight into YouTube.

        Never called implicitly: only the New query button, ?new=true, or the
        prompt box. A generated query is a cache miss by definition and costs
        100 units.
        """
        raw_prompt = (payload or {}).get("prompt")
        # Whitespace-only is no prompt at all -- normalise to None so "manual"
        # never gets recorded for an empty box.
        prompt = raw_prompt.strip() or None if isinstance(raw_prompt, str) else None
        config = load_config(config_path)
        if not config.query_generation.enabled:
            raise HTTPException(status_code=409, detail="query_generation.enabled is false")
        if not settings.gemini_api_key:
            raise HTTPException(
                status_code=503, detail="GEMINI_API_KEY is not set; cannot generate a query"
            )
        # Refuse before calling Gemini, not after: a generated query is a cache
        # miss by definition, so generating one we cannot then search for wastes
        # a Gemini call and leaves the wall unchanged anyway.
        if budget.would_exceed(cache_dir, config.quota.daily_limit_units):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"daily search budget of {config.quota.daily_limit_units} units is spent "
                    f"({budget.spent(cache_dir)} used); keeping the current query."
                ),
            )

        try:
            query = await gemini.generate_query(
                config.query_generation.theme,
                state["history"][-config.query_generation.avoid_repeats :],
                config.query_generation.model,
                settings.gemini_api_key,
                instruction=prompt,
            )
        except gemini.QueryGenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        state["query"] = query
        state["history"].append(query)
        wallstate.save(cache_dir, state)
        message = await current_videos(source="manual" if prompt else "generated", prompt=prompt)
        await manager.broadcast(message)
        return message

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
