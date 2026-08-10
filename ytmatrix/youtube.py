from __future__ import annotations

import asyncio

import httpx

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# search.list costs 100 quota units per call no matter how many results are
# requested, so there is never a reason to ask for fewer than the maximum.
MAX_RESULTS = 50

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)

REQUEST_TIMEOUT_SECONDS = 15.0


class SearchError(RuntimeError):
    """A YouTube search failed for a reason that is not quota exhaustion."""


class QuotaExceededError(SearchError):
    """The daily quota is spent. Distinguished because callers fall back to stale cache."""


def build_params(
    query: str,
    order: str,
    video_duration: str,
    safe_search: str,
    relevance_language: str | None,
) -> dict[str, str]:
    """Build the upstream parameter set, excluding the API key.

    The key is excluded on purpose: it does not affect which videos come back,
    and this dict is hashed to form the cache key.
    """
    params = {
        "part": "snippet",
        # Forced, not configurable: channels and playlists have no embeddable
        # video id, and non-embeddable videos cannot go on the wall at all.
        "type": "video",
        "videoEmbeddable": "true",
        "maxResults": str(MAX_RESULTS),
        "q": query,
        "order": order,
        "videoDuration": video_duration,
        "safeSearch": safe_search,
    }
    if relevance_language:
        params["relevanceLanguage"] = relevance_language
    return params


def _is_quota_error(response: httpx.Response) -> bool:
    try:
        errors = response.json()["error"]["errors"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(entry.get("reason") == "quotaExceeded" for entry in errors)


def _flatten(payload: dict) -> list[dict]:
    items = []
    for item in payload.get("items", []):
        video_id = (item.get("id") or {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet") or {}
        items.append(
            {
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
            }
        )
    return items


async def search(
    params: dict[str, str],
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        last_error: SearchError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = await client.get(SEARCH_URL, params={**params, "key": api_key})
            except httpx.HTTPError as exc:
                last_error = SearchError(f"transport error contacting YouTube: {exc}")
            else:
                if response.status_code == 403 and _is_quota_error(response):
                    # Retrying cannot help and the budget is already spent.
                    raise QuotaExceededError("YouTube API daily quota exceeded")
                if response.status_code >= 500:
                    last_error = SearchError(f"YouTube API returned {response.status_code}")
                elif response.status_code != 200:
                    # Never interpolate the response body wholesale here; the
                    # request URL it may echo back contains the API key.
                    raise SearchError(f"YouTube API error {response.status_code}")
                else:
                    return _flatten(response.json())

            if attempt < len(RETRY_BACKOFF_SECONDS):
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])

        raise last_error if last_error else SearchError("search failed")
    finally:
        if owns_client:
            await client.aclose()
