# tests/test_store.py
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

from ytmatrix.store import FileStore, R2Store, r2_client


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_get_missing_key_is_none(store):
    assert await store.get("nope.json") is None


async def test_put_then_get_roundtrips(store):
    await store.put("a/b.json", b'{"x": 1}')
    assert await store.get("a/b.json") == b'{"x": 1}'


async def test_put_creates_nested_prefixes(store):
    await store.put("users/a@b.com/wall.json", b"{}")
    assert await store.get("users/a@b.com/wall.json") == b"{}"


async def test_get_with_version_returns_stable_version(store):
    await store.put("k", b"hello")
    first = await store.get_with_version("k")
    second = await store.get_with_version("k")
    assert first is not None and first[0] == b"hello"
    assert first[1] == second[1]


async def test_version_changes_when_content_changes(store):
    await store.put("k", b"one")
    before = (await store.get_with_version("k"))[1]
    await store.put("k", b"two")
    after = (await store.get_with_version("k"))[1]
    assert before != after


async def test_put_if_version_none_creates_only_when_absent(store):
    assert await store.put_if_version("k", b"first", None) is True
    assert await store.put_if_version("k", b"second", None) is False
    assert await store.get("k") == b"first"


async def test_put_if_version_matching_succeeds(store):
    await store.put("k", b"one")
    version = (await store.get_with_version("k"))[1]
    assert await store.put_if_version("k", b"two", version) is True
    assert await store.get("k") == b"two"


async def test_put_if_version_stale_is_refused(store):
    await store.put("k", b"one")
    stale = (await store.get_with_version("k"))[1]
    await store.put("k", b"two")
    assert await store.put_if_version("k", b"three", stale) is False
    assert await store.get("k") == b"two"


async def test_list_keys_filters_by_prefix(store):
    await store.put("logs/2026-08-16/a.json", b"{}")
    await store.put("logs/2026-08-16/b.json", b"{}")
    await store.put("logs/2026-08-17/c.json", b"{}")
    await store.put("users/x/config.yaml", b"{}")
    assert await store.list_keys("logs/2026-08-16/") == [
        "logs/2026-08-16/a.json",
        "logs/2026-08-16/b.json",
    ]


async def test_list_keys_is_sorted(store):
    for name in ["c", "a", "b"]:
        await store.put(f"logs/{name}.json", b"{}")
    assert await store.list_keys("logs/") == [
        "logs/a.json",
        "logs/b.json",
        "logs/c.json",
    ]


async def test_list_keys_missing_prefix_is_empty(store):
    assert await store.list_keys("nothing/") == []


# --- R2Store: the conditional-write mechanism itself ---
#
# `r2_client` builds a real boto3 S3 client and teaches it, via two botocore
# event handlers, to send the If-Match/If-None-Match headers R2 needs for a
# real compare-and-swap. Nothing above this point exercises that wiring: the
# FileStore tests never touch boto3, and unit-testing lift_custom_headers /
# apply_custom_headers directly would just reimplement them under another
# name. Instead these tests build a real client through r2_client(), and
# intercept it one stage later than PutObject/GetObject's own handlers, on
# `before-send`. By the time that event fires, `apply_custom_headers` has
# already mutated the outgoing headers -- so capturing them there proves the
# real production handlers ran, under their real registered event names, and
# put the real header on the real request, without a network call ever
# happening: this handler substitutes a canned response instead of letting
# botocore open a socket.


class _FakeRaw:
    """Just enough of urllib3's response object for AWSResponse.content."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **kwargs):
        yield self._body

    def read(self, *args, **kwargs):
        return self._body


def _fake_response(status_code: int, body: bytes = b"", headers: dict | None = None):
    return AWSResponse(
        url="https://example.r2.cloudflarestorage.com/",
        status_code=status_code,
        headers=headers or {},
        raw=_FakeRaw(body),
    )


_NO_SUCH_KEY = b"<Error><Code>NoSuchKey</Code><Message>nope</Message></Error>"
_ACCESS_DENIED = b"<Error><Code>AccessDenied</Code><Message>denied</Message></Error>"
_PRECONDITION_FAILED = b"<Error><Code>PreconditionFailed</Code><Message>stale</Message></Error>"


@pytest.fixture
def r2():
    """A real r2_client(), wired to a recorder instead of the network."""
    client = r2_client("test-account", "test-key-id", "test-secret")
    captured: dict = {}

    def record_and_respond(request, **kwargs):
        captured["headers"] = dict(request.headers)
        return captured["next_response"]

    client.meta.events.register("before-send.s3.PutObject", record_and_respond)
    client.meta.events.register("before-send.s3.GetObject", record_and_respond)

    def set_response(status_code: int, body: bytes = b"", headers: dict | None = None) -> None:
        captured["next_response"] = _fake_response(status_code, body, headers)

    return R2Store(client, "test-bucket"), captured, set_response


async def test_r2_create_only_sends_if_none_match_star(r2):
    store, captured, set_response = r2
    set_response(200, headers={"ETag": '"etag1"'})
    assert await store.put_if_version("k", b"first", None) is True
    assert captured["headers"]["If-None-Match"] == b"*"


async def test_r2_compare_and_swap_sends_if_match_with_version(r2):
    store, captured, set_response = r2
    set_response(200, headers={"ETag": '"etag2"'})
    assert await store.put_if_version("k", b"two", '"etag1"') is True
    assert captured["headers"]["If-Match"] == b'"etag1"'


async def test_r2_precondition_failure_returns_false_not_raise(r2):
    store, _, set_response = r2
    set_response(412, _PRECONDITION_FAILED)
    assert await store.put_if_version("k", b"three", '"stale"') is False


async def test_r2_unrelated_client_error_propagates_from_put(r2):
    store, _, set_response = r2
    set_response(403, _ACCESS_DENIED)
    with pytest.raises(ClientError):
        await store.put_if_version("k", b"four", '"etag1"')


async def test_r2_unrelated_client_error_propagates_from_get(r2):
    store, _, set_response = r2
    set_response(403, _ACCESS_DENIED)
    with pytest.raises(ClientError):
        await store.get_with_version("k")


async def test_r2_get_missing_key_returns_none(r2):
    store, _, set_response = r2
    set_response(404, _NO_SUCH_KEY)
    assert await store.get("missing") is None
