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
        # Specifically cell 0, which is the one the menu action below targets.
        # "some player is playing" would pass with cell 0 never having started,
        # and then the assertion at the end would be vacuously true.
        broadcast.wait_for_function(
            "window.__players[0] && window.__players[0].getPlayerState() === 1", timeout=10_000
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
    """Play and unmute both survive the gesture moving to another document.

    Launched with NO `--autoplay-policy=no-user-gesture-required`, unlike the
    menu test above -- that is the point. The buttons an operator actually
    presses now live on /layout-control, and BroadcastChannel confers no user
    activation on the receiving document, so /layout starts and unmutes eight
    players having never been clicked. This is the test that says that works;
    see CLAUDE.md gotcha 41 for why it is allowed to.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)

        control.goto(f"{running_server}/layout-control", wait_until="load")
        # The mute button already reads "Unmute" in the page's static HTML, so
        # waiting on its text proves nothing. The status line starting out as
        # this page's own placeholder and then changing IS proof a snapshot
        # arrived -- only renderFromSnapshot ever replaces it.
        control.wait_for_function(
            "document.getElementById('status').textContent !== 'waiting for /layout to connect…'",
            timeout=10_000,
        )

        control.click("#play")
        broadcast.wait_for_function(
            "window.__players.every(p => p && p.getPlayerState() === 1)", timeout=20_000
        )

        control.click("#mute")
        broadcast.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=10_000)
        # Still playing, specifically. A browser that refused the unmute would
        # not necessarily report muted -- it could pause the media instead, and
        # a wall that goes silent-and-frozen is the failure worth naming.
        broadcast.wait_for_function(
            "window.__players.every(p => p && p.getPlayerState() === 1)", timeout=10_000
        )
        control.wait_for_function(
            "document.getElementById('mute').textContent.trim() === 'Mute'", timeout=10_000
        )
        browser.close()


def test_save_and_restore_zoom_and_video_sets(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)
        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_selector('.cell[data-empty="false"]', timeout=20_000)

        def broadcast_zoom_ratio(nth=0):
            return broadcast.evaluate(
                f"""() => {{
                    const cells = document.querySelectorAll('.cell');
                    const f = cells[{nth}].querySelector('iframe').getBoundingClientRect();
                    const c = cells[{nth}].getBoundingClientRect();
                    return f.width / c.width;
                }}"""
            )

        # --- zoom/pan set ---------------------------------------------------
        box = control.locator(".cell").first.bounding_box()
        control.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(8):
            control.mouse.wheel(0, -120)
        zoomed = broadcast_zoom_ratio()
        assert zoomed > 1.1, "the wheel above should have zoomed cell 0 in"

        control.fill("#zoom-set-name", "wide shot")
        control.click("#zoom-set-save")
        control.wait_for_function(
            "[...document.getElementById('zoom-set-select').options].some(o => o.value === 'wide shot')",
            timeout=10_000,
        )

        control.click("#reset-view")
        broadcast.wait_for_function(
            f"() => {{"
            f"  const cells = document.querySelectorAll('.cell');"
            f"  const f = cells[0].querySelector('iframe').getBoundingClientRect();"
            f"  const c = cells[0].getBoundingClientRect();"
            f"  return f.width / c.width < {zoomed - 0.05};"
            f"}}",
            timeout=10_000,
        )

        control.select_option("#zoom-set-select", "wide shot")
        control.click("#zoom-set-restore")
        broadcast.wait_for_function(
            f"() => {{"
            f"  const cells = document.querySelectorAll('.cell');"
            f"  const f = cells[0].querySelector('iframe').getBoundingClientRect();"
            f"  const c = cells[0].getBoundingClientRect();"
            f"  return f.width / c.width > {zoomed - 0.05};"
            f"}}",
            timeout=10_000,
        )

        # --- video set -------------------------------------------------------
        before_ids = broadcast.evaluate(
            "[...document.querySelectorAll('.cell')].map(c => c.dataset.videoId)"
        )
        # The control page is what actually renders a title (/layout has no
        # .label at all -- it draws real iframes, not plain rectangles), so
        # this is the only place a lost title would be observable.
        before_titles = control.evaluate(
            "[...document.querySelectorAll('.cell')].map(c => c.querySelector('.label').textContent)"
        )
        assert all(before_titles), "fixture videos must all have a resolvable title to start"
        control.fill("#video-set-name", "finale")
        control.click("#video-set-save")
        control.wait_for_function(
            "[...document.getElementById('video-set-select').options].some(o => o.value === 'finale')",
            timeout=10_000,
        )

        control.click("#shuffle")
        broadcast.wait_for_function("window.__prerolled === true", timeout=20_000)

        control.select_option("#video-set-select", "finale")
        control.click("#video-set-restore")
        broadcast.wait_for_function(
            "ids => JSON.stringify([...document.querySelectorAll('.cell')].map(c => c.dataset.videoId)) === JSON.stringify(ids)",
            arg=before_ids,
            timeout=15_000,
        )
        # Titles must come back too, not just ids -- a restored set has no
        # search response to pull titles from, so this is what proves
        # slotStateToVideoSet's captured titles actually round-tripped
        # through the saved set rather than silently falling back to
        # showing each cell's raw video id.
        control.wait_for_function(
            "titles => JSON.stringify([...document.querySelectorAll('.cell')]"
            ".map(c => c.querySelector('.label').textContent)) === JSON.stringify(titles)",
            arg=before_titles,
            timeout=10_000,
        )
        browser.close()


def test_a_restored_video_set_survives_a_simulated_reconnect(running_server):
    """resync() is what a real WebSocket reconnect calls (socket.js's
    onReconnect) -- window.__resync lets this test trigger exactly that
    without tearing down and re-establishing a real socket.

    The wall must diverge from its own plain, deterministic default before
    the assertion that matters, or this test cannot tell a sticky resync
    apart from a non-sticky one: nothing here ever presses "New query", so
    `loadQuery()` is empty throughout, and a resync with Task 6's sticky
    branch deleted would fall through straight to fetching /api/videos with
    no stored query -- landing right back on the same deterministic
    cache-ranked order the very first page load already produced
    (`default_ids` below). If "finale" happened to hold that same default
    order, a non-sticky resync would reproduce it by accident and the test
    would pass either way.

    So "finale" is saved from a SHUFFLED state instead -- #shuffle reorders
    (and can swap in reserves for) the already-fetched pool client-side, with
    no server round trip at all, so it changes what is on screen without
    changing what a later plain /api/videos fetch would return. That gives
    two provably different candidates for "what resync produces": the
    shuffled-then-saved `finale_ids`, and the untouched `default_ids`. A
    sticky resync reapplies the restored `finale_ids` from localStorage
    unchanged; a non-sticky one re-derives `default_ids` from the server.
    Asserting the post-resync ids equal `finale_ids` -- and explicitly do NOT
    equal `default_ids` -- is a real proof, not a coincidence.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)
        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_selector('.cell[data-empty="false"]', timeout=20_000)

        def video_ids():
            return broadcast.evaluate(
                "[...document.querySelectorAll('.cell')].map(c => c.dataset.videoId)"
            )

        default_ids = video_ids()

        control.click("#shuffle")
        broadcast.wait_for_function("window.__prerolled === true", timeout=20_000)
        finale_ids = video_ids()
        assert finale_ids != default_ids, (
            "the shuffle above should have changed the on-screen set -- "
            "otherwise 'finale' would be indistinguishable from the "
            "deterministic default and this test could not tell a sticky "
            "resync from a non-sticky one"
        )

        control.fill("#video-set-name", "finale")
        control.click("#video-set-save")
        control.wait_for_function(
            "[...document.getElementById('video-set-select').options].some(o => o.value === 'finale')",
            timeout=10_000,
        )

        # Diverge the live wall again so restoring "finale" below is a real
        # change, not a no-op that would prove nothing either way.
        control.click("#shuffle")
        broadcast.wait_for_function("window.__prerolled === true", timeout=20_000)

        control.select_option("#video-set-select", "finale")
        control.click("#video-set-restore")
        broadcast.wait_for_function(
            "ids => JSON.stringify([...document.querySelectorAll('.cell')].map(c => c.dataset.videoId)) === JSON.stringify(ids)",
            arg=finale_ids,
            timeout=15_000,
        )
        restored_ids = video_ids()
        assert restored_ids == finale_ids
        assert restored_ids != default_ids

        broadcast.evaluate("window.__resync()")

        after_reconnect_ids = video_ids()
        # The property that actually distinguishes sticky from non-sticky: a
        # non-sticky resync would ignore the restored "finale" state and
        # re-fetch /api/videos with no stored query, landing back on
        # default_ids -- which was just proven to differ from restored_ids.
        assert after_reconnect_ids == restored_ids
        assert after_reconnect_ids != default_ids
        browser.close()


