import asyncio

import pytest
from pydantic import ValidationError

from ytmatrix.sets import (
    VideoSetPayload,
    ZoomSetPayload,
    delete_video_set,
    delete_zoom_set,
    list_video_set_names,
    list_zoom_set_names,
    load_video_set,
    load_zoom_set,
    save_video_set,
    save_zoom_set,
)
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


# --- zoom sets ---------------------------------------------------------


async def test_a_fresh_store_has_no_zoom_sets(store):
    assert await list_zoom_set_names(store) == []


async def test_loading_a_missing_zoom_set_is_none(store):
    assert await load_zoom_set(store, "nope") is None


async def test_a_zoom_set_round_trips(store):
    payload = ZoomSetPayload.model_validate(
        {"views": {"0": {"zoom": 1.4, "offsetX": 12, "offsetY": -8}}}
    )
    await save_zoom_set(store, "wide shot", payload)
    saved = await load_zoom_set(store, "wide shot")
    assert saved.name == "wide shot"
    assert saved.views["0"].zoom == 1.4
    assert saved.views["0"].offsetX == 12
    assert saved.views["0"].offsetY == -8
    assert saved.saved_at  # non-empty; exact format is not this test's concern


async def test_a_saved_zoom_set_name_is_listed(store):
    await save_zoom_set(store, "wide shot", ZoomSetPayload.model_validate({"views": {}}))
    assert await list_zoom_set_names(store) == ["wide shot"]


async def test_zoom_set_names_are_listed_sorted(store):
    for name in ["c", "a", "b"]:
        await save_zoom_set(store, name, ZoomSetPayload.model_validate({"views": {}}))
    assert await list_zoom_set_names(store) == ["a", "b", "c"]


async def test_a_zoom_set_name_with_spaces_and_slashes_round_trips(store):
    # Names are free text and become part of a storage key -- both characters
    # must survive being encoded into and back out of one.
    name = "act 2 / wide shot"
    await save_zoom_set(store, name, ZoomSetPayload.model_validate({"views": {}}))
    assert await list_zoom_set_names(store) == [name]
    assert (await load_zoom_set(store, name)).name == name


async def test_saving_under_an_existing_zoom_set_name_overwrites_it(store):
    await save_zoom_set(
        store,
        "n",
        ZoomSetPayload.model_validate({"views": {"0": {"zoom": 1, "offsetX": 0, "offsetY": 0}}}),
    )
    await save_zoom_set(
        store,
        "n",
        ZoomSetPayload.model_validate({"views": {"1": {"zoom": 2, "offsetX": 0, "offsetY": 0}}}),
    )
    saved = await load_zoom_set(store, "n")
    assert list(saved.views.keys()) == ["1"]
    assert await list_zoom_set_names(store) == ["n"]


async def test_deleting_a_zoom_set_removes_it(store):
    await save_zoom_set(store, "n", ZoomSetPayload.model_validate({"views": {}}))
    assert await delete_zoom_set(store, "n") is True
    assert await load_zoom_set(store, "n") is None
    assert await list_zoom_set_names(store) == []


async def test_deleting_a_missing_zoom_set_reports_false(store):
    assert await delete_zoom_set(store, "nope") is False


def test_zoom_set_payload_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        ZoomSetPayload.model_validate({"views": {}, "colour": "blue"})


def test_zoom_view_requires_all_three_fields():
    with pytest.raises(ValidationError):
        ZoomSetPayload.model_validate({"views": {"0": {"zoom": 1}}})


# --- video sets ----------------------------------------------------------


async def test_a_fresh_store_has_no_video_sets(store):
    assert await list_video_set_names(store) == []


async def test_a_video_set_round_trips(store):
    payload = VideoSetPayload.model_validate({"video_ids": ["a", "b"], "reserves": ["c"]})
    await save_video_set(store, "finale", payload)
    saved = await load_video_set(store, "finale")
    assert saved.name == "finale"
    assert saved.video_ids == ["a", "b"]
    assert saved.reserves == ["c"]


async def test_a_video_set_defaults_to_no_reserves(store):
    await save_video_set(store, "n", VideoSetPayload.model_validate({"video_ids": ["a"]}))
    assert (await load_video_set(store, "n")).reserves == []


async def test_deleting_a_video_set_removes_it(store):
    await save_video_set(store, "n", VideoSetPayload.model_validate({"video_ids": ["a"]}))
    assert await delete_video_set(store, "n") is True
    assert await load_video_set(store, "n") is None


