from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
from ytmatrix.config import DEFAULT_CONFIG_PATH, Config, load_config, merge_config, save_config
from ytmatrix.settings import Settings
from ytmatrix.store import Store
from ytmatrix.ws import ConnectionManager


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
    config: Config,
    store: Store,
    api_key: str,
    query: str | None = None,
    *,
    global_limit_units: int = budget.DAILY_QUOTA_UNITS,
    inflight: dict[str, asyncio.Lock] | None = None,
) -> dict:
    """Return the video list for this config, spending quota only when required.

    `inflight` is the single-flight guard, one lock per cache key, owned by
    `create_app`. Saving a search-affecting config change tells every connected
    browser to resync at once, and they all miss the same new cache key in the
    same instant -- without this, ten open walls turn one toggle of
    `search.order` into ten searches, 1000 of the day's 10,000 units. Waiters
    do not search: they take the lock, re-read the cache, and find what the
    winner wrote.

    An in-process lock is enough *because there is exactly one container for
    the whole installation* (CLAUDE.md gotcha 30). The corollary is the thing
    worth writing down: move to per-user containers and this guard silently
    stops covering anything, because each instance would have its own dict. A
    shared guard would then have to be a claim written to R2.

    Omitting `inflight` skips the guard entirely, which is what a lone caller
    -- a test, or a script -- wants.
    """
    params = search_params_for(config, query)

    items = await cache.read(store, params, config.cache.ttl_hours)
    if items is not None:
        return {"items": items, "from_cache": True, "note": None}

    if inflight is None:
        return await _search_and_cache(config, store, api_key, params, global_limit_units)

    key = cache.cache_key(params)
    # No await between the read and the insert, so this cannot race.
    lock = inflight.get(key)
    if lock is None:
        lock = inflight[key] = asyncio.Lock()
    try:
        async with lock:
            # The winner wrote the cache before releasing, so this is a hit for
            # everyone who queued behind it. A miss here means the winner
            # failed or was refused, and searching again is then correct.
            items = await cache.read(store, params, config.cache.ttl_hours)
            if items is not None:
                return {"items": items, "from_cache": True, "note": None}
            return await _search_and_cache(config, store, api_key, params, global_limit_units)
    finally:
        # Dropped as soon as nobody holds it, so the dict does not accumulate a
        # lock per query the process ever missed. A waiter that has been woken
        # but has not yet resumed makes `locked()` briefly false and can lose
        # its entry to this line -- harmless: it still holds its own reference
        # to the lock, and anyone arriving after the write hits the cache
        # before reaching this code at all.
        if not lock.locked() and inflight.get(key) is lock:
            del inflight[key]


async def _search_and_cache(
    config: Config,
    store: Store,
    api_key: str,
    params: dict[str, str],
    global_limit_units: int,
) -> dict:
    """The spending half of `resolve_videos`, past the cache miss."""
    # Checked before the call, not after: the point is to not spend the unit.
    # Two ceilings: the shared config's, which any user can lower, and the
    # project-wide one, which none of them can raise.
    if await budget.would_exceed(
        store, config.quota.daily_limit_units, global_limit_units=global_limit_units
    ):
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
    config: Config, resolved: dict, query: str, selection: dict, units_spent_today: int
) -> dict:
    """Shape one resolved set for the wire. Pure -- the spend is passed in
    rather than read, so this stays synchronous for one number."""
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
        "units_spent_today": units_spent_today,
        "daily_limit_units": config.quota.daily_limit_units,
    }


