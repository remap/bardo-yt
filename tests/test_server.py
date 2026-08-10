import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ytmatrix import budget, cache, gemini, letterbox, youtube
from ytmatrix.server import create_app
from ytmatrix.settings import Settings

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


def seed_cache(cache_dir: Path, count: int = 50, prefix: str = "vid") -> list[str]:
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    ids = [f"{prefix}{i:03d}" for i in range(count)]
    cache.write(
        cache_dir,
        params,
        [{"video_id": v, "title": f"T{v}", "channel": "C"} for v in ids],
    )
    return ids


@pytest.fixture
def app_env(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(VALID))
    cache_dir = tmp_path / "cache"
    settings = Settings(youtube_api_key="TEST_KEY")
    app = create_app(config_path=config_path, cache_dir=cache_dir, settings=settings)
    return app, config_path, cache_dir


def test_healthz_reports_ok(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_get_config_returns_the_file_contents(app_env):
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
    app, _, cache_dir = app_env
    ids = seed_cache(cache_dir)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["from_cache"] is True
    assert body["video_ids"] == ids[:8]
    assert body["reserves"] == ids[8:]


def test_get_videos_splits_at_the_configured_cell_count(app_env):
    app, config_path, cache_dir = app_env
    seed_cache(cache_dir)
    # The server re-reads config.yaml per request, so writing the file is
    # enough -- no PUT needed, and no broadcast to reason about.
    config_path.write_text(yaml.safe_dump({**VALID, "grid": {"cols": 3, "rows": 1}}))
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert len(body["video_ids"]) == 3
    assert len(body["reserves"]) == 47


def test_put_config_persists_to_disk(app_env):
    app, config_path, cache_dir = app_env
    # A grid change re-broadcasts the video set. The search params are
    # unchanged, so seeding the cache keeps that off the network.
    seed_cache(cache_dir)
    updated = {**VALID, "grid": {"cols": 2, "rows": 2}}
    with TestClient(app) as client:
        assert client.put("/api/config", json=updated).status_code == 200
    assert yaml.safe_load(config_path.read_text())["grid"] == {"cols": 2, "rows": 2}


def test_put_config_rejects_invalid_input_with_422(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        response = client.put("/api/config", json={**VALID, "query": ""})
    assert response.status_code == 422


def test_put_config_accepts_starting_unmuted(app_env):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)
    unmuted = {**VALID, "playback": {**VALID["playback"], "muted": False}}
    with TestClient(app) as client:
        assert client.put("/api/config", json=unmuted).status_code == 200


def test_a_rejected_put_leaves_the_file_untouched(app_env):
    app, config_path, _ = app_env
    before = config_path.read_text()
    with TestClient(app) as client:
        client.put("/api/config", json={**VALID, "grid": {"cols": 99, "rows": 99}})
    assert config_path.read_text() == before


def test_cache_status_reports_a_hit_for_already_cached_parameters(app_env):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)
    with TestClient(app) as client:
        assert client.post("/api/cache-status", json=VALID).json()["would_hit"] is True


def test_cache_status_reports_a_miss_for_a_new_query(app_env):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)
    with TestClient(app) as client:
        body = client.post("/api/cache-status", json={**VALID, "query": "silver cover"}).json()
    assert body["would_hit"] is False


def test_cache_status_ignores_changes_that_do_not_affect_the_search(app_env):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)
    cosmetic = {**VALID, "playback": {**VALID["playback"], "start_offset": 30}}
    with TestClient(app) as client:
        assert client.post("/api/cache-status", json=cosmetic).json()["would_hit"] is True


def test_a_search_affecting_change_broadcasts_both_config_and_videos(app_env, monkeypatch):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)

    # order=date is a different parameter set, so it misses cache. Stub the
    # network rather than seeding it -- the point of this test is the
    # broadcast, and no test may reach the real API.
    async def fake_search(params, api_key, *, client=None):
        return [{"video_id": f"d{i}", "title": "T", "channel": "C"} for i in range(50)]

    monkeypatch.setattr(youtube, "search", fake_search)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        client.put("/api/config", json={**VALID, "search": {**VALID["search"], "order": "date"}})
        types = [json.loads(ws.receive_text())["type"] for _ in range(2)]

    assert types == ["config", "videos"]


