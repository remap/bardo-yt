"""Named, server-side saved sets: a zoom/pan "look" and a video set, both
independent of the shared config/search mechanism.

JSON, not YAML like config.py: these are machine-generated records nobody
hand-edits, one JSON object per name -- the same shape querylog.py already
uses for its own per-entry records, for the same reason.
"""

from __future__ import annotations

import json
import urllib.parse

from pydantic import BaseModel, ConfigDict, Field

from ytmatrix.querylog import local_timestamp
from ytmatrix.store import Store


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ZoomView(Strict):
    zoom: float
    offsetX: float
    offsetY: float


class ZoomSetPayload(Strict):
    views: dict[str, ZoomView]


class ZoomSet(ZoomSetPayload):
    name: str
    saved_at: str


class VideoSetPayload(Strict):
    video_ids: list[str]
    reserves: list[str] = Field(default_factory=list)


class VideoSet(VideoSetPayload):
    name: str
    saved_at: str


ZOOM_SET_PREFIX = "zoom-sets/"
VIDEO_SET_PREFIX = "video-sets/"


def _key(prefix: str, name: str) -> str:
    # quote(..., safe="") so a name containing "/" cannot spill across the
    # prefix boundary and land somewhere else in the key namespace.
    return f"{prefix}{urllib.parse.quote(name, safe='')}.json"


def _name_from_key(prefix: str, key: str) -> str:
    return urllib.parse.unquote(key[len(prefix) : -len(".json")])


async def list_zoom_set_names(store: Store) -> list[str]:
    keys = await store.list_keys(ZOOM_SET_PREFIX)
    return sorted(_name_from_key(ZOOM_SET_PREFIX, key) for key in keys)


async def load_zoom_set(store: Store, name: str) -> ZoomSet | None:
    raw = await store.get(_key(ZOOM_SET_PREFIX, name))
    if raw is None:
        return None
    return ZoomSet.model_validate(json.loads(raw))


async def save_zoom_set(store: Store, name: str, payload: ZoomSetPayload) -> ZoomSet:
    zoom_set = ZoomSet(name=name, saved_at=local_timestamp(), **payload.model_dump())
    await store.put(_key(ZOOM_SET_PREFIX, name), zoom_set.model_dump_json().encode("utf-8"))
    return zoom_set


async def delete_zoom_set(store: Store, name: str) -> bool:
    key = _key(ZOOM_SET_PREFIX, name)
    if await store.get(key) is None:
        return False
    await store.delete(key)
    return True


async def list_video_set_names(store: Store) -> list[str]:
    keys = await store.list_keys(VIDEO_SET_PREFIX)
    return sorted(_name_from_key(VIDEO_SET_PREFIX, key) for key in keys)


async def load_video_set(store: Store, name: str) -> VideoSet | None:
    raw = await store.get(_key(VIDEO_SET_PREFIX, name))
    if raw is None:
        return None
    return VideoSet.model_validate(json.loads(raw))


async def save_video_set(store: Store, name: str, payload: VideoSetPayload) -> VideoSet:
    video_set = VideoSet(name=name, saved_at=local_timestamp(), **payload.model_dump())
    await store.put(_key(VIDEO_SET_PREFIX, name), video_set.model_dump_json().encode("utf-8"))
    return video_set


async def delete_video_set(store: Store, name: str) -> bool:
    key = _key(VIDEO_SET_PREFIX, name)
    if await store.get(key) is None:
        return False
    await store.delete(key)
    return True
