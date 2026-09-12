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
    await save_zoom_set(store, "n", ZoomSetPayload.model_validate({"views": {"0": {"zoom": 1, "offsetX": 0, "offsetY": 0}}}))
    await save_zoom_set(store, "n", ZoomSetPayload.model_validate({"views": {"1": {"zoom": 2, "offsetX": 0, "offsetY": 0}}}))
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