def create_app(
    store: Store,
    settings: Settings,
    *,
    default_config_path: Path = DEFAULT_CONFIG_PATH,
) -> FastAPI:
    app = FastAPI(title="yt matrix")
    manager = ConnectionManager()

    # One lock per cache key currently being searched for, shared by every
    # request this process serves -- see `resolve_videos`. It lives here rather
    # than at module scope so two apps in one test session cannot collide.
    inflight: dict[str, asyncio.Lock] = {}

    def user_of(request: Request) -> str:
        """Who is asking, for the query log and nothing else.

        Set by the Worker after it validated the Access JWT. Nothing branches
        on it: config is shared and the current query lives in the caller's
        own browser, so the server has no per-user state to look up.
        """
        return (request.headers.get("x-wall-user") or "").strip().lower()

    async def shared_config() -> Config:
        return await load_config(store, default_path=default_config_path)

    async def usable_query(config: Config, query: str | None) -> tuple[str, list[dict] | None]:
        """Which query to put on the wall, and the cached results to serve it.

        A query supplied by the browser is honoured only if the shared cache
        already has it -- stale included. The browser replays whatever is in
        its localStorage on every load and every WebSocket reconnect, and a
        cache miss there must never become a 100-unit search: spending is the
        New query button's job and nothing else's (gotcha 2). An unknown query
        silently falls back to the shared config query.

        Stale counts as known on purpose. Expiry must not quietly move
        somebody's wall back to the shared query -- the ids are the same ones
        they were already watching.

        The items come back with the decision rather than being looked up
        again downstream, and that is the whole point of the return shape.
        Re-reading would apply the TTL a second time, and an expired entry
        would miss and fall through to a search -- so a client query would be
        free only until it aged out, and a connection that flaps after that
        would cost 100 units a reconnect, per user. Handing the items over
        makes searching structurally impossible on this path rather than
        merely unintended.

        The consequence, accepted deliberately: a client query's results are
        never refreshed on expiry. The query is fixed until someone presses
        New query, and re-running the same search buys a slightly different
        fifty videos for 100 units -- a bad trade. The fallback path (no
        client query, or an unknown one) is unchanged and still refreshes.
        """
        if not query:
            return config.query, None
        params = search_params_for(config, query)
        cached = await cache.read(store, params, config.cache.ttl_hours, allow_stale=True)
        if cached is None:
            return config.query, None
        return query, cached

    async def videos_for(
        config: Config,
        query: str,
        *,
        source: str,
        email: str,
        prompt: str | None = None,
        cached: list[dict] | None = None,
    ) -> dict:
        if cached is not None:
            # A query the caller supplied that the shared cache already holds.
            # Served from what usable_query found and never re-resolved, so
            # this branch cannot reach youtube.search at all -- see the note
            # in usable_query about why the items travel with the decision.
            resolved = {"items": cached, "from_cache": True, "note": None}
        else:
            try:
                resolved = await resolve_videos(
                    config,
                    store,
                    settings.youtube_api_key,
                    query,
                    global_limit_units=settings.global_daily_units,
                    inflight=inflight,
                )
            except BudgetExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            except youtube.QuotaExceededError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "YouTube API daily quota exceeded and no cached results are available."
                    ),
                ) from exc
            except youtube.SearchError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        video_ids = [item["video_id"] for item in resolved["items"]]
        selection = await select_videos(config, video_ids, store, settings.youtube_api_key)
        message = videos_message(config, resolved, query, selection, await budget.spent(store))

        # Every resolution is logged, including plain reloads: the log is a
        # record of what was on the wall and when, not just of what was newly
        # searched. `from_cache` distinguishes the two.
        await querylog.append(
            store,
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
            email=email,
        )
        return message

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/api/config")
    async def get_config() -> dict:
        return (await shared_config()).model_dump(mode="json")

    @app.put("/api/config")
    async def put_config(payload: dict) -> dict:
        previous = await shared_config()
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

        await save_config(new_config, store)

        # Config only. The server cannot broadcast a video set any more: it does
        # not know what query any given browser is watching -- that lives in
        # each browser's own localStorage. Each client decides for itself
        # whether the change means it has to refetch, and whether a hand-typed
        # config query should override the one it had stored.
        await manager.broadcast({"type": "config", "config": new_config.model_dump(mode="json")})
        return {"status": "ok"}

    @app.post("/api/cache-status")
    async def cache_status(payload: dict) -> dict:
        try:
            # Same merge as the PUT, so the quota indicator predicts what
            # saving would actually do rather than what the payload says.
            candidate = Config.model_validate(
                merge_config((await shared_config()).model_dump(mode="json"), payload)
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
        hit = await cache.read(store, params, candidate.cache.ttl_hours) is not None
        return {
            "would_hit": hit,
            "quota_cost": 0 if hit else budget.SEARCH_COST_UNITS,
            "units_spent_today": await budget.spent(store),
            "daily_limit_units": candidate.quota.daily_limit_units,
        }

    @app.get("/api/videos")
    async def get_videos(request: Request, query: str | None = None) -> dict:
        config = await shared_config()
        chosen, cached = await usable_query(config, query)
        return await videos_for(
            config,
            chosen,
            # `cached` is non-None exactly when the caller's own query was
            # honoured, so it is also the answer to "where did this come from".
            source="client" if cached is not None else "config",
            email=user_of(request),
            cached=cached,
        )

    @app.get("/api/content-box/{video_id}")
    async def content_box(video_id: str) -> dict:
        """Where the actual picture sits inside this video's 16:9 frame.

        Thumbnails come from i.ytimg.com, which is not the Data API and costs
        no quota. Results are cached forever: a video's shape never changes.
        """
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
    async def new_query(request: Request, payload: dict | None = None) -> dict:
        """Invent a fresh query with Gemini and return it to the caller.

        An optional `prompt` is the operator's steer -- a metaprompt, not a
        raw query. It goes to Gemini together with the app's standing guidance
        (return things that move, avoid static-upload words, must return dozens
        of results), so "sad piano" comes back as a usable search rather than
        being pasted straight into YouTube.

        Never called implicitly: only the New query button, ?new=true, or the
        prompt box. A generated query is a cache miss by definition and costs
        100 units.

        The result is returned, not broadcast. Config is shared but walls are
        not -- the caller stores this query in its own localStorage and nobody
        else's wall moves.
        """
        payload = payload or {}
        raw_prompt = payload.get("prompt")
        # Whitespace-only is no prompt at all -- normalise to None so "manual"
        # never gets recorded for an empty box.
        prompt = raw_prompt.strip() or None if isinstance(raw_prompt, str) else None
        config = await shared_config()
        if not config.query_generation.enabled:
            raise HTTPException(status_code=409, detail="query_generation.enabled is false")
        if not settings.gemini_api_key:
            raise HTTPException(
                status_code=503, detail="GEMINI_API_KEY is not set; cannot generate a query"
            )
        # Refuse before calling Gemini, not after: a generated query is a cache
        # miss by definition, so generating one we cannot then search for wastes
        # a Gemini call and leaves the wall unchanged anyway.
        if await budget.would_exceed(
            store,
            config.quota.daily_limit_units,
            global_limit_units=settings.global_daily_units,
        ):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"daily search budget of {config.quota.daily_limit_units} units is spent "
                    f"({await budget.spent(store)} used); keeping the current query."
                ),
            )

        # The avoid-list lives in the caller's browser now, so it arrives on the
        # request rather than being read from disk.
        raw_history = payload.get("history")
        if not isinstance(raw_history, list):
            raw_history = []
        # Sliced before it is converted, never after. Only the tail is ever
        # used, and the length of the posted list is the caller's choice rather
        # than ours -- the browser caps its own history, but the request body is
        # not the browser. Guarded, because `history[-0:]` is `history[0:]`, the
        # whole list: a config of 0, which means "do not avoid repeats", would
        # otherwise hand Gemini maximum avoidance and a prompt carrying every
        # query this browser has ever stored.
        keep = config.query_generation.avoid_repeats
        avoid = [str(q) for q in raw_history[-keep:]] if keep else []

        try:
            query = await gemini.generate_query(
                config.query_generation.theme,
                avoid,
                config.query_generation.model,
                settings.gemini_api_key,
                instruction=prompt,
            )
        except gemini.QueryGenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return await videos_for(
            config,
            query,
            source="manual" if prompt else "generated",
            email=user_of(request),
            prompt=prompt,
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await manager.connect(websocket)
        try:
            while True:
                # Server->client channel only; this read exists to detect close.
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    return app
