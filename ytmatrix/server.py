from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ytmatrix import (
    budget,
    cache,
    gemini,
    letterbox,
    motion,
    origin,
    querylog,
    youtube,
)
from ytmatrix.config import Config, load_config, merge_config, save_config
from ytmatrix.settings import Settings
from ytmatrix.store import FileStore, Store
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
    config: Config, store: Store, api_key: str, query: str | None = None
) -> dict:
    """Return the video list for this config, spending quota only when required."""
    params = search_params_for(config, query)

    items = await cache.read(store, params, config.cache.ttl_hours)
    if items is not None:
        return {"items": items, "from_cache": True, "note": None}

    # Checked before the call, not after: the point is to not spend the unit.
    if await budget.would_exceed(store, config.quota.daily_limit_units):
        stale = await cache.read(store, params, config.cache.ttl_hours, allow_stale=True)
        if stale is not None:
            return {"items": stale, "from_cache": True, "note": "budget_exceeded_stale"}
        raise BudgetExceededError(
            f"daily search budget of {config.quota.daily_limit_units} units is spent "
            f"({await budget.spent(store)} used). Raise quota.daily_limit_units to continue."
        )

    try:
        items = await youtube.search(params, api_key)
    except youtube.QuotaExceededError:
        # Expired-but-present beats blank: keep the wall showing something.
        stale = await cache.read(store, params, config.cache.ttl_hours, allow_stale=True)
        if stale is None:
            raise
        return {"items": stale, "from_cache": True, "note": "quota_exceeded_stale"}

    await budget.record_search(store)
    await cache.write(store, params, items)
    return {"items": items, "from_cache": False, "note": None}


async def motion_score(video_id: str, store: Store, client: httpx.AsyncClient) -> float:
    """Storyboard-frame motion score for one video, cached forever.

    A video's shape never changes, and these thumbnails are not the Data API,
    so this costs no quota -- only three small image fetches, once per video.
    """
    key = f"motion/{video_id}.json"
    raw = await store.get(key)
    if raw is not None:
        try:
            cached = json.loads(raw)["score"]
            result = motion.UNKNOWN_SCORE if cached is None else float(cached)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass  # Re-measure rather than fail -- a corrupt entry is a miss.
        else:
            return result

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

    await store.put(
        key,
        json.dumps({"score": score if score != motion.UNKNOWN_SCORE else None}).encode("utf-8"),
    )
    return score


async def video_countries(video_ids: list[str], store: Store, api_key: str) -> dict[str, str]:
    """video id -> ISO country, for the ids we can resolve.

    Two batched calls at 1 unit each, versus 100 for the search itself.
    Results are cached per video forever -- a video does not change origin.
    Any failure returns what it has: diversity is a preference, not a
    requirement, and must never stop the wall from resolving.
    """
    known: dict[str, str] = {}
    missing: list[str] = []
    for video_id in video_ids:
        raw = await store.get(f"origin/{video_id}.json")
        if raw is not None:
            try:
                country = json.loads(raw).get("country")
                if country:
                    known[video_id] = country
                continue
            except (json.JSONDecodeError, TypeError):
                pass
        missing.append(video_id)

    if not missing:
        return known

    resolved: dict[str, str | None] = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for batch in origin.chunk(missing):
                videos = await client.get(
                    origin.VIDEOS_URL,
                    params={"part": "snippet", "id": ",".join(batch), "key": api_key},
                )
                if videos.status_code != 200:
                    continue
                channel_of = origin.parse_channel_ids(videos.json())

                channel_ids = list(dict.fromkeys(channel_of.values()))
                country_of: dict[str, str | None] = {}
                for channel_batch in origin.chunk(channel_ids):
                    channels = await client.get(
                        origin.CHANNELS_URL,
                        params={
                            "part": "snippet",
                            "id": ",".join(channel_batch),
                            "key": api_key,
                        },
                    )
                    if channels.status_code == 200:
                        country_of.update(origin.parse_countries(channels.json()))

                for video_id in batch:
                    resolved[video_id] = country_of.get(channel_of.get(video_id, ""), None)
    except httpx.HTTPError:
        return known

    for video_id, country in resolved.items():
        await store.put(f"origin/{video_id}.json", json.dumps({"country": country}).encode("utf-8"))
        if country:
            known[video_id] = country
    return known


async def select_videos(
    config: Config, video_ids: list[str], store: Store, api_key: str = ""
) -> dict:
    """Order the results so the wall shows things that move, from many places."""
    if not video_ids:
        return {"slots": [], "reserves": [], "relaxed": 0}

    # Diversity first, motion second: motion.rank preserves the order it is
    # given among the videos it keeps, so spreading countries here survives
    # the static filter rather than being undone by it.
    if config.filtering.prefer_country_diversity and api_key:
        countries = await video_countries(video_ids, store, api_key)
        video_ids = origin.diversify([(v, countries.get(v)) for v in video_ids])

    if not config.filtering.skip_static:
        cells = config.grid.cells
        return {"slots": video_ids[:cells], "reserves": video_ids[cells:], "relaxed": 0}

    # Measure only as deep as needed: the grid plus enough spares to substitute
    # from. Scoring all 50 would mean 150 fetches for a wall of eight.
    depth = min(len(video_ids), config.grid.cells + config.filtering.scan_depth)
    head, tail = video_ids[:depth], video_ids[depth:]

    async with httpx.AsyncClient(timeout=10.0) as client:
        scores = await asyncio.gather(*(motion_score(v, store, client) for v in head))

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
        selection = await select_videos(config, video_ids, cache_dir, settings.youtube_api_key)
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
        previous = load_config(config_path)
        try:
            # Merged, not replaced: an omitted section means "leave it alone".
            # Validating the payload alone would reset every section the
            # sender did not know about back to its model defaults.
            new_config = Config.model_validate(
                merge_config(previous.model_dump(mode="json"), payload)
            )
        except ValidationError as exc:
            # include_context=False matters: the context of a custom validator
            # error holds the original ValueError object, which is not JSON
            # serializable and makes FastAPI raise while encoding the 422.
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_url=False, include_context=False),
            ) from exc

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
            # Same merge as the PUT, so the quota indicator predicts what
            # saving would actually do rather than what the payload says.
            candidate = Config.model_validate(
                merge_config(load_config(config_path).model_dump(mode="json"), payload)
            )
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
        no quota. Results are cached forever: a video's shape never changes.
        """
        # A FileStore is just a wrapper around cache_dir -- cheap to build per
        # request. `create_app` itself still takes `cache_dir`, not `store`;
        # that wiring is Task 6's job.
        store: Store = FileStore(cache_dir)
        raw = await store.get(f"contentbox/{video_id}.json")
        if raw is not None:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
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

        await store.put(f"contentbox/{video_id}.json", json.dumps(box).encode("utf-8"))
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
