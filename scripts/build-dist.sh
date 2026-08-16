#!/usr/bin/env bash
# Assemble the static bundle the Worker serves.
#
# The HTML asks for /static/player.js, so the JS keeps that path and the HTML
# needs no rewriting. player.html becomes index.html because Workers static
# assets serve index.html at "/", and config.html is served at "/config" by
# the default .html-stripping behaviour -- which is exactly what the existing
# <a href="/config"> and <a href="/"> links already expect.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dist="$root/dist"

rm -rf "$dist"
mkdir -p "$dist/static"

cp "$root/static/player.html" "$dist/index.html"
cp "$root/static/config.html" "$dist/config.html"
# *.js only: grid-logic.test.mjs is a node test and must not ship.
cp "$root"/static/*.js "$dist/static/"

echo "built $dist"
