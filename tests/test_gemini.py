import pytest

from ytmatrix import gemini


class FakeResponse:
    def __init__(self, parsed):
        self.parsed = parsed


class FakeModels:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    def __init__(self, result):
        self.aio = type("Aio", (), {"models": FakeModels(result)})()


def client_returning(query: str | None, rationale: str = "because"):
    parsed = None if query is None else gemini.GeneratedQuery(query=query, rationale=rationale)
    return FakeClient(FakeResponse(parsed))


async def test_returns_the_generated_query():
    client = client_returning("shoegaze covers of motown")
    result = await gemini.generate_query("cover songs", [], "gemini-3.6-flash", client=client)
    assert result == "shoegaze covers of motown"


async def test_strips_surrounding_whitespace_and_quotes():
    client = client_returning('  "bossa nova covers"  ')
    result = await gemini.generate_query("cover songs", [], "gemini-3.6-flash", client=client)
    assert result == "bossa nova covers"


async def test_passes_the_theme_to_the_model():
    client = client_returning("x")
    await gemini.generate_query("sea shanties", [], "gemini-3.6-flash", client=client)
    sent = str(client.aio.models.calls[0]["contents"])
    assert "sea shanties" in sent


async def test_tells_the_model_what_to_avoid():
    client = client_returning("x")
    await gemini.generate_query(
        "cover songs", ["golden cover", "silver cover"], "gemini-3.6-flash", client=client
    )
    sent = str(client.aio.models.calls[0]["contents"])
    assert "golden cover" in sent
    assert "silver cover" in sent


async def test_requests_structured_output():
    client = client_returning("x")
    await gemini.generate_query("t", [], "gemini-3.6-flash", client=client)
    config = client.aio.models.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is gemini.GeneratedQuery


async def test_uses_the_configured_model():
    client = client_returning("x")
    await gemini.generate_query("t", [], "gemini-9.9-experimental", client=client)
    assert client.aio.models.calls[0]["model"] == "gemini-9.9-experimental"


async def test_an_unparseable_response_raises_rather_than_returning_nothing():
    client = client_returning(None)
    with pytest.raises(gemini.QueryGenerationError):
        await gemini.generate_query("t", [], "gemini-3.6-flash", client=client)


async def test_an_empty_query_raises():
    client = client_returning("   ")
    with pytest.raises(gemini.QueryGenerationError):
        await gemini.generate_query("t", [], "gemini-3.6-flash", client=client)


async def test_an_sdk_failure_is_wrapped_in_our_own_error_type():
    client = FakeClient(RuntimeError("upstream exploded"))
    with pytest.raises(gemini.QueryGenerationError):
        await gemini.generate_query("t", [], "gemini-3.6-flash", client=client)


async def test_the_avoid_list_is_capped_so_the_prompt_cannot_grow_without_bound():
    client = client_returning("x")
    await gemini.generate_query(
        "t", [f"query {i}" for i in range(500)], "gemini-3.6-flash", client=client
    )
    sent = str(client.aio.models.calls[0]["contents"])
    assert "query 499" in sent, "the most recent queries must be the ones kept"
    assert "query 0" not in sent, "the oldest should have been dropped"