async def test_deleting_a_missing_video_set_reports_false(store):
    assert await delete_video_set(store, "nope") is False


def test_video_set_payload_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        VideoSetPayload.model_validate({"video_ids": [], "colour": "blue"})


# --- server routes -------------------------------------------------------

from fastapi.testclient import TestClient

from ytmatrix.server import create_app
from ytmatrix.settings import Settings


@pytest.fixture
def app_env(tmp_path):
    default_path = tmp_path / "config.yaml"
    default_path.write_text("query: seeded\ngrid:\n  cols: 1\n  rows: 1\n")
    store = FileStore(tmp_path / "store")
    settings = Settings(youtube_api_key="TEST_KEY")
    app = create_app(store=store, settings=settings, default_config_path=default_path)
    return app, store


def test_get_zoom_sets_starts_empty(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.get("/api/zoom-sets").json() == []


def test_put_then_get_zoom_set(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        put = client.put(
            "/api/zoom-sets/wide%20shot",
            json={"views": {"0": {"zoom": 1.4, "offsetX": 12, "offsetY": -8}}},
        )
        assert put.status_code == 200
        assert client.get("/api/zoom-sets").json() == ["wide shot"]
        got = client.get("/api/zoom-sets/wide%20shot")
        assert got.status_code == 200
        assert got.json()["views"]["0"]["zoom"] == 1.4


def test_get_a_missing_zoom_set_is_404(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.get("/api/zoom-sets/nope").status_code == 404


def test_delete_a_missing_zoom_set_is_404(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.delete("/api/zoom-sets/nope").status_code == 404


def test_delete_a_zoom_set(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        client.put("/api/zoom-sets/n", json={"views": {}})
        assert client.delete("/api/zoom-sets/n").status_code == 200
        assert client.get("/api/zoom-sets").json() == []


def test_put_zoom_set_rejects_invalid_payload_with_422(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        response = client.put("/api/zoom-sets/n", json={"views": {"0": {"zoom": 1}}})
        assert response.status_code == 422


def test_a_rejected_zoom_set_put_leaves_an_existing_one_untouched(app_env):
    """Mirrors gotcha 8's config guarantee for this new resource."""
    app, store = app_env
    with TestClient(app) as client:
        client.put(
            "/api/zoom-sets/n", json={"views": {"0": {"zoom": 1, "offsetX": 0, "offsetY": 0}}}
        )
        response = client.put("/api/zoom-sets/n", json={"views": {"0": {"zoom": "not a number"}}})
        assert response.status_code == 422
    from ytmatrix.sets import load_zoom_set

    saved = asyncio.run(load_zoom_set(store, "n"))
    assert saved.views["0"].zoom == 1


def test_get_video_sets_starts_empty(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.get("/api/video-sets").json() == []


def test_put_then_get_video_set(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        put = client.put(
            "/api/video-sets/finale", json={"video_ids": ["a", "b"], "reserves": ["c"]}
        )
        assert put.status_code == 200
        assert client.get("/api/video-sets").json() == ["finale"]
        got = client.get("/api/video-sets/finale")
        assert got.status_code == 200
        assert got.json()["video_ids"] == ["a", "b"]
        assert got.json()["reserves"] == ["c"]


def test_get_a_missing_video_set_is_404(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.get("/api/video-sets/nope").status_code == 404


def test_delete_a_missing_video_set_is_404(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.delete("/api/video-sets/nope").status_code == 404


def test_delete_a_video_set(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        client.put("/api/video-sets/n", json={"video_ids": ["a"]})
        assert client.delete("/api/video-sets/n").status_code == 200
        assert client.get("/api/video-sets").json() == []


def test_put_video_set_rejects_invalid_payload_with_422(app_env):
    app, _ = app_env
    with TestClient(app) as client:
        assert client.put("/api/video-sets/n", json={"video_ids": "not a list"}).status_code == 422


def test_a_rejected_video_set_put_leaves_an_existing_one_untouched(app_env):
    """Mirrors gotcha 8's config guarantee for this new resource."""
    app, store = app_env
    with TestClient(app) as client:
        client.put("/api/video-sets/n", json={"video_ids": ["a"]})
        response = client.put("/api/video-sets/n", json={"video_ids": "not a list"})
        assert response.status_code == 422
    from ytmatrix.sets import load_video_set

    saved = asyncio.run(load_video_set(store, "n"))
    assert saved.video_ids == ["a"]
