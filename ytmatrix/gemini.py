"""Gemini-authored YouTube search queries.

Knows about Gemini and nothing else -- not config files, not the cache, not
YouTube. The caller passes primitives and gets a query string back.
"""

from __future__ import annotations

import logging
import time

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
- NAME REAL THINGS. Specific artists, groups, and song titles are wanted, not \
avoided -- a well-known song paired with "cover", "dance practice" or "live" \
returns dozens of different people performing it, which is exactly what fills \
a wall. "NewJeans Ditto dance cover" is a better query than "kpop dance".
- The only specificity to avoid is the kind that genuinely has no results: one \
obscure b-side by an unknown act, or a named individual covering a named song.
- Vary what you name. Do not return to the same artist or song repeatedly.
- Do not repeat or trivially reword any query in the avoid list.

CRITICAL. If the theme or the operator asks for covers, EVERY query you \
produce must contain a word that forces covers -- cover, dance cover, dance \
practice, busking, acoustic version, reinterpretation, remix, fan cam of a \
cover. Naming an artist and a song WITHOUT such a word returns that act's own \
official upload: one canonical video and its algorithmic neighbours, which is \
the opposite of a wall of different people. "BTS Dynamite" is wrong. \
"BTS Dynamite dance cover" is right. Whatever constraint the theme carries -- \
covers, live, a place, an instrument -- must survive into the query itself.
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


logger = logging.getLogger(__name__)


def build_prompt(theme: str, avoid: list[str], instruction: str | None = None) -> str:
    recent = avoid[-MAX_AVOID_ENTRIES:]
    parts = [f"Theme: {theme}"]
    if instruction:
        # The operator's steer wins over the standing theme, but the rules
        # above still apply -- this is a metaprompt, not a raw query. Typing
        # "sad piano" should still come back as something that returns dozens
        # of moving results.
        parts.append(
            "The operator has asked for this specifically, and it takes "
            f"precedence over the theme:\n{instruction}"
        )
    # The avoid-list is suppressed whenever the operator has steered, and that
    # is the whole point of the branch above: a steer that "takes precedence
    # over the theme" cannot also lose to a list of things not to say.
    #
    # It fought the steer badly in practice. The list is per-browser
    # localStorage history, so the same steer produced different queries on
    # different origins -- ask for "golden fingerstyle cover" from a browser
    # that had already generated golden queries and Gemini dutifully avoided
    # golden, returning fingerstyle covers of other songs. Measured: with an
    # empty list the steer came back verbatim 2 of 3 times; with five golden
    # entries it never did, and dropped golden entirely 1 of 3.
    #
    # Repeats are fine when they were asked for. Repeating a steer is also
    # nearly free -- the same query is a search cache hit, so it spends the
    # Gemini call and no quota.
    if recent and not instruction:
        parts.append("Avoid these, already used:\n" + "\n".join(f"- {q}" for q in recent))
    parts.append("Invent one new search query.")
    return "\n\n".join(parts)


async def generate_query(
    theme: str,
    avoid: list[str],
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    *,
    instruction: str | None = None,
    client=None,
) -> str:
    client = client or genai.Client(api_key=api_key)
    prompt = build_prompt(theme, avoid, instruction)
    started = time.monotonic()
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=[SYSTEM_PROMPT, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedQuery,
                # Pinned, because the default is DYNAMIC and this task does not
                # need it. Measured on gemini-3.6-flash: the default spent 730
                # then 285 thinking tokens on consecutive calls to produce a
                # ~50-token query, taking 5.3s and then 2.8s -- the swing is
                # what makes New query feel unpredictable rather than merely
                # slow. "minimal" removes thinking entirely: mean 1.43s over
                # four calls, range 1.24-1.62s.
                #
                # Quality is unaffected, which matters because gotcha 25 rests
                # on it: 8/8 generated queries still carried a cover word, so
                # none of them would return an act's own official upload. If
                # you raise this, re-check that -- a query naming an artist and
                # song with no cover word is the one failure mode here.
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
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
    # Logged in full, deliberately. The steer is a metaprompt, so what actually
    # reaches YouTube is whatever Gemini made of it -- and when the wall shows
    # something unexpected, this line is the difference between "Gemini ignored
    # me" and "Gemini said something reasonable and the search disagreed".
    logger.info(
        "gemini %.2fs model=%s avoid=%d steer=%r -> query=%r",
        time.monotonic() - started,
        model,
        len(avoid) if not instruction else 0,
        instruction,
        query,
    )
    return query
