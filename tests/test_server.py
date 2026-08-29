import asyncio
import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ytmatrix import budget, cache, gemini, letterbox, motion, querylog, server, youtube
from ytmatrix.config import CONFIG_KEY, load_config
from ytmatrix.server import create_app
from ytmatrix.settings import Settings
from ytmatrix.store import FileStore

VALID = {
    "query": "golden cover",
    "grid": {"cols": 4, "rows": 2},
    "search": {
        "order": "relevance",
        "video_duration": "any",
        "safe_search": "moderate",
        "relevance_language": "en",
    },
    "playback": {"muted": True, "autoplay_on_change": True, "start_offset": 0, "loop": True},
    "cache": {"ttl_hours": 24},
}


def seed_cache(store, count: int = 50, prefix: str = "vid") -> list[str]:
    """Still synchronous: the tests around it drive a sync TestClient, and there
    is no running loop to conflict with."""
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    ids = [f"{prefix}{i:03d}" for i in range(count)]
    asyncio.run(
        cache.write(
            store,
            params,
            [{"video_id": v, "title": f"T{v}", "channel": "C"} for v in ids],
        )
    )
    return ids


def seed_query(store, query: str) -> str:
    """Put a query in the shared cache so the server will honour it."""
    params = youtube.build_params(query, "relevance", "any", "moderate", "en")
    asyncio.run(
        cache.write(
            store,
            params,
            [{"video_id": f"q{i:03d}", "title": "T", "channel": "C"} for i in range(50)],
        )
    )
    return query


def age_cache(store, query: str = "golden cover") -> None:
    """Push a cached entry back past any TTL, so only the stale path can serve it."""
    params = youtube.build_params(query, "relevance", "any", "moderate", "en")
    key = f"{cache.KEY_PREFIX}{cache.cache_key(params)}.json"
    payload = json.loads(asyncio.run(store.get(key)))
    payload["fetched_at"] = 0
    asyncio.run(store.put(key, json.dumps(payload).encode("utf-8")))


def _record(sink):
    async def broadcast(self, message):
        sink.append(message)

    return broadcast


def _returns(value):
    async def generate(*args, **kwargs):
        return value

    return generate


@pytest.fixture
def app_env(tmp_path):
    default_path = tmp_path / "config.yaml"
    default_path.write_text(yaml.safe_dump(VALID))
    store = FileStore(tmp_path / "store")
    settings = Settings(youtube_api_key="TEST_KEY")
    app = create_app(store=store, settings=settings, default_config_path=default_path)
    return app, default_path, store


