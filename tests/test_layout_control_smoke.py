"""Browser smoke test for /layout-control: an interaction on the control
page must land on the real broadcast page, over BroadcastChannel.

Marked `browser`, excluded from the default suite. Both pages come from one
Playwright BrowserContext -- BroadcastChannel only connects tabs in the same
browser profile, which is exactly the constraint this feature is built
around (the NDI broadcaster already only ever captures a local window).
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
        yield f"https://localhost:{port}"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_a_wheel_on_the_control_page_zooms_the_real_cell(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )
        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_selector(".cell", timeout=20_000)

        def broadcast_zoom(nth=0):
            return broadcast.evaluate(
                f"""() => {{
                    const cells = document.querySelectorAll('.cell');
                    const f = cells[{nth}].querySelector('iframe').getBoundingClientRect();
                    const c = cells[{nth}].getBoundingClientRect();
                    return f.width / c.width;
                }}"""
            )

        before = broadcast_zoom()

        box = control.locator(".cell").first.bounding_box()
        control.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(8):
            control.mouse.wheel(0, -120)

        broadcast.wait_for_function(
            f"""() => {{
                const cells = document.querySelectorAll('.cell');
                const f = cells[0].querySelector('iframe').getBoundingClientRect();
                const c = cells[0].getBoundingClientRect();
                return f.width / c.width > {before + 0.2};
            }}""",
            timeout=10_000,
        )
        browser.close()


def test_a_menu_action_on_the_control_page_pauses_the_real_player(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)
        broadcast.evaluate("window.__players.forEach(p => p && p.playVideo())")
        broadcast.wait_for_function(
            "window.__players.some(p => p && p.getPlayerState() === 1)", timeout=10_000
        )

        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_selector('.cell[data-empty="false"]', timeout=20_000)

        control.locator(".cell").first.click(button="right")
        control.wait_for_selector("#menu:not([hidden])", timeout=5_000)
        control.locator("#menu button", has_text="Pause this cell").first.click()

        broadcast.wait_for_function(
            "window.__players[0] && window.__players[0].getPlayerState() !== 1", timeout=10_000
        )
        browser.close()


def test_the_control_page_reflects_mute_state_from_the_broadcast_page(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)

        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_function(
            "document.getElementById('mute').textContent.trim() === 'Unmute'", timeout=10_000
        )

        control.click("#mute")
        broadcast.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=10_000)
        control.wait_for_function(
            "document.getElementById('mute').textContent.trim() === 'Mute'", timeout=10_000
        )
        browser.close()
