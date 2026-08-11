#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Port 8444 keeps this clear of layout-driver (8443) and audio-snippet (8010),
# so all three can run at once.
export YTMATRIX_HOST="${YTMATRIX_HOST:-0.0.0.0}"
export YTMATRIX_PORT="${YTMATRIX_PORT:-8444}"

if [[ ! -f .env ]]; then
  echo "No .env found. Copy .env.example and set YOUTUBE_API_KEY." >&2
  exit 1
fi

# main() generates the self-signed cert on first run before uvicorn binds.
exec uv run python -m ytmatrix.main
