"""One real call against the YouTube API.

Excluded from the default suite (`addopts = "-m 'not live'"`) because it spends
100 of the 10,000 daily quota units. Run deliberately:

    uv run pytest -m live -v

This exists because audio-snippet learned the hard way that a fully mocked
boundary hides bugs that live only at the boundary.
"""

import os

import pytest

from ytmatrix import youtube

pytestmark = pytest.mark.live


@pytest.fixture
def api_key():
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        pytest.skip("YOUTUBE_API_KEY is not set")
    return key


async def test_a_real_search_returns_embeddable_videos(api_key):
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    items = await youtube.search(params, api_key)

    assert len(items) >= 8, "expected enough results to fill an 8-cell grid"
    assert len(items) <= 50
    for item in items:
        assert item["video_id"]
        assert set(item) == {"video_id", "title", "channel"}
    assert len({item["video_id"] for item in items}) == len(items), "duplicate video ids"
