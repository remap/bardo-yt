import httpx
import pytest

from ytmatrix import youtube


def response_payload(*video_ids):
    return {
        "items": [
            {
                "id": {"kind": "youtube#video", "videoId": vid},
                "snippet": {"title": f"Title {vid}", "channelTitle": f"Channel {vid}"},
            }
            for vid in video_ids
        ]
    }


def client_returning(*responses):
    remaining = list(responses)
    calls = []

    def handler(request):
        calls.append(request)
        return remaining.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


def test_build_params_forces_video_type_and_embeddable():
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    assert params["type"] == "video"
    assert params["videoEmbeddable"] == "true"


def test_build_params_always_requests_the_maximum_page():
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    assert params["maxResults"] == "50"


def test_build_params_never_includes_the_api_key():
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    assert not any(k.lower() == "key" for k in params)


def test_build_params_omits_relevance_language_when_unset():
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", None)
    assert "relevanceLanguage" not in params


async def test_search_returns_flattened_items():
    client, _ = client_returning(httpx.Response(200, json=response_payload("aaa", "bbb")))
    async with client:
        items = await youtube.search({"q": "x"}, "KEY", client=client)
    assert items == [
        {"video_id": "aaa", "title": "Title aaa", "channel": "Channel aaa"},
        {"video_id": "bbb", "title": "Title bbb", "channel": "Channel bbb"},
    ]


async def test_search_sends_the_api_key_as_a_query_parameter():
    client, calls = client_returning(httpx.Response(200, json=response_payload("aaa")))
    async with client:
        await youtube.search({"q": "x"}, "SECRET", client=client)
    assert calls[0].url.params["key"] == "SECRET"


async def test_search_skips_items_without_a_video_id():
    payload = {"items": [{"id": {"kind": "youtube#channel"}, "snippet": {}}]}
    client, _ = client_returning(httpx.Response(200, json=payload))
    async with client:
        assert await youtube.search({"q": "x"}, "KEY", client=client) == []


async def test_search_returns_an_empty_list_for_no_results():
    client, _ = client_returning(httpx.Response(200, json={"items": []}))
    async with client:
        assert await youtube.search({"q": "x"}, "KEY", client=client) == []


async def test_quota_exhaustion_raises_its_own_error_type():
    body = {"error": {"errors": [{"reason": "quotaExceeded"}], "code": 403}}
    client, _ = client_returning(httpx.Response(403, json=body))
    async with client:
        with pytest.raises(youtube.QuotaExceededError):
            await youtube.search({"q": "x"}, "KEY", client=client)


async def test_a_non_quota_403_is_a_plain_search_error():
    body = {"error": {"errors": [{"reason": "keyInvalid"}], "code": 403}}
    client, _ = client_returning(httpx.Response(403, json=body))
    async with client:
        with pytest.raises(youtube.SearchError):
            await youtube.search({"q": "x"}, "KEY", client=client)


async def test_quota_exhaustion_is_not_retried(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(youtube.asyncio, "sleep", fake_sleep)
    body = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    client, calls = client_returning(httpx.Response(403, json=body))
    async with client:
        with pytest.raises(youtube.QuotaExceededError):
            await youtube.search({"q": "x"}, "KEY", client=client)
    assert len(calls) == 1
    assert slept == []


async def test_a_5xx_is_retried_and_can_succeed(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(youtube.asyncio, "sleep", fake_sleep)
    client, calls = client_returning(
        httpx.Response(503),
        httpx.Response(200, json=response_payload("aaa")),
    )
    async with client:
        items = await youtube.search({"q": "x"}, "KEY", client=client)
    assert len(items) == 1
    assert len(calls) == 2
    assert slept == [1.0]


async def test_persistent_5xx_gives_up_after_three_attempts(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(youtube.asyncio, "sleep", fake_sleep)
    client, calls = client_returning(
        httpx.Response(500), httpx.Response(500), httpx.Response(500)
    )
    async with client:
        with pytest.raises(youtube.SearchError):
            await youtube.search({"q": "x"}, "KEY", client=client)
    assert len(calls) == 3
    assert slept == [1.0, 2.0]


async def test_a_transport_error_is_retried(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(youtube.asyncio, "sleep", fake_sleep)
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=response_payload("aaa"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await youtube.search({"q": "x"}, "KEY", client=client)
    assert len(items) == 1
    assert len(attempts) == 3


async def test_the_api_key_never_appears_in_an_error_message():
    client, _ = client_returning(httpx.Response(400, text="Bad Request"))
    async with client:
        with pytest.raises(youtube.SearchError) as excinfo:
            await youtube.search({"q": "x"}, "SUPERSECRETKEY", client=client)
    assert "SUPERSECRETKEY" not in str(excinfo.value)