def test_healthz_reports_ok(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_get_config_returns_the_shared_config(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        body = client.get("/api/config").json()
    assert body["query"] == "golden cover"
    assert body["grid"] == {"cols": 4, "rows": 2}


def test_get_config_never_leaks_the_api_key(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert "TEST_KEY" not in client.get("/api/config").text


def test_get_videos_serves_from_cache_without_network(app_env):
    app, _, store = app_env
    ids = seed_cache(store)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["from_cache"] is True
    assert body["video_ids"] == ids[:8]
    assert body["reserves"] == ids[8:]


def test_get_videos_splits_at_the_configured_cell_count(app_env):
    app, default_path, store = app_env
    seed_cache(store)
    # Nothing has been saved to the store yet, so the committed template is
    # still what every request reads: rewriting it is enough, no PUT needed
    # and no broadcast to reason about.
    default_path.write_text(yaml.safe_dump({**VALID, "grid": {"cols": 3, "rows": 1}}))
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert len(body["video_ids"]) == 3
    assert len(body["reserves"]) == 47


def test_put_config_persists_to_the_store(app_env):
    app, default_path, store = app_env
    updated = {**VALID, "grid": {"cols": 2, "rows": 2}}
    with TestClient(app) as client:
        assert client.put("/api/config", json=updated).status_code == 200
    # Read back through load_config rather than the route, so this fails if the
    # save never reached the store and only the in-flight object was right.
    saved = asyncio.run(load_config(store, default_path=default_path))
    assert saved.grid.model_dump() == {"cols": 2, "rows": 2}


def test_put_config_rejects_invalid_input_with_422(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        response = client.put("/api/config", json={**VALID, "query": ""})
    assert response.status_code == 422


def test_put_config_accepts_starting_unmuted(app_env):
    app, _, store = app_env
    seed_cache(store)
    unmuted = {**VALID, "playback": {**VALID["playback"], "muted": False}}
    with TestClient(app) as client:
        assert client.put("/api/config", json=unmuted).status_code == 200


def test_a_rejected_put_leaves_an_oversized_grid_unsaved(app_env):
    """Gotcha 8 for the model-level validator: 99x99 exceeds one search's 50
    results, and the refusal must not have written anything.

    Asserted against the store directly, NOT through GET /api/config. A stored
    config that fails validation makes `load_config` fall back to the committed
    template (config.py), so a config written before validation would land in
    the store and be completely invisible through the route -- the obvious
    place to look is the one place that hides this bug.
    """
    app, _, store = app_env
    with TestClient(app) as client:
        response = client.put("/api/config", json={**VALID, "grid": {"cols": 99, "rows": 99}})

    assert response.status_code == 422
    assert asyncio.run(store.get(CONFIG_KEY)) is None, "a rejected PUT wrote to the store"


def test_cache_status_reports_a_hit_for_already_cached_parameters(app_env):
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        assert client.post("/api/cache-status", json=VALID).json()["would_hit"] is True


def test_cache_status_reports_a_miss_for_a_new_query(app_env):
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        body = client.post("/api/cache-status", json={**VALID, "query": "silver cover"}).json()
    assert body["would_hit"] is False


def test_cache_status_ignores_changes_that_do_not_affect_the_search(app_env):
    app, _, store = app_env
    seed_cache(store)
    cosmetic = {**VALID, "playback": {**VALID["playback"], "start_offset": 30}}
    with TestClient(app) as client:
        assert client.post("/api/cache-status", json=cosmetic).json()["would_hit"] is True


def test_a_cosmetic_change_broadcasts_config_only(app_env):
    app, _, store = app_env
    seed_cache(store)
    cosmetic = {**VALID, "playback": {**VALID["playback"], "start_offset": 45}}
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        client.put("/api/config", json=cosmetic)
        first = json.loads(ws.receive_text())
    assert first["type"] == "config"
    assert first["config"]["playback"]["start_offset"] == 45


def test_a_wall_wide_intent_is_broadcast_to_every_connection(app_env):
    app, _, _ = app_env
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        response = client.post("/api/intent", json={"type": "muteToggle"})
        assert response.status_code == 200
        message = json.loads(ws.receive_text())
    assert message == {"type": "intent", "intent": {"type": "muteToggle"}}


def test_an_intent_with_extra_fields_is_relayed_verbatim(app_env):
    """hoverUnmuteToggle/followToggle carry a `checked` flag; newQuery carries
    an optional `prompt`. The endpoint does not know or care about a given
    type's extra fields -- it relays whatever the caller sent, and the
    client-side applyIntent() dispatcher is what interprets them."""
    app, _, _ = app_env
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        client.post("/api/intent", json={"type": "hoverUnmuteToggle", "checked": True})
        message = json.loads(ws.receive_text())
    assert message == {"type": "intent", "intent": {"type": "hoverUnmuteToggle", "checked": True}}


def test_an_unknown_intent_type_is_rejected_with_422(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        response = client.post("/api/intent", json={"type": "cellMenuAction", "index": 0})
        assert response.status_code == 422


def test_a_missing_intent_type_is_rejected_with_422(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.post("/api/intent", json={}).status_code == 422


def test_quota_exhaustion_falls_back_to_stale_cache(app_env, monkeypatch):
    app, _, store = app_env
    ids = seed_cache(store)
    age_cache(store)

    async def out_of_quota(*args, **kwargs):
        raise youtube.QuotaExceededError("spent")

    monkeypatch.setattr(youtube, "search", out_of_quota)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["video_ids"] == ids[:8]
    assert body["note"] == "quota_exceeded_stale"


def test_quota_exhaustion_with_no_cache_at_all_returns_503(app_env, monkeypatch):
    app, _, _ = app_env

    async def out_of_quota(*args, **kwargs):
        raise youtube.QuotaExceededError("spent")

    monkeypatch.setattr(youtube, "search", out_of_quota)
    with TestClient(app) as client:
        response = client.get("/api/videos")
    assert response.status_code == 503
    assert "quota" in response.json()["detail"].lower()


def test_a_successful_search_is_written_to_cache(app_env, monkeypatch):
    app, _, _ = app_env
    calls = []

    async def fake_search(params, api_key, *, client=None):
        calls.append(params)
        return [{"video_id": f"x{i}", "title": "T", "channel": "C"} for i in range(50)]

    monkeypatch.setattr(youtube, "search", fake_search)
    with TestClient(app) as client:
        first = client.get("/api/videos").json()
        second = client.get("/api/videos").json()

    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert len(calls) == 1  # the second request must not spend quota


def counting_search(calls, delay: float = 0.05):
    """A stubbed search that records its calls and takes long enough to overlap.

    The `sleep` is the whole point: a coroutine that never really awaits runs
    to completion before anything else is scheduled, so without it the second
    caller could not arrive mid-search and there would be no race to guard
    against.
    """

    async def fake_search(params, api_key, *, client=None):
        calls.append(params)
        await asyncio.sleep(delay)
        return [{"video_id": f"c{i}", "title": "T", "channel": "C"} for i in range(50)]

    return fake_search


async def test_concurrent_resolutions_of_one_query_run_a_single_search(app_env, monkeypatch):
    """Ten open walls must not turn one config save into ten searches.

    A search-affecting config change makes every connected browser resync at
    once (`needsRefetch` in grid-logic.js), and they all miss the same
    brand-new cache key in the same instant. The single-flight is what keeps
    that at 100 units instead of 100 per wall.

    The second half is the evidence that the guard is doing the work: the
    identical scenario without the `inflight` dict searches once per caller.
    """
    _, default_path, store = app_env
    config = await load_config(store, default_path=default_path)

    guarded = []
    monkeypatch.setattr(youtube, "search", counting_search(guarded))
    inflight = {}
    results = await asyncio.gather(
        *(
            server.resolve_videos(config, store, "TEST_KEY", "one query", inflight=inflight)
            for _ in range(2)
        )
    )

    assert len(guarded) == 1, "each concurrent caller ran its own 100-unit search"
    assert await budget.spent(store) == 100
    assert results[0]["items"] == results[1]["items"], "the waiter got the winner's results"
    assert results[1]["from_cache"] is True, "the waiter was served from the cache, not the wire"
    assert inflight == {}, "the lock outlived the search it was guarding"

    unguarded = []
    monkeypatch.setattr(youtube, "search", counting_search(unguarded))
    await asyncio.gather(
        *(server.resolve_videos(config, store, "TEST_KEY", "another query") for _ in range(2))
    )
    assert len(unguarded) == 2, "without the guard this test could not fail"


async def test_two_browsers_resyncing_at_once_share_one_search(app_env, monkeypatch):
    """The same guard, reached through the route -- create_app must wire it."""
    app, _, store = app_env
    calls = []
    monkeypatch.setattr(youtube, "search", counting_search(calls))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(client.get("/api/videos"), client.get("/api/videos"))

    assert len(calls) == 1
    assert await budget.spent(store) == 100
    assert first.json()["video_ids"] == second.json()["video_ids"]


def test_zero_results_is_not_an_error(app_env, monkeypatch):
    app, _, _ = app_env

    async def empty_search(params, api_key, *, client=None):
        return []

    monkeypatch.setattr(youtube, "search", empty_search)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["video_ids"] == []
    assert body["note"] == "no_results"


GENERATING = {
    **VALID,
    "query_generation": {
        "enabled": True,
        "theme": "cover songs",
        "model": "gemini-3.6-flash",
        "avoid_repeats": 20,
    },
}


@pytest.fixture
def generating_env(tmp_path):
    default_path = tmp_path / "config.yaml"
    default_path.write_text(yaml.safe_dump(GENERATING))
    store = FileStore(tmp_path / "store")
    settings = Settings(youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY")
    app = create_app(store=store, settings=settings, default_config_path=default_path)
    return app, default_path, store


def stub_search(monkeypatch, count=50, prefix="g"):
    async def fake_search(params, api_key, *, client=None):
        return [{"video_id": f"{prefix}{i}", "title": "T", "channel": "C"} for i in range(count)]

    monkeypatch.setattr(youtube, "search", fake_search)


def test_new_query_puts_the_generated_query_on_the_wall(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "shoegaze motown covers"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        body = client.post("/api/new-query").json()
    assert body["query"] == "shoegaze motown covers"
    assert len(body["video_ids"]) == 8


def test_a_generated_query_is_a_cache_miss_that_costs_a_search(generating_env, monkeypatch):
    """The whole reason generation is never implicit: it always spends 100
    units, because an invented query cannot already be in the shared cache."""
    app, _, store = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "bossa nova covers"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        body = client.post("/api/new-query").json()

    assert body["from_cache"] is False
    assert asyncio.run(budget.spent(store)) == 100


def test_new_query_is_refused_when_generation_is_disabled(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 409


def test_new_query_is_refused_without_a_gemini_key(tmp_path):
    default_path = tmp_path / "config.yaml"
    default_path.write_text(yaml.safe_dump(GENERATING))
    settings = Settings(youtube_api_key="K", gemini_api_key=None)
    app = create_app(
        store=FileStore(tmp_path / "store"), settings=settings, default_config_path=default_path
    )
    with TestClient(app) as client:
        response = client.post("/api/new-query")
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_a_gemini_failure_leaves_the_wall_working(generating_env, monkeypatch):
    app, _, store = generating_env
    seed_cache(store)

    async def broken(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        raise gemini.QueryGenerationError("model unavailable")

    monkeypatch.setattr(gemini, "generate_query", broken)
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 502
        # The wall must still work off the configured query.
        assert client.get("/api/videos").json()["query"] == "golden cover"


def test_the_daily_budget_blocks_a_new_search(app_env, monkeypatch):
    app, default_path, store = app_env
    default_path.write_text(yaml.safe_dump({**VALID, "quota": {"daily_limit_units": 100}}))
    asyncio.run(budget.record_search(store))  # 100 spent, limit 100 -> next would be 200
    stub_search(monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/videos")
    assert response.status_code == 429
    assert "budget" in response.json()["detail"].lower()


def test_the_budget_serves_stale_cache_rather_than_failing_when_it_can(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    age_cache(store)
    asyncio.run(budget.record_search(store))
    default_path.write_text(yaml.safe_dump({**VALID, "quota": {"daily_limit_units": 100}}))

    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["note"] == "budget_exceeded_stale"
    assert body["video_ids"] == ids[:8]


def test_the_budget_blocks_generation_before_calling_gemini(generating_env, monkeypatch):
    app, default_path, store = generating_env
    default_path.write_text(yaml.safe_dump({**GENERATING, "quota": {"daily_limit_units": 100}}))
    asyncio.run(budget.record_search(store))
    called = []

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        called.append(theme)
        return "should not happen"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 429
    assert called == [], "Gemini must not be called when the search cannot be afforded"


def test_the_config_limit_cannot_raise_the_global_ceiling(tmp_path, monkeypatch):
    """`quota.daily_limit_units` lives in the shared, editable config, so it may
    only ever LOWER the ceiling -- every wall spends from one API key and one
    10,000-unit bucket, and a config anybody can edit must not be able to hand
    itself more of it.

    Both defaults are 10,000, so the wiring is invisible unless the two are
    forced apart: without that, deleting `global_limit_units=` from the
    resolve_videos call is a silent no-op.
    """
    default_path = tmp_path / "config.yaml"
    default_path.write_text(yaml.safe_dump({**VALID, "quota": {"daily_limit_units": 1_000_000}}))
    store = FileStore(tmp_path / "store")
    app = create_app(
        store=store,
        settings=Settings(youtube_api_key="TEST_KEY", global_daily_units=100),
        default_config_path=default_path,
    )
    stub_search(monkeypatch)
    asyncio.run(budget.record_search(store))  # 100 spent; another would reach 200

    with TestClient(app) as client:
        response = client.get("/api/videos")

    assert response.status_code == 429, "the config's million-unit limit was honoured"


def test_the_global_ceiling_also_blocks_generation(tmp_path, monkeypatch):
    """The same ceiling, on the other route that checks it. new-query has its
    own would_exceed call, so it needs its own assertion."""
    default_path = tmp_path / "config.yaml"
    default_path.write_text(
        yaml.safe_dump({**GENERATING, "quota": {"daily_limit_units": 1_000_000}})
    )
    store = FileStore(tmp_path / "store")
    app = create_app(
        store=store,
        settings=Settings(
            youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY", global_daily_units=100
        ),
        default_config_path=default_path,
    )
    stub_search(monkeypatch)
    asyncio.run(budget.record_search(store))
    called = []

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        called.append(theme)
        return "should not happen"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 429
    assert called == [], "Gemini must not be called past the project-wide ceiling"


def test_a_successful_search_records_its_cost(app_env, monkeypatch):
    app, _, store = app_env
    stub_search(monkeypatch)
    with TestClient(app) as client:
        client.get("/api/videos")
    assert asyncio.run(budget.spent(store)) == 100


def test_a_cached_result_costs_nothing(app_env):
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        client.get("/api/videos")
    assert asyncio.run(budget.spent(store)) == 0


def test_replaying_a_generated_query_across_a_restart_is_free(generating_env, monkeypatch):
    """The wall state moved into the browser, but the quota guarantee it bought
    has to survive the move: a browser replaying its stored query at a fresh
    container must be served from the shared cache, not re-searched."""
    app, default_path, store = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "remembered by the browser"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")
    spent_after_generation = asyncio.run(budget.spent(store))

    # A fresh app over the same store is what a restart -- or another user's
    # container -- looks like. It remembers nothing; the query comes from them.
    restarted = create_app(
        store=store,
        settings=Settings(youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY"),
        default_config_path=default_path,
    )
    with TestClient(restarted) as client:
        body = client.get("/api/videos", params={"query": "remembered by the browser"}).json()
        client.get("/api/videos", params={"query": "remembered by the browser"})

    assert body["query"] == "remembered by the browser"
    assert asyncio.run(budget.spent(store)) == spent_after_generation, "reloads must be free"


def pillarboxed_jpeg() -> bytes:
    import io

    from PIL import Image

    image = Image.new("L", (320, 180), 0)
    image.paste(Image.new("L", (102, 180), 200), (109, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_content_box_detects_bars_from_the_thumbnail(app_env, monkeypatch):
    app, _, _ = app_env
    thumbnail = pillarboxed_jpeg()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert "i.ytimg.com" in url, "thumbnails must not come from the Data API"
            assert "mqdefault" in url, "hqdefault is 4:3 with baked padding"
            return httpx.Response(200, content=thumbnail)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with TestClient(app) as client:
        box = client.get("/api/content-box/abc123").json()
    assert box["w"] < 0.4, box
    assert box["h"] > 0.95, box


def test_content_box_is_cached_so_a_video_is_only_analysed_once(app_env, monkeypatch):
    app, _, _ = app_env
    thumbnail = pillarboxed_jpeg()
    fetches = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            fetches.append(url)
            return httpx.Response(200, content=thumbnail)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with TestClient(app) as client:
        first = client.get("/api/content-box/abc123").json()
        second = client.get("/api/content-box/abc123").json()
    assert first == second
    assert len(fetches) == 1


def test_a_missing_thumbnail_falls_back_to_the_full_frame(app_env, monkeypatch):
    app, _, _ = app_env

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return httpx.Response(404)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with TestClient(app) as client:
        assert client.get("/api/content-box/gone").json() == letterbox.FULL_FRAME


def test_a_thumbnail_fetch_failure_does_not_break_the_cell(app_env, monkeypatch):
    app, _, _ = app_env

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with TestClient(app) as client:
        response = client.get("/api/content-box/abc123")
    assert response.status_code == 200
    assert response.json() == letterbox.FULL_FRAME


def stub_motion(monkeypatch, scores: dict, default=motion.UNKNOWN_SCORE):
    async def fake_score(video_id, store, client):
        return scores.get(video_id, default)

    monkeypatch.setattr(server, "motion_score", fake_score)


def test_still_image_videos_are_kept_off_the_wall(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(yaml.safe_dump({**VALID, "filtering": {"skip_static": True}}))
    # The first four results are static album art.
    stub_motion(monkeypatch, {ids[i]: 0.5 for i in range(4)}, default=30.0)

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert not set(body["video_ids"]) & set(ids[:4]), "a still made it onto the wall"
    assert len(body["video_ids"]) == 8
    assert body["static_relaxed"] == 0


def test_filtering_preserves_relevance_order_among_the_survivors(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(yaml.safe_dump({**VALID, "filtering": {"skip_static": True}}))
    stub_motion(monkeypatch, {ids[0]: 0.5}, default=30.0)

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert body["video_ids"] == ids[1:9], "search order should survive the filter"


def test_it_relaxes_rather_than_leaving_cells_empty(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(yaml.safe_dump({**VALID, "filtering": {"skip_static": True}}))
    # Almost everything is a still: the wall must still fill.
    stub_motion(monkeypatch, {v: 0.5 for v in ids[:40]}, default=30.0)

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert len(body["video_ids"]) == 8, "cells were left empty instead of relaxing"


def test_relaxing_is_reported_so_it_is_not_silent(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(yaml.safe_dump({**VALID, "filtering": {"skip_static": True}}))
    stub_motion(monkeypatch, {v: 0.5 for v in ids}, default=0.5)

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert body["static_relaxed"] == 8


def test_filtering_can_be_turned_off(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(yaml.safe_dump({**VALID, "filtering": {"skip_static": False}}))
    stub_motion(monkeypatch, {v: 0.0 for v in ids})

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert body["video_ids"] == ids[:8], "filtering was applied despite being disabled"


def test_a_moving_query_measures_only_the_grid(app_env, monkeypatch):
    """Scoring widens in waves and stops as soon as the wall can be filled.

    Three image fetches per video, so measuring the grid plus the whole scan
    depth was ~96 fetches for a wall of eight -- 3.3s on Cloudflare against
    0.22s on a laptop. Most queries are mostly-moving, so the first wave is
    usually enough and the rest is never fetched.
    """
    app, default_path, store = app_env
    seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"skip_static": True, "scan_depth": 16}})
    )
    measured = []

    async def all_moving(video_id, index, client):
        measured.append(video_id)
        return 30.0

    monkeypatch.setattr(server, "motion_score", all_moving)
    with TestClient(app) as client:
        client.get("/api/videos")

    # The grid, and not one more: eight moving videos fill eight cells.
    assert len(measured) == 8


def test_a_static_query_widens_but_never_past_the_ceiling(app_env, monkeypatch):
    """The old depth is now a ceiling rather than a target.

    A query with nothing moving in it costs exactly what it always did -- and
    still refuses to measure all fifty, which would be 150 fetches for a wall
    of eight.
    """
    app, default_path, store = app_env
    seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"skip_static": True, "scan_depth": 4}})
    )
    measured = []

    async def all_static(video_id, index, client):
        measured.append(video_id)
        return 0.5  # below any sane static_threshold

    monkeypatch.setattr(server, "motion_score", all_static)
    with TestClient(app) as client:
        response = client.get("/api/videos")

    # grid (8) + scan_depth (4), and it stops there rather than scanning all 50.
    assert len(measured) == 12
    # Nothing moving, so the wall relaxes and uses the liveliest stills rather
    # than leaving cells empty (gotcha 16).
    assert response.json()["static_relaxed"] > 0


def test_settings_require_an_api_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="youtube_api_key|YOUTUBE_API_KEY"):
        Settings(_env_file=None)


# --- manual metaprompt + query logging -------------------------------------


# The query log lives in the same Store as everything else now, so
# `generating_env` covers these too -- there is no separate log directory to
# point a fixture at.


def test_a_manual_prompt_is_passed_to_gemini_as_guidance(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)
    seen = {}

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        seen["instruction"] = instruction
        seen["theme"] = theme
        return "sad piano covers live"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        body = client.post("/api/new-query", json={"prompt": "something sadder"}).json()

    assert seen["instruction"] == "something sadder"
    # The standing theme still goes along: the prompt steers, it does not replace.
    assert seen["theme"] == GENERATING["query_generation"]["theme"]
    assert body["query"] == "sad piano covers live"


def test_a_blank_prompt_is_treated_as_no_prompt(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)
    seen = {}

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        seen["instruction"] = instruction
        return "q"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query", json={"prompt": "   "})
    assert seen["instruction"] is None


def test_new_query_still_works_with_no_body_at_all(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "q"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 200


def test_every_resolution_is_logged(generating_env, monkeypatch):
    app, _, store = generating_env
    seed_cache(store)

    with TestClient(app) as client:
        client.get("/api/videos")
        client.get("/api/videos")

    entries = asyncio.run(querylog.read_all(store))
    assert len(entries) == 2, "plain reloads must be recorded too"
    assert all(e["from_cache"] is True for e in entries)


def test_the_log_records_the_query_and_its_results(generating_env, monkeypatch):
    app, _, store = generating_env
    ids = seed_cache(store)

    with TestClient(app) as client:
        client.get("/api/videos")

    entry = asyncio.run(querylog.read_all(store))[0]
    assert entry["query"] == "golden cover"
    assert [r["video_id"] for r in entry["results"]] == ids[:8]
    assert entry["count"] == 8
    assert entry["units_spent_today"] == 0


def test_a_manual_query_is_logged_with_its_prompt_and_source(generating_env, monkeypatch):
    app, _, store = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "generated from prompt"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query", json={"prompt": "more guitars"})

    entry = asyncio.run(querylog.read_all(store))[-1]
    assert entry["source"] == "manual"
    assert entry["prompt"] == "more guitars"
    assert entry["query"] == "generated from prompt"


def test_an_unprompted_generation_is_logged_as_generated(generating_env, monkeypatch):
    app, _, store = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "invented"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")

    entry = asyncio.run(querylog.read_all(store))[-1]
    assert entry["source"] == "generated"
    assert "prompt" not in entry


def test_the_log_accumulates_across_restarts(generating_env, monkeypatch):
    app, default_path, store = generating_env
    seed_cache(store)
    with TestClient(app) as client:
        client.get("/api/videos")

    restarted = create_app(
        store=store,
        settings=Settings(youtube_api_key="K", gemini_api_key="G"),
        default_config_path=default_path,
    )
    with TestClient(restarted) as client:
        client.get("/api/videos")

    entries = asyncio.run(querylog.read_all(store))
    assert len(entries) == 2, "the log must not be truncated on restart"


# --- country diversity -----------------------------------------------------


def stub_countries(monkeypatch, mapping):
    async def fake(video_ids, store, api_key):
        return {v: c for v, c in mapping.items() if c}

    monkeypatch.setattr(server, "video_countries", fake)


def test_the_wall_spreads_across_countries(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"prefer_country_diversity": True}})
    )
    # The first ten results all come from one country, as really happens.
    mapping = {v: "US" for v in ids[:10]}
    mapping.update({ids[10]: "KR", ids[11]: "GB", ids[12]: "ES", ids[13]: "AU"})
    stub_countries(monkeypatch, mapping)

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    on_wall = [mapping.get(v) for v in body["video_ids"]]
    assert len({c for c in on_wall if c}) >= 5, on_wall


def test_diversity_keeps_the_most_relevant_result_first(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"prefer_country_diversity": True}})
    )
    stub_countries(monkeypatch, {v: "US" for v in ids[:5]} | {ids[5]: "KR"})

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert body["video_ids"][0] == ids[0], "reordering must not demote the top hit"


def test_diversity_drops_nothing(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"prefer_country_diversity": True}})
    )
    stub_countries(monkeypatch, {v: "US" for v in ids})

    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert sorted(body["video_ids"] + body["reserves"]) == sorted(ids)


def test_diversity_can_be_turned_off(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"prefer_country_diversity": False}})
    )
    called = []

    async def should_not_run(video_ids, store, api_key):
        called.append(1)
        return {}

    monkeypatch.setattr(server, "video_countries", should_not_run)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert called == [], "country lookup must not spend units when disabled"
    assert body["video_ids"] == ids[:8]


