FROM python:3.13-slim

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