def test_a_cosmetic_change_broadcasts_config_only(app_env):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)
    cosmetic = {**VALID, "playback": {**VALID["playback"], "start_offset": 45}}
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        client.put("/api/config", json=cosmetic)
        first = json.loads(ws.receive_text())
    assert first["type"] == "config"
    assert first["config"]["playback"]["start_offset"] == 45


def test_quota_exhaustion_falls_back_to_stale_cache(app_env, monkeypatch):
    app, _, cache_dir = app_env
    ids = seed_cache(cache_dir)
    # Age the entry past its TTL so only the stale path can serve it.
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    entry = cache_dir / f"{cache.cache_key(params)}.json"
    payload = json.loads(entry.read_text())
    payload["fetched_at"] = 0
    entry.write_text(json.dumps(payload))

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
    app, _, _unused_cache_dir = app_env
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


def test_zero_results_is_not_an_error(app_env, monkeypatch):
    app, _, _ = app_env

    async def empty_search(params, api_key, *, client=None):
        return []

    monkeypatch.setattr(youtube, "search", empty_search)
    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["video_ids"] == []
    assert body["note"] == "no_results"


def test_the_player_page_is_served_at_the_root(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.get("/").status_code == 200


def test_the_config_page_is_served(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.get("/config").status_code == 200


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
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(GENERATING))
    cache_dir = tmp_path / "cache"
    settings = Settings(youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY")
    app = create_app(config_path=config_path, cache_dir=cache_dir, settings=settings)
    return app, config_path, cache_dir


def stub_search(monkeypatch, count=50, prefix="g"):
    async def fake_search(params, api_key, *, client=None):
        return [{"video_id": f"{prefix}{i}", "title": "T", "channel": "C"} for i in range(count)]

    monkeypatch.setattr(youtube, "search", fake_search)


def test_new_query_puts_the_generated_query_on_the_wall(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        return "shoegaze motown covers"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        body = client.post("/api/new-query").json()
    assert body["query"] == "shoegaze motown covers"
    assert len(body["video_ids"]) == 8


def test_the_generated_query_persists_for_later_requests(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        return "bossa nova covers"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")
        assert client.get("/api/videos").json()["query"] == "bossa nova covers"


def test_each_generation_is_told_what_was_already_used(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)
    seen = []
    counter = iter(range(100))

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        seen.append(list(avoid))
        return f"query {next(counter)}"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")
        client.post("/api/new-query")
        client.post("/api/new-query")

    assert seen[0] == []
    assert seen[1] == ["query 0"]
    assert seen[2] == ["query 0", "query 1"]


def test_new_query_broadcasts_to_the_wall(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        return "sea shanty covers"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        client.post("/api/new-query")
        message = json.loads(ws.receive_text())
    assert message["type"] == "videos"
    assert message["query"] == "sea shanty covers"


def test_new_query_is_refused_when_generation_is_disabled(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 409


def test_new_query_is_refused_without_a_gemini_key(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(GENERATING))
    settings = Settings(youtube_api_key="K", gemini_api_key=None)
    app = create_app(config_path=config_path, cache_dir=tmp_path / "c", settings=settings)
    with TestClient(app) as client:
        response = client.post("/api/new-query")
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_a_gemini_failure_leaves_the_previous_query_in_place(generating_env, monkeypatch):
    app, _, cache_dir = generating_env
    seed_cache(cache_dir)

    async def broken(theme, avoid, model, api_key=None, *, client=None):
        raise gemini.QueryGenerationError("model unavailable")

    monkeypatch.setattr(gemini, "generate_query", broken)
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 502
        # The wall must still work off the configured query.
        assert client.get("/api/videos").json()["query"] == "golden cover"


def test_the_daily_budget_blocks_a_new_search(app_env, monkeypatch):
    app, config_path, cache_dir = app_env
    config_path.write_text(yaml.safe_dump({**VALID, "quota": {"daily_limit_units": 100}}))
    budget.record_search(cache_dir)  # 100 spent, limit 100 -> next would be 200
    stub_search(monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/videos")
    assert response.status_code == 429
    assert "budget" in response.json()["detail"].lower()


def test_the_budget_serves_stale_cache_rather_than_failing_when_it_can(app_env, monkeypatch):
    app, config_path, cache_dir = app_env
    ids = seed_cache(cache_dir)
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    entry = cache_dir / f"{cache.cache_key(params)}.json"
    payload = json.loads(entry.read_text())
    payload["fetched_at"] = 0
    entry.write_text(json.dumps(payload))
    budget.record_search(cache_dir)
    config_path.write_text(yaml.safe_dump({**VALID, "quota": {"daily_limit_units": 100}}))

    with TestClient(app) as client:
        body = client.get("/api/videos").json()
    assert body["note"] == "budget_exceeded_stale"
    assert body["video_ids"] == ids[:8]


def test_the_budget_blocks_generation_before_calling_gemini(generating_env, monkeypatch):
    app, config_path, cache_dir = generating_env
    config_path.write_text(yaml.safe_dump({**GENERATING, "quota": {"daily_limit_units": 100}}))
    budget.record_search(cache_dir)
    called = []

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        called.append(theme)
        return "should not happen"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        assert client.post("/api/new-query").status_code == 429
    assert called == [], "Gemini must not be called when the search cannot be afforded"


def test_a_successful_search_records_its_cost(app_env, monkeypatch):
    app, _, cache_dir = app_env
    stub_search(monkeypatch)
    with TestClient(app) as client:
        client.get("/api/videos")
    assert budget.spent(cache_dir) == 100


def test_a_cached_result_costs_nothing(app_env):
    app, _, cache_dir = app_env
    seed_cache(cache_dir)
    with TestClient(app) as client:
        client.get("/api/videos")
    assert budget.spent(cache_dir) == 0


def test_editing_the_query_by_hand_overrides_a_generated_one(generating_env, monkeypatch):
    app, _, _ = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        return "generated thing"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")
        assert client.get("/api/videos").json()["query"] == "generated thing"
        client.put("/api/config", json={**GENERATING, "query": "typed by hand"})
        assert client.get("/api/videos").json()["query"] == "typed by hand"


def test_the_generated_query_survives_a_server_restart(generating_env, monkeypatch):
    app, config_path, cache_dir = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        return "persisted query"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")

    # A fresh app over the same cache dir is what a restart looks like.
    restarted = create_app(
        config_path=config_path,
        cache_dir=cache_dir,
        settings=Settings(youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY"),
    )
    with TestClient(restarted) as client:
        assert client.get("/api/videos").json()["query"] == "persisted query"


def test_a_restart_does_not_spend_quota_to_show_the_same_wall(generating_env, monkeypatch):
    app, config_path, cache_dir = generating_env
    stub_search(monkeypatch)

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        return "persisted query"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")
    spent_after_generation = budget.spent(cache_dir)

    restarted = create_app(
        config_path=config_path,
        cache_dir=cache_dir,
        settings=Settings(youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY"),
    )
    with TestClient(restarted) as client:
        client.get("/api/videos")
        client.get("/api/videos")

    assert budget.spent(cache_dir) == spent_after_generation, "reloads must be free"


def test_history_survives_a_restart_so_gemini_keeps_avoiding_old_queries(
    generating_env, monkeypatch
):
    app, config_path, cache_dir = generating_env
    stub_search(monkeypatch)
    seen = []
    counter = iter(range(100))

    async def fake_generate(theme, avoid, model, api_key=None, *, client=None):
        seen.append(list(avoid))
        return f"query {next(counter)}"

    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(app) as client:
        client.post("/api/new-query")

    restarted = create_app(
        config_path=config_path,
        cache_dir=cache_dir,
        settings=Settings(youtube_api_key="TEST_KEY", gemini_api_key="GEMINI_KEY"),
    )
    monkeypatch.setattr(gemini, "generate_query", fake_generate)
    with TestClient(restarted) as client:
        client.post("/api/new-query")

    assert seen[1] == ["query 0"], "the restarted server forgot what it had already used"


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


def test_settings_require_an_api_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="youtube_api_key|YOUTUBE_API_KEY"):
        Settings(_env_file=None)