def test_global_layout_shift_from_the_control_page(running_server):
    """Nudge and Set both round-trip through PUT /api/config (shared,
    persistent -- unlike zoom/pan or video sets, this must survive a reload
    for anyone else watching), and the resulting shift lands as a CSS
    transform on /layout's own #grid container, not on any individual cell.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)
        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_selector('.cell[data-empty="false"]', timeout=20_000)

        def grid_transform():
            return broadcast.evaluate("document.getElementById('grid').style.transform")

        assert grid_transform() == "translate(0px, 0px)"
        control.wait_for_function(
            "document.getElementById('shift-readout').textContent === '0, 0'", timeout=10_000
        )

        control.click("#shift-right")
        broadcast.wait_for_function(
            "document.getElementById('grid').style.transform === 'translate(10px, 0px)'",
            timeout=10_000,
        )
        control.wait_for_function(
            "document.getElementById('shift-readout').textContent === '10, 0'", timeout=10_000
        )

        control.click("#shift-down")
        broadcast.wait_for_function(
            "document.getElementById('grid').style.transform === 'translate(10px, 10px)'",
            timeout=10_000,
        )

        control.fill("#shift-x", "-25")
        control.fill("#shift-y", "40")
        control.click("#shift-set")
        broadcast.wait_for_function(
            "document.getElementById('grid').style.transform === 'translate(-25px, 40px)'",
            timeout=10_000,
        )
        control.wait_for_function(
            "document.getElementById('shift-readout').textContent === '-25, 40'", timeout=10_000
        )

        control.click("#shift-reset")
        broadcast.wait_for_function(
            "document.getElementById('grid').style.transform === 'translate(0px, 0px)'",
            timeout=10_000,
        )
        browser.close()


def test_per_screen_shift_and_scale_from_the_control_page(running_server):
    """Nudging one screen's shift/scale lands on that screen's own cells as a
    per-cell transform + clip-path -- and, critically, must NOT touch a
    sibling screen's cells at all (that isolation, not the exact pixel math
    already covered by static/layout-fit.test.mjs, is what a browser test is
    for here). screens.json lists screens A, B, C, D, E, F in that order, so
    under the default layout config screen A's one cell is at index 0 and
    screen F's three cells are at indices 5-7 -- see layout-fit.test.mjs's
    "produces 8 cells across the six real screens".
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(ignore_https_errors=True)
        broadcast = context.new_page()
        control = context.new_page()

        broadcast.goto(f"{running_server}/layout", wait_until="load")
        broadcast.wait_for_function("window.__prerolled === true", timeout=40_000)
        control.goto(f"{running_server}/layout-control", wait_until="load")
        control.wait_for_selector('.cell[data-empty="false"]', timeout=20_000)
        control.wait_for_selector('.screen-group[data-screen-id="F"]', timeout=10_000)

        def cell_style(page, index, prop):
            return page.evaluate(f"document.querySelectorAll('.cell')[{index}].style.{prop}")

        def readout(screen_id):
            return control.locator(
                f'.screen-group[data-screen-id="{screen_id}"] [data-readout]'
            ).text_content()

        def readout_selector(screen_id):
            return f'.screen-group[data-screen-id="{screen_id}"] [data-readout]'

        # Each cell on the control page is labelled with its own screen's
        # letter -- index 0 is screen A's only cell, index 5 the first of
        # screen F's three.
        assert (
            control.evaluate(
                "document.querySelectorAll('.cell')[0].querySelector('.screen-letter').textContent"
            )
            == "A"
        )
        assert (
            control.evaluate(
                "document.querySelectorAll('.cell')[5].querySelector('.screen-letter').textContent"
            )
            == "F"
        )

        # Both pages agree: nothing shifted or scaled yet, on either screen.
        assert cell_style(broadcast, 5, "transform") == "none"
        assert cell_style(broadcast, 0, "transform") == "none"
        assert cell_style(control, 5, "transform") == "none"
        assert readout("F") == "0,0 100%,100%"
        assert readout("A") == "0,0 100%,100%"

        screen_f = control.locator('.screen-group[data-screen-id="F"]')
        screen_f.get_by_title("Nudge screen F right").click()
        control.wait_for_function(
            f"document.querySelector('{readout_selector('F')}').textContent === '10,0 100%,100%'",
            timeout=10_000,
        )
        broadcast.wait_for_function(
            "document.querySelectorAll('.cell')[5].style.transform !== 'none'", timeout=10_000
        )
        control.wait_for_function(
            "document.querySelectorAll('.cell')[5].style.transform !== 'none'", timeout=10_000
        )
        # Screen A, untouched, must show no effect at all on either page.
        assert cell_style(broadcast, 0, "transform") == "none"
        assert cell_style(control, 0, "transform") == "none"
        assert readout("A") == "0,0 100%,100%"

        screen_f.get_by_title("Stretch screen F wider").click()
        # Waiting on the readout itself, not on clipPath going non-"none": a
        # plain shift on a cell that is only ONE quadrant of its screen (F
        # tiles 2x2) already produces a non-"none" clip-path -- "inset(0% 0%
        # 0% 0%)" is a real, distinct string from "none" even when every
        # inset is zero -- so that condition was already true from the
        # previous step and would race ahead of this click's own PUT.
        control.wait_for_function(
            "document.querySelector('.screen-group[data-screen-id=\"F\"] [data-readout]')"
            ".textContent === '10,0 102%,100%'",
            timeout=10_000,
        )
        broadcast.wait_for_function(
            "document.querySelectorAll('.cell')[5].style.transform.includes('scale(1.02')",
            timeout=10_000,
        )
        assert cell_style(broadcast, 0, "clipPath") == "none"
        assert readout("A") == "0,0 100%,100%"

        screen_f.get_by_title("Reset screen F's shift and scale").click()
        control.wait_for_function(
            f"document.querySelector('{readout_selector('F')}').textContent === '0,0 100%,100%'",
            timeout=10_000,
        )
        broadcast.wait_for_function(
            "document.querySelectorAll('.cell')[5].style.transform === 'none' && "
            "document.querySelectorAll('.cell')[5].style.clipPath === 'none'",
            timeout=10_000,
        )
        browser.close()
