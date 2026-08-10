"""Gemini-authored YouTube search queries.

Knows about Gemini and nothing else -- not config files, not the cache, not
YouTube. The caller passes primitives and gets a query string back.
"""

from __future__ import annotations

from google import genai
from google.genai import types
from pydantic import BaseModel

DEFAULT_MODEL = "gemini-3.6-flash"

# Every generated query costs a 100-unit YouTube search when it is used, so the
# prompt is written to avoid near-duplicates: a query that returns the same
# videos as the last one spends the same quota for nothing.
SYSTEM_PROMPT = """\
You invent YouTube search queries for a silent-by-default video wall in a \
cafe. The wall shows eight videos at once, so a good query is one that \
returns many different takes on the same idea rather than one canonical video.

What matters most: the results must be things that MOVE. A huge share of \
music results on YouTube are a single static album cover with audio over it, \
which is useless on a wall. Bias every query toward footage of something \
happening in front of a camera.

Rules:
- 2 to 6 words. Plain search terms, no quotes, no boolean operators.
- Favour words that imply a filmed event: live, performance, session, \
busking, rehearsal, backstage, studio, street, stage, workshop, on location.
- Prefer people doing something visible -- playing, building, cooking, \
dancing, repairing -- over topics that would be illustrated by a still image.
- Avoid words that attract static uploads: album, full album, playlist, \
lyrics, audio, mix, compilation, soundtrack, OST, 1 hour, extended.
- It must plausibly return dozens of results. Avoid anything so specific that \
only one or two videos exist.
- Do not repeat or trivially reword any query in the avoid list.
"""

# A hard ceiling on how much history goes into the prompt. Without it the
# avoid list grows unbounded across a long run and eventually dominates the
# request.
MAX_AVOID_ENTRIES = 40


class GeneratedQuery(BaseModel):
    query: str
    rationale: str


class QueryGenerationError(RuntimeError):
    """Gemini did not return a usable query."""


def build_prompt(theme: str, avoid: list[str]) -> str:
    recent = avoid[-MAX_AVOID_ENTRIES:]
    parts = [f"Theme: {theme}"]
    if recent:
        parts.append("Avoid these, already used:\n" + "\n".join(f"- {q}" for q in recent))
    parts.append("Invent one new search query that fits the theme.")
    return "\n\n".join(parts)


async def generate_query(
    theme: str,
    avoid: list[str],
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    *,
    client=None,
) -> str:
    client = client or genai.Client(api_key=api_key)
    prompt = build_prompt(theme, avoid)
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[SYSTEM_PROMPT, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedQuery,
            ),
        )
    except Exception as exc:  # the SDK raises a wide, undocumented range
        raise QueryGenerationError(f"Gemini request failed: {exc}") from exc

    spec = response.parsed
    if spec is None:
        raise QueryGenerationError("Gemini returned no parseable structured output")

    query = spec.query.strip().strip('"').strip()
    if not query:
        raise QueryGenerationError("Gemini returned an empty query")
    return query
