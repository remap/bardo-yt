# check=skip=FromPlatformFlagConstDisallowed
# ^ Must be the first line: BuildKit only reads parser directives before any
# other content. It warns that a constant --platform hurts portability; here
# that is the point, because this image has exactly one target and inheriting
# the host's architecture is the bug rather than the feature.

# linux/amd64 is pinned, not inherited from the build host. Cloudflare
# Containers run amd64 only, and `docker build` on an Apple Silicon Mac
# otherwise produces an arm64 image that Cloudflare cannot run.
#
# It fails locally too, and not in a way that points at the cause: the
# arm64 wheel for google-genai (pulled in by ytmatrix/gemini.py) dies on
# `from google import genai` with SIGILL — an illegal-instruction crash,
# exit 132, no traceback, no error message. Everything else in the image
# imports fine, so the process simply vanishes during startup.
FROM --platform=linux/amd64 python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so an edit to ytmatrix/ does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ytmatrix/ ./ytmatrix/
# The committed config is the template every new user's wall starts from.
COPY config.yaml ./config.yaml
RUN uv sync --frozen --no-dev

ENV PORT=8080
# Required for `wrangler dev` to reach the container locally.
EXPOSE 8080

CMD ["uv", "run", "--no-dev", "python", "-m", "ytmatrix.container"]
