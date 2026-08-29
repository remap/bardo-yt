# tests/test_layout_smoke.py
"""Browser smoke test for /layout: the wall must build across the real
screens, the same class of check test_player_smoke.py does for /.

Marked `browser`, excluded from the default suite -- launches Chromium, no
quota spent (pre-seeded cache, same technique as test_player_smoke.py).
"""

from __future__ import annotations

import asyncio
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
from ytmatrix.store import FileStore

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.browser

# No `layout` key: this exercises the "never configured" fallback in both
# ytmatrix/config.py (Config.layout is None) and static/layout-page.js
# (DEFAULT_LAYOUT_CONFIG). With the real six screens in static/layout/screens.json,
# total=8/max_per_screen=3 resolves to F=3, and B/C/D/A/E=1 each -- verified in
# static/layout-fit.test.mjs's "real six-screen default" test.
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
    "query_generation": {"enabled": True},
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def _fresh_dist():
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "build-dist.sh")],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def running_server(tmp_path, _fresh_dist):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(CONFIG))

    cache_dir = tmp_path / "cache"
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
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
    asyncio.run(
        cache.write(
            FileStore(cache_dir),
            params,
            [{"video_id": v, "title": v, "channel": "c"} for v in ids],
        )
    )

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
        yield f"https://localhost:{port}/layout"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_the_layout_wall_builds_one_player_per_resolved_cell(running_server):
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(running_server, wait_until="load")

        page.wait_for_selector(".cell iframe", timeout=20_000)
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        assert page.locator(".cell").count() == 8
        assert page.locator('.cell[data-empty="true"]').count() == 0
        browser.close()

    assert errors == [], f"page errors: {errors}"


def test_cells_are_positioned_within_the_canvas_bounds(running_server):
    """Every cell must land inside the visible grid area -- proof the percentage
    math (Task 4/7) is wired correctly, not just that 8 iframes exist somewhere."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        bounds = page.evaluate(
            """() => {
                const grid = document.getElementById('grid').getBoundingClientRect();
                return [...document.querySelectorAll('.cell')].map(cell => {
                    const c = cell.getBoundingClientRect();
                    return c.left >= grid.left - 1 && c.top >= grid.top - 1
                        && c.right <= grid.right + 1 && c.bottom <= grid.bottom + 1
                        && c.width > 0 && c.height > 0;
                });
            }"""
        )
        assert len(bounds) == 8
        assert all(bounds), f"a cell fell outside the grid: {bounds}"
        browser.close()


def test_pre_roll_and_mute_work_the_same_as_the_grid_page(running_server):
    """The shared engine's behavior, exercised once here as a spot check --
    the exhaustive coverage lives in test_player_smoke.py against /."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        assert page.evaluate("window.__players.every(p => p.isMuted())"), "should start muted"
        page.click("#play")
        page.click("#mute")
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=15_000)
        browser.close()
