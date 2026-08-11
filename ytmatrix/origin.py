"""Spread the wall across countries of origin.

`search.list` returns no country field at all. It can be recovered in two
cheap hops, both batched 50 ids at a time and 1 unit each -- 2 units against
the 100 the search itself costs:

    videos.list?part=snippet   -> channelId per video
    channels.list?part=snippet -> snippet.country per channel

Coverage is partial: measured on a real k-pop cover result set, 29 of 50
videos had a country, spanning 12 of them. That is more than enough to spread
eight cells, which is why unknown origin is treated as a bucket to draw from
rather than a reason to drop a video.
"""

from __future__ import annotations

VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

# Both endpoints accept up to 50 ids per request, and cost one unit per
# request regardless. Anything smaller wastes units.
MAX_IDS_PER_CALL = 50

UNKNOWN = None


def diversify(candidates: list[tuple[str, str | None]]) -> list[str]:
    """Reorder so consecutive picks come from different countries.

    Round-robin across country buckets, taking the most relevant unused video
    from each in turn. Buckets are visited in order of first appearance, so
    the top search result stays first and relevance is preserved *within* each
    country -- this reorders, it never re-ranks on quality.

    Videos of unknown origin form one bucket rather than one bucket each, so
    they take their turn like any other country instead of flooding the grid.
    Nothing is dropped: every input appears exactly once in the output.
    """
    buckets: dict[str | None, list[str]] = {}
    for video_id, country in candidates:
        buckets.setdefault(country, []).append(video_id)

    order = list(buckets)
    out: list[str] = []
    while len(out) < len(candidates):
        progressed = False
        for country in order:
            bucket = buckets[country]
            if bucket:
                out.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break
    return out


def parse_channel_ids(payload: dict) -> dict[str, str]:
    """video id -> channel id, from a videos.list response."""
    mapping = {}
    for item in payload.get("items", []):
        snippet = item.get("snippet") or {}
        channel_id = snippet.get("channelId")
        if item.get("id") and channel_id:
            mapping[item["id"]] = channel_id
    return mapping


def parse_countries(payload: dict) -> dict[str, str | None]:
    """channel id -> ISO country code, from a channels.list response.

    The field is optional and plenty of channels never set it, so a missing
    country is normal and recorded as None rather than treated as an error.
    """
    return {
        item["id"]: (item.get("snippet") or {}).get("country")
        for item in payload.get("items", [])
        if item.get("id")
    }


def chunk(ids: list[str], size: int = MAX_IDS_PER_CALL) -> list[list[str]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]