def test_a_country_lookup_failure_does_not_break_the_wall(app_env, monkeypatch):
    app, default_path, store = app_env
    ids = seed_cache(store)
    default_path.write_text(
        yaml.safe_dump({**VALID, "filtering": {"prefer_country_diversity": True}})
    )

    async def unavailable(video_ids, store, api_key):
        return {}  # everything unknown

    monkeypatch.setattr(server, "video_countries", unavailable)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()

    assert len(body["video_ids"]) == 8
    assert body["video_ids"] == ids[:8], "unknown origin should leave order alone"


def test_saving_config_does_not_switch_query_generation_off(generating_env, monkeypatch):
    """The config page has no field for query_generation, and used to send a
    payload without it -- which reset it to its default of disabled."""
    app, _, store = generating_env
    seed_cache(store)
    page_payload = {
        "query": "golden cover",
        "grid": {"cols": 4, "rows": 2},
        "search": VALID["search"],
        "playback": VALID["playback"],
        "cache": {"ttl_hours": 24},
    }

    with TestClient(app) as client:
        assert client.put("/api/config", json=page_payload).status_code == 200
        after = client.get("/api/config").json()

    assert after["query_generation"]["enabled"] is True
    assert after["query_generation"]["theme"] == GENERATING["query_generation"]["theme"]


