import pytest

from ytmatrix import motion, server, youtube


@pytest.fixture(autouse=True)
def no_thumbnail_fetches(request, monkeypatch):
    """Keep storyboard/motion scoring off the network in the default suite.

    Motion scoring fetches three images per video from i.ytimg.com. That costs
    no quota, but it is still the network: it makes the suite slow, flaky, and
    dependent on ids that only exist in a fixture. Default to "moving" -- the
    same thing an unmeasurable video gets -- and let tests that care stub it.
    """
    if request.node.get_closest_marker("browser") or request.node.get_closest_marker("live"):
        return

    async def unmeasured(video_id, store, client):
        return motion.UNKNOWN_SCORE

    monkeypatch.setattr(server, "motion_score", unmeasured)

    # Country lookup is two more network calls per query. Same rule: no test
    # in the default suite touches the wire.
    async def no_countries(video_ids, store, api_key):
        return {}

    monkeypatch.setattr(server, "video_countries", no_countries)


@pytest.fixture(autouse=True)
def no_live_api(request, monkeypatch):
    """Fail loudly if any default-suite test reaches the real YouTube API.

    The budget is 100 searches a day. A test that silently spends one is worse
    than a test that fails: tests run often, and quota does not come back until
    midnight Pacific. Tests that need a search stub `youtube.search` themselves;
    that monkeypatch is applied after this one and wins.
    """
    # Two exemptions. test_youtube.py exercises search() itself and is already
    # safe -- every call goes through an injected MockTransport. Tests marked
    # `live` are opt-in and reaching the network is the entire point.
    if request.module.__name__.rsplit(".", 1)[-1] == "test_youtube":
        return
    if request.node.get_closest_marker("live"):
        return

    async def forbidden(*args, **kwargs):
        raise AssertionError(
            "A test tried to call the live YouTube API. Seed the cache or stub "
            "youtube.search. Only tests marked `live` may hit the network."
        )

    monkeypatch.setattr(youtube, "search", forbidden)
