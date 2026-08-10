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


def test_pause_stops_every_player(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "window.__players?.every(p => typeof p?.pauseVideo === 'function')", timeout=25_000
        )

        page.evaluate("""
            window.__pauseCalls = 0;
            for (const player of window.__players) {
                const original = player.pauseVideo.bind(player);
                player.pauseVideo = () => { window.__pauseCalls += 1; return original(); };
            }
        """)
        page.click("#play")
        page.click("#pause")
        page.wait_for_function("window.__pauseCalls >= 8", timeout=15_000)
        browser.close()


def test_the_wall_starts_muted_and_the_button_unmutes_every_player(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "window.__players?.every(p => typeof p?.isMuted === 'function')", timeout=25_000
        )

        assert page.locator("#mute").text_content().strip() == "Unmute all"
        assert page.evaluate("window.__players.every(p => p.isMuted())"), "should start muted"

        page.click("#play")
        page.click("#mute")
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=15_000)
        assert page.locator("#mute").text_content().strip() == "Mute all"

        page.click("#mute")
        page.wait_for_function("window.__players.every(p => p.isMuted())", timeout=15_000)
        assert page.locator("#mute").text_content().strip() == "Unmute all"
        browser.close()


def test_player_chrome_is_suppressed_as_far_as_the_api_allows(running_server):
    """Assert the parameters that still work, and the hover block.

    modestbranding and showinfo are deprecated and ignored by YouTube, so
    asserting them would test nothing. iv_load_policy, cc_load_policy,
    controls, disablekb and fs are still honoured.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        sources = page.eval_on_selector_all(".cell iframe", "els => els.map(e => e.src)")
        assert len(sources) == 8
        for src in sources:
            assert "controls=0" in src, src
            assert "iv_load_policy=3" in src, src
            assert "cc_load_policy=0" in src, src
            assert "disablekb=1" in src, src
            assert "rel=0" in src, src

        # The title bar and channel avatar appear on mouse-over and no player
        # parameter suppresses them. Blocking pointer events is what does.
        assert page.eval_on_selector_all(
            ".cell iframe",
            "els => els.every(e => getComputedStyle(e).pointerEvents === 'none')",
        )
        browser.close()


def test_a_detected_content_box_zooms_further_than_plain_cover_fit(running_server):
    """Where black bars are detected, the crop must push past them.

    Stated as an invariant rather than against one hand-picked video: whether
    any particular video is pillarboxed is not this code's business, and
    pinning a specific id would make the test rot when that video changes.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )
        page.wait_for_timeout(4000)  # let the content-box fetches land

        report = page.evaluate("""
            [...document.querySelectorAll('.cell')].map(cell => {
                const f = cell.querySelector('iframe');
                const c = cell.getBoundingClientRect();
                const plainWidth = Math.max(c.width, (16 * c.height) / 9);
                return {
                    id: cell.dataset.videoId,
                    width: f.getBoundingClientRect().width,
                    plainWidth,
                    cellW: c.width,
                    cellH: c.height,
                };
            })
        """)

        import httpx as _httpx

        checked = 0
        for cell in report:
            box = _httpx.get(
                f"{running_server}api/content-box/{cell['id']}", verify=False, timeout=20
            ).json()
            cropped = box["w"] < 0.98 or box["h"] < 0.98
            if not cropped:
                # No bars detected: geometry must equal plain cover-fit.
                assert cell["width"] == pytest.approx(cell["plainWidth"], rel=0.02), cell
                continue
            checked += 1
            assert cell["width"] > cell["plainWidth"] * 1.001, (
                f"bars detected ({box}) but no extra zoom applied: {cell}"
            )
            # And the content region must still cover the cell.
            assert box["w"] * cell["width"] >= cell["cellW"] - 1, cell

        print(f"\n  {checked}/8 cells had detectable bars and were zoomed past them")
        browser.close()


def test_every_iframe_covers_its_cell_with_no_letterboxing(running_server):
    """The iframe must fill the cell in both axes, cropped and centred.

    A YouTube iframe is internally 16:9 and letterboxes itself in any other
    shape, so this only holds if the iframe is deliberately oversized past the
    cell bounds and clipped by overflow:hidden.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            ignore_https_errors=True, viewport={"width": 1400, "height": 700}
        ).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )
        # Cells are 4x2 across a 1400x700 viewport, so they are nowhere near
        # 16:9 -- exactly the case that letterboxes without a cover fit.
        page.wait_for_function(
            """[...document.querySelectorAll('.cell')].every(cell => {
                 const f = cell.querySelector('iframe');
                 if (!f) return false;
                 const c = cell.getBoundingClientRect();
                 const r = f.getBoundingClientRect();
                 return r.width >= c.width - 1 && r.height >= c.height - 1;
               })""",
            timeout=20_000,
        )

        measurements = page.evaluate("""
            [...document.querySelectorAll('.cell')].map(cell => {
                const c = cell.getBoundingClientRect();
                const r = cell.querySelector('iframe').getBoundingClientRect();
                return {
                    coversWidth:  r.width  >= c.width  - 1,
                    coversHeight: r.height >= c.height - 1,
                    aspect: r.width / r.height,
                    centredX: Math.abs((r.left + r.right) / 2 - (c.left + c.right) / 2),
                    centredY: Math.abs((r.top + r.bottom) / 2 - (c.top + c.bottom) / 2),
                };
            })
        """)
        assert len(measurements) == 8
        for i, m in enumerate(measurements):
            assert m["coversWidth"], f"cell {i} letterboxes horizontally"
            assert m["coversHeight"], f"cell {i} letterboxes vertically"
            assert abs(m["aspect"] - 16 / 9) < 0.02, f"cell {i} distorts: {m['aspect']}"
            assert m["centredX"] < 1.5, f"cell {i} crop off-centre by {m['centredX']}px"
            assert m["centredY"] < 1.5, f"cell {i} crop off-centre by {m['centredY']}px"
        browser.close()