def test_a_partial_save_preserves_filtering_and_quota(app_env, monkeypatch):
    app, default_path, store = app_env
    seed_cache(store)
    stub_search(monkeypatch)  # the changed query is a cache miss
    default_path.write_text(
        yaml.safe_dump(
            {
                **VALID,
                "filtering": {"skip_static": False, "static_threshold": 9.5},
                "quota": {"daily_limit_units": 1234},
            }
        )
    )
    with TestClient(app) as client:
        client.put("/api/config", json={"query": "something else"})
        after = client.get("/api/config").json()

    assert after["filtering"]["static_threshold"] == 9.5
    assert after["filtering"]["skip_static"] is False
    assert after["quota"]["daily_limit_units"] == 1234
    assert after["query"] == "something else"


def test_new_query_still_works_after_a_config_save(generating_env, monkeypatch):
    """End to end for the reported failure: Save, then New query."""
    app, _, store = generating_env
    seed_cache(store)
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "still working"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.put("/api/config", json={"grid": {"cols": 4, "rows": 4}})
        response = client.post("/api/new-query")

    assert response.status_code == 200, response.json()
    assert response.json()["query"] == "still working"


# --- stateless server: shared config, per-browser query ---------------------


def test_config_is_shared_between_callers(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        client.put("/api/config", json={"grid": {"cols": 3, "rows": 3}})
        assert client.get("/api/config").json()["grid"]["cols"] == 3


def test_a_rejected_put_leaves_the_stored_config_untouched(app_env):
    """Gotcha 8, now against a Store key rather than a file."""
    app, _, _ = app_env
    with TestClient(app) as client:
        client.put("/api/config", json={"query": "good"})
        assert client.put("/api/config", json={"query": "   "}).status_code == 422
        assert client.get("/api/config").json()["query"] == "good"


def test_a_cached_client_query_is_honoured(app_env):
    app, _, store = app_env
    seed_cache(store, prefix="vid")
    cached = seed_query(store, "already searched")
    with TestClient(app) as client:
        assert client.get("/api/videos", params={"query": cached}).json()["query"] == cached


def test_an_unknown_client_query_never_spends_a_search(app_env):
    """The browser replays localStorage on every load and every reconnect. A
    cache miss there must fall back, not search -- gotcha 2."""
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        body = client.get("/api/videos", params={"query": "never searched before"}).json()
    assert body["query"] == "golden cover"
    assert body["units_spent_today"] == 0


def test_a_stale_client_query_is_still_honoured(app_env, monkeypatch):
    """Expiry must not silently move somebody's wall back to the shared query;
    the ids are the same ones they were already watching."""
    app, _, store = app_env
    seed_cache(store)
    cached = seed_query(store, "watched yesterday")
    monkeypatch.setattr(server.cache.time, "time", lambda: 1e12)
    with TestClient(app) as client:
        assert client.get("/api/videos", params={"query": cached}).json()["query"] == cached


def test_a_stale_client_query_is_served_without_spending(app_env, monkeypatch):
    """The other half of gotcha 2, and the one that is easy to miss: honouring
    a client query is only free while its cache entry is fresh unless the
    resolution is *also* pinned to cache. `resync()` runs on every WebSocket
    reconnect, so without this an aged-out query turns a flapping connection
    into 100 units a head.

    Deliberately does not stub `youtube.search`: conftest's guard is the
    assertion. If this path ever searches again, the test says so.
    """
    app, _, store = app_env
    seed_cache(store)
    cached = seed_query(store, "watched yesterday")
    monkeypatch.setattr(server.cache.time, "time", lambda: 1e12)
    with TestClient(app) as client:
        body = client.get("/api/videos", params={"query": cached}).json()

    assert body["query"] == cached
    assert body["video_ids"], "the stale entry must still fill the wall"
    assert body["from_cache"] is True
    assert body["units_spent_today"] == 0
    assert asyncio.run(budget.spent(store)) == 0


def test_no_query_serves_the_shared_config_query(app_env):
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        assert client.get("/api/videos").json()["query"] == "golden cover"


def test_new_query_does_not_broadcast_videos(generating_env, monkeypatch):
    """Everyone shares config but not walls: one person pressing New query must
    not move anybody else's."""
    app, _, store = generating_env
    seed_cache(store)
    stub_search(monkeypatch)
    sent = []
    monkeypatch.setattr(server.ConnectionManager, "broadcast", _record(sent))
    monkeypatch.setattr(server.gemini, "generate_query", _returns("invented"))
    with TestClient(app) as client:
        client.post("/api/new-query", json={})
    assert [m["type"] for m in sent] == []


def test_new_query_returns_the_query_for_the_client_to_store(generating_env, monkeypatch):
    app, _, store = generating_env
    seed_cache(store)
    stub_search(monkeypatch)
    monkeypatch.setattr(server.gemini, "generate_query", _returns("invented"))
    with TestClient(app) as client:
        assert client.post("/api/new-query", json={}).json()["query"] == "invented"


def test_new_query_passes_the_clients_history_to_gemini(generating_env, monkeypatch):
    """History steers Gemini away from repeats and now lives in the browser, so
    it has to arrive on the request."""
    seen = {}

    async def capture(theme, history, model, api_key, instruction=None):
        seen["history"] = list(history)
        return "invented"

    app, _, store = generating_env
    seed_cache(store)
    stub_search(monkeypatch)
    monkeypatch.setattr(server.gemini, "generate_query", capture)
    with TestClient(app) as client:
        client.post("/api/new-query", json={"history": ["a", "b"]})
    assert seen["history"] == ["a", "b"]


def test_only_the_most_recent_history_reaches_gemini(generating_env, monkeypatch):
    """`avoid_repeats` bounds what the browser's history contributes to the
    prompt. The history now arrives on the request, so nothing but this slice
    stands between a browser's whole stored list and the Gemini call."""
    app, _, _ = generating_env  # GENERATING sets avoid_repeats: 20
    stub_search(monkeypatch)
    seen = {}

    async def capture(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        seen["avoid"] = list(avoid)
        return "invented"

    monkeypatch.setattr(gemini, "generate_query", capture)
    history = [f"q{i:02d}" for i in range(25)]
    with TestClient(app) as client:
        client.post("/api/new-query", json={"history": history})

    assert len(seen["avoid"]) == 20
    assert seen["avoid"] == history[-20:], "the OLDEST entries must be the ones dropped"


def test_avoid_repeats_of_zero_means_no_avoid_list(generating_env, monkeypatch):
    """`history[-0:]` is `history[0:]` -- the WHOLE list. Setting avoid_repeats
    to 0 must mean "do not avoid repeats", not "avoid every query this browser
    has ever stored", which is what the naive slice does."""
    app, default_path, _ = generating_env
    default_path.write_text(
        yaml.safe_dump(
            {
                **GENERATING,
                "query_generation": {**GENERATING["query_generation"], "avoid_repeats": 0},
            }
        )
    )
    stub_search(monkeypatch)
    seen = {}

    async def capture(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        seen["avoid"] = list(avoid)
        return "invented"

    monkeypatch.setattr(gemini, "generate_query", capture)
    with TestClient(app) as client:
        client.post("/api/new-query", json={"history": ["a", "b", "c"]})
    assert seen["avoid"] == []


def test_new_query_tolerates_a_missing_history(generating_env, monkeypatch):
    app, _, store = generating_env
    seed_cache(store)
    stub_search(monkeypatch)
    monkeypatch.setattr(server.gemini, "generate_query", _returns("invented"))
    with TestClient(app) as client:
        assert client.post("/api/new-query", json={}).status_code == 200


def test_a_config_change_broadcasts_config_only(app_env, monkeypatch):
    """The client decides whether its own query needs refetching -- the server
    cannot, because it does not know what any browser is watching."""
    app, _, store = app_env
    seed_cache(store)
    sent = []
    monkeypatch.setattr(server.ConnectionManager, "broadcast", _record(sent))
    with TestClient(app) as client:
        client.put("/api/config", json={"search": {"order": "date"}})
    assert [m["type"] for m in sent] == ["config"]


def test_the_query_log_records_who_asked(app_env):
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        client.get("/api/videos", headers={"X-Wall-User": "A@B.com"})
    assert asyncio.run(querylog.read_all(store))[0]["user"] == "a@b.com"


def test_the_log_distinguishes_a_client_query_from_the_shared_one(app_env):
    """`source` is what tells an operator reading the log whether somebody was
    watching a query they chose or the one everybody gets. All three cases in
    one test, because the fallback is the one that could silently mislabel:
    an unknown query is served as "config", not as the client's."""
    app, _, store = app_env
    seed_cache(store)
    cached = seed_query(store, "already searched")
    with TestClient(app) as client:
        client.get("/api/videos")
        client.get("/api/videos", params={"query": cached})
        client.get("/api/videos", params={"query": "never searched before"})

    sources = [entry["source"] for entry in asyncio.run(querylog.read_all(store))]
    assert sources == ["config", "client", "config"]


def test_the_html_routes_are_gone(app_env):
    """The frontend is served by the Worker's asset binding now, not FastAPI."""
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/config").status_code == 404


def test_a_generation_reports_how_long_gemini_took(generating_env, monkeypatch):
    """The Gemini call happens in the route, before there is a query to resolve,
    so `videos_for` cannot time it -- but it is the phase most likely to be the
    one somebody is waiting on, and it has to reach the browser to be useful."""
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, instruction=None, client=None):
        return "shoegaze motown covers"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        body = client.post("/api/new-query").json()

    timings = body["timings"]
    assert "gemini" in timings, "a generation must report its Gemini time"
    assert timings["gemini"] >= 0
    # And the phases this function does measure are still there.
    assert "total" in timings


def test_a_plain_resolution_reports_no_gemini_phase(app_env):
    """Nothing generated, so there is nothing to report -- an absent key rather
    than a zero, which would read as "Gemini ran and was instant"."""
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        timings = client.get("/api/videos").json()["timings"]
    assert "gemini" not in timings
    assert "total" in timings
