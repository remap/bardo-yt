import pytest

from ytmatrix import youtube


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
