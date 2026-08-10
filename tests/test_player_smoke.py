"""Browser smoke test: the wall must actually build its players.

Marked `browser` and excluded from the default suite because it launches
Chromium. It uses a pre-seeded cache, so it never spends quota.

This exists because of a real bug: the page rendered completely blank with no
console errors. `YT.Player` was defined and the app had registered
`onYouTubeIframeAPIReady`, but the API had already finished loading before the
deferred module script ran, so the callback was never invoked and the grid was
never built. Nothing in the Python suite or the node tests could have caught
that -- it only exists in the browser, in the ordering between a classic
script and a module.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import sync_playwright

from ytmatrix import cache, youtube

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.browser

CONFIG = {
    "query": "golden cover",
    "grid": {"cols": 4, "rows": 2},
    "search": {
        "order": "relevance",
        "video_duration": "any",
        "safe_search": "moderate",
        "relevance_language": "en",
    },
    "playback": {"muted": True, "autoplay_on_change": True, "start_offset": 0, "loop": True},
    "cache": {"ttl_hours": 24},
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    """A real server over TLS, backed by a seeded cache so no quota is spent."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(CONFIG))

    cache_dir = tmp_path / "cache"
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    # Real ids from a live search. Seeded to the full 50 a real search returns,
    # because whether any given video is embeddable changes over time: with a
    # reserve pool behind them, one that stops working is substituted and the
    # cell count assertion stays true. Seeding only 8 makes this test fail for
    # a reason that has nothing to do with the code under test.
    base = [
        "43DOm50YWaI",
        "1qwq1UCG9c4",
        "AuaABDWFs_8",
        "0-xSuAexKTw",
        "huqUnIVAjHg",
        "GnDfJC1vPlQ",
        "R7EH2TKJHYQ",
        "uSAPVDS2LUo",
    ]
    ids = [base[i % len(base)] for i in range(50)]
    cache.write(cache_dir, params, [{"video_id": v, "title": v, "channel": "c"} for v in ids])

    port = _find_free_port()
    env = {
        **os.environ,
        "YOUTUBE_API_KEY": "SMOKE_TEST_KEY_UNUSED",
        "YTMATRIX_HOST": "127.0.0.1",
        "YTMATRIX_PORT": str(port),
        "YTMATRIX_CONFIG_PATH": str(config_path),
        "YTMATRIX_CACHE_DIR": str(cache_dir),
        "YTMATRIX_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "ytmatrix.main"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if (
                    httpx.get(
                        f"https://localhost:{port}/healthz", verify=False, timeout=1.0
                    ).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("server did not become healthy in time")
        # localhost, NOT 127.0.0.1. YouTube rejects a 127.0.0.1 page as an embed
        # origin with error 150 ("embedding disallowed") while accepting
        # localhost against the very same server. Using the IP here makes every
        # player fail for a reason that has nothing to do with this code.
        yield f"https://localhost:{port}/"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_the_wall_builds_one_player_per_cell(running_server):
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(running_server, wait_until="load")

        # The grid must populate. This is the assertion that was failing:
        # eight cells, each containing a real YouTube iframe.
        page.wait_for_selector(".cell iframe", timeout=20_000)
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        assert page.locator(".cell").count() == 8
        assert page.locator('.cell[data-empty="true"]').count() == 0
        browser.close()

    assert errors == [], f"page errors: {errors}"


def test_one_play_click_reaches_every_player(running_server):
    """The single Play button must drive all eight players, not just the first.

    This asserts the click reaches every player, not that YouTube then plays.
    Whether a given video actually starts depends on rights, region and the
    ad roll -- none of which this code controls, and all of which would make
    the assertion flaky. Real playback (all eight in state 1) was confirmed
    against a live server; what belongs in an automated test is the part the
    app is responsible for.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        # An iframe existing is not the same as a player being usable: the API
        # only attaches playVideo and friends after its ready handshake.
        page.wait_for_function(
            "window.__players?.every(p => typeof p?.playVideo === 'function')",
            timeout=25_000,
        )

        page.evaluate("""
            window.__playCalls = 0;
            for (const player of window.__players) {
                const original = player.playVideo.bind(player);
                player.playVideo = () => { window.__playCalls += 1; return original(); };
            }
        """)
        page.click("#play")
        # >= 8, not == 8. A player whose onReady lands after the click starts
        # itself (hasPlayed is set), so extra calls are the self-healing path
        # working, not a bug. What matters is that no cell is left behind.
        page.wait_for_function("window.__playCalls >= 8", timeout=15_000)
        assert page.locator('.cell[data-empty="true"]').count() == 0
        browser.close()
