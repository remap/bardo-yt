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
    # Enabled so the New query button is visible: player.js hides it when
    # generation is off, and the model default is off. It only un-hides the
    # button -- nothing here generates unless a test clicks it, and the tests
    # that do intercept the request rather than letting it reach Gemini.
    "query_generation": {"enabled": True},
}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def _fresh_dist():
    """Rebuild dist/ before any browser test runs.

    The server serves dist/, not static/ -- the Worker's asset binding wants an
    assembled bundle, and main.py mirrors that locally. dist/ is a copy, so
    editing static/player.js changes nothing the browser sees until something
    rebuilds it, and this suite would go on passing against the previous copy.
    That is precisely the failure gotcha 14 exists to catch, so the suite
    rebuilds rather than trusting whatever a previous command happened to leave
    behind. The script is a handful of `cp`s -- once per session costs nothing.
    """
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "build-dist.sh")],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def running_server(tmp_path, _fresh_dist):
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
    # cache.write is async and goes through a Store, not a directory -- the
    # container has no durable disk. asyncio.run because this fixture is sync:
    # calling it without awaiting builds a coroutine, seeds nothing, and every
    # test in this file then sits waiting on a live search that never comes.
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
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
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

        # Wrap only AFTER pre-roll: pre-roll pauses every player itself, so
        # counting from before it would let this test pass even if the button
        # did nothing at all.
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
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


def test_a_posted_intent_reaches_the_wall_over_websocket(running_server):
    """POST /api/intent is the second front door into applyIntent() -- for a
    controller with no browser to share a BroadcastChannel with (e.g. chasa,
    an OSC router). This proves the whole relay: HTTP POST -> server
    broadcast -> this page's own /ws connection -> applyIntent() -> a real
    effect on the players, with no button ever clicked.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "window.__players?.every(p => typeof p?.pauseVideo === 'function')", timeout=25_000
        )
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.evaluate("""
            window.__pauseCalls = 0;
            for (const player of window.__players) {
                const original = player.pauseVideo.bind(player);
                player.pauseVideo = () => { window.__pauseCalls += 1; return original(); };
            }
        """)
        page.click("#play")

        response = httpx.post(
            f"{running_server}api/intent", json={"type": "pause"}, verify=False, timeout=5.0
        )
        assert response.status_code == 200

        page.wait_for_function("window.__pauseCalls >= 8", timeout=15_000)
        browser.close()


def test_a_new_set_stays_paused_until_pre_rolled(running_server):
    """Nothing starts until every cell has buffered, and not even then."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")

        # Play is unavailable while the set is still pre-rolling, so there is
        # no window in which pressing it starts only some of the cells.
        page.wait_for_selector(".cell iframe", timeout=20_000)
        assert page.locator("#play").is_disabled() or page.evaluate("window.__prerolled === true")

        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        assert page.locator("#play").is_enabled()

        # Pre-rolled, but deliberately not playing: the wall waits for a human.
        assert page.evaluate("window.__wantPlaying") is False
        states = page.evaluate("window.__players.map(p => p.getPlayerState())")
        assert all(state != 1 for state in states), f"something started on its own: {states}"

        # And every player is parked at the beginning, not mid-buffer.
        times = page.evaluate("window.__players.map(p => p.getCurrentTime())")
        assert all(t < 3 for t in times), f"players were not rewound after pre-roll: {times}"
        browser.close()


def test_the_wall_starts_muted_and_the_button_unmutes_every_player(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "window.__players?.every(p => typeof p?.isMuted === 'function')", timeout=25_000
        )

        assert page.locator("#mute").text_content().strip() == "Unmute"
        assert page.evaluate("window.__players.every(p => p.isMuted())"), "should start muted"

        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.click("#mute")
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=15_000)
        assert page.locator("#mute").text_content().strip() == "Mute"

        page.click("#mute")
        page.wait_for_function("window.__players.every(p => p.isMuted())", timeout=15_000)
        assert page.locator("#mute").text_content().strip() == "Unmute"
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


def test_right_click_offers_copy_url_at_time(running_server):
    """The iframe has pointer-events:none, which is what frees the cell to
    receive a right-click at all. If that CSS ever goes, so does this menu."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(
            ignore_https_errors=True, permissions=["clipboard-read", "clipboard-write"]
        )
        page = context.new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        page.click("#play")
        page.wait_for_timeout(3500)  # let a few seconds of video elapse

        page.locator(".cell").first.click(button="right")
        page.wait_for_selector("#menu:not([hidden])", timeout=5_000)

        labels = page.eval_on_selector_all(
            "#menu button span:first-child", "e => e.map(x => x.textContent)"
        )
        assert "Copy video URL at time" in labels
        assert "Copy video URL" in labels
        assert "Open on YouTube at time" in labels
        assert any(label.endswith("this cell") for label in labels)

        page.locator("#menu button", has_text="Copy video URL at time").first.click()
        # state="hidden": the default waits for visibility, which a hidden
        # element can never satisfy.
        page.wait_for_selector("#menu", state="hidden", timeout=5_000)

        copied = page.evaluate("navigator.clipboard.readText()")
        assert copied.startswith("https://youtu.be/"), copied
        assert "?t=" in copied, f"timestamp missing: {copied}"
        seconds = int(copied.split("?t=")[1])
        assert seconds >= 1, f"expected a real playback position, got {seconds}"
        browser.close()


def test_right_click_on_an_empty_cell_does_nothing(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        page.evaluate("""
            const cell = document.querySelector('.cell');
            cell.dataset.empty = 'true';
            delete cell.dataset.videoId;
        """)
        page.locator(".cell").first.click(button="right")
        page.wait_for_timeout(500)
        assert page.locator("#menu").is_hidden()
        browser.close()


def test_the_new_query_button_carries_the_prompt(running_server):
    """The button is the only trigger, and it sends whatever is in the box."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        sent = {}
        page.route(
            "**/api/new-query",
            lambda route: (
                sent.update({"body": route.request.post_data}),
                route.fulfill(status=409, json={"detail": "stubbed"}),
            )[-1],
        )
        page.fill("#prompt", "sadder, more piano")
        page.click("#new-query")
        page.wait_for_timeout(1200)

        assert sent, "the button did not trigger a request"
        assert '"prompt"' in sent["body"]
        assert "sadder, more piano" in sent["body"]
        browser.close()


def test_enter_in_the_prompt_box_presses_the_query_button(running_server):
    """Enter is how you finish typing in a text box, so it sends what you
    typed -- exactly as if the button had been clicked, steer and all.

    It is literally a .click() rather than a second call into
    requestNewQuery: the button owns the disabled state, the empty-box
    meaning and the 100-unit spend, and a parallel path would eventually
    disagree with it about one of them.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        sent = {}
        page.route(
            "**/api/new-query",
            lambda route: (sent.update(body=route.request.post_data or ""), route.abort())[-1],
        )
        page.fill("#prompt", "sadder, more piano")
        page.press("#prompt", "Enter")
        page.wait_for_timeout(1200)

        assert sent, "Enter did not trigger a request"
        assert "sadder, more piano" in sent["body"], "the steer must survive the keystroke"
        browser.close()


def test_enter_while_a_generation_is_running_does_not_start_a_second(running_server):
    """The button disables itself for the duration of a search. Enter goes
    through that same button, so it inherits the guard -- otherwise holding
    the key down would spend 100 units per repeat."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        calls = []
        # Never answer, so the first generation stays in flight.
        page.route("**/api/new-query", lambda route: calls.append(1))
        page.fill("#prompt", "sadder, more piano")
        page.press("#prompt", "Enter")
        page.wait_for_timeout(600)
        page.press("#prompt", "Enter")
        page.press("#prompt", "Enter")
        page.wait_for_timeout(600)

        assert len(calls) == 1, f"one generation, not {len(calls)}"
        browser.close()


def test_an_empty_box_generates_from_the_theme_alone(running_server):
    """An empty box is not an error -- it means "no steer", and the server
    records that as `generated` rather than `manual`. Whitespace is normalised
    away rather than sent as a prompt of spaces."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        sent = {}
        page.route(
            "**/api/new-query",
            lambda route: (
                sent.update({"body": route.request.post_data}),
                route.fulfill(status=409, json={"detail": "stubbed"}),
            )[-1],
        )
        page.fill("#prompt", "   ")
        page.click("#new-query")
        page.wait_for_timeout(1200)

        assert sent, "an empty box should still generate from the theme"
        assert '"prompt"' not in sent["body"], "whitespace must not travel as a prompt"
        browser.close()


def test_the_grid_is_hidden_while_pre_rolling(running_server):
    """Every cell must play briefly to buffer; none of that should be visible.

    Reported as "playstate control is unclean": on a new query the wall showed
    eight videos starting and then stopping again.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")

        page.wait_for_selector(".cell", timeout=20_000)
        # Still pre-rolling: the grid must not be showing its flicker.
        if not page.evaluate("window.__prerolled === true"):
            assert page.evaluate("document.getElementById('grid').dataset.preroll") == "true"
            assert page.evaluate("getComputedStyle(document.getElementById('grid')).opacity") == "0"

        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        assert page.evaluate("document.getElementById('grid').dataset.preroll") == "false"
        browser.close()


def test_no_cell_is_left_running_after_pre_roll(running_server):
    """Each player is paused as soon as it individually buffers, so no cell
    sits playing while the slowest one catches up."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        # State alone is not proof: a cell can report BUFFERING (3) and start
        # playing a moment later. Advancing playback time is the ground truth.
        first = page.evaluate("window.__players.map(p => p.getCurrentTime())")
        page.wait_for_timeout(2500)
        second = page.evaluate("window.__players.map(p => p.getCurrentTime())")

        advanced = [(i, a, b) for i, (a, b) in enumerate(zip(first, second)) if b - a > 0.5]
        assert advanced == [], f"cells kept playing after pre-roll: {advanced}"

        states = page.evaluate("window.__players.map(p => p.getPlayerState())")
        assert all(s != 1 for s in states), f"a cell reports PLAYING: {states}"
        assert all(t < 3 for t in second), f"players drifted instead of parking: {second}"
        browser.close()


def test_hover_to_unmute_makes_exactly_one_cell_audible(running_server):
    """Point at a cell and only that cell has sound.

    Works for the same reason the context menu does: the iframe has
    pointer-events:none, so the cell receives the pointer, not YouTube.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        page.check("#hover-unmute")
        # Nothing hovered yet: the global mute state still rules.
        assert page.evaluate("window.__players.every(p => p.isMuted())")

        page.locator(".cell").nth(3).hover()
        page.wait_for_function(
            "window.__players.filter(p => !p.isMuted()).length === 1", timeout=10_000
        )
        muted_flags = page.evaluate("window.__players.map(p => p.isMuted())")
        assert muted_flags[3] is False, muted_flags
        assert sum(1 for m in muted_flags if not m) == 1

        # Move to a different cell: the audible one moves with the cursor.
        page.locator(".cell").nth(6).hover()
        page.wait_for_function("window.__players[6].isMuted() === false", timeout=10_000)
        assert page.evaluate("window.__players[3].isMuted()") is True
        assert page.locator('.cell[data-audible="true"]').count() == 1

        # Leaving the grid restores the global state -- nothing stays audible.
        page.locator("#status").hover()
        page.wait_for_function("window.__players.every(p => p.isMuted())", timeout=10_000)
        assert page.locator('.cell[data-audible="true"]').count() == 0
        browser.close()


def test_turning_hover_unmute_off_restores_the_global_state(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        page.click("#mute")  # unmute everything globally
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=10_000)

        page.check("#hover-unmute")
        page.locator(".cell").nth(2).hover()
        page.wait_for_function(
            "window.__players.filter(p => !p.isMuted()).length === 1", timeout=10_000
        )

        # Unchecking must not strand seven cells muted.
        page.uncheck("#hover-unmute")
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=10_000)
        browser.close()


def test_scroll_wheel_zooms_toward_the_pointer(running_server):
    """The pixel under the cursor must stay under it while zooming."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        cell = page.locator(".cell").first
        box = cell.bounding_box()
        # A point well off centre, so anchoring is actually exercised.
        px = box["x"] + box["width"] * 0.2
        py = box["y"] + box["height"] * 0.75

        def anchored_pixel():
            return page.evaluate(
                """([px, py]) => {
                    const cell = document.querySelector('.cell');
                    const b = cell.getBoundingClientRect();
                    const f = cell.querySelector('iframe').getBoundingClientRect();
                    return [(px - f.left) / f.width, (py - f.top) / f.height];
                }""",
                [px, py],
            )

        before = anchored_pixel()
        page.mouse.move(px, py)
        for _ in range(8):
            page.mouse.wheel(0, -120)
        page.wait_for_timeout(400)

        after = anchored_pixel()
        assert abs(after[0] - before[0]) < 0.02, f"drifted horizontally: {before} -> {after}"
        assert abs(after[1] - before[1]) < 0.02, f"drifted vertically: {before} -> {after}"

        # And it really zoomed.
        sizes = page.evaluate(
            """() => {
                const cell = document.querySelector('.cell');
                const b = cell.getBoundingClientRect();
                const f = cell.querySelector('iframe').getBoundingClientRect();
                return [f.width / b.width, b.width, f.width];
            }"""
        )
        assert sizes[0] > 1.5, f"expected real magnification, got {sizes}"
        browser.close()


def test_zooming_out_pulls_back_to_the_whole_frame(running_server):
    """Zooming out past cover is intended -- it shows the video entire."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        box = page.locator(".cell").first.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        def measure():
            return page.evaluate(
                """() => {
                    const cell = document.querySelector('.cell');
                    const b = cell.getBoundingClientRect();
                    const f = cell.querySelector('iframe').getBoundingClientRect();
                    return {
                        ratio: f.width / b.width,
                        inside: f.left >= b.left - 1 && f.right <= b.right + 1
                             && f.top >= b.top - 1 && f.bottom <= b.bottom + 1,
                        centredX: Math.abs((f.left + f.right) / 2 - (b.left + b.right) / 2),
                    };
                }"""
            )

        before_ratio = measure()["ratio"]
        assert before_ratio >= 1.0, "should start at cover"
        assert not measure()["inside"], "at cover the frame must overflow the cell"

        for _ in range(25):
            page.mouse.wheel(0, 120)
        page.wait_for_timeout(400)

        out = measure()
        # "Fit", not "smaller than the cell": at the floor the frame touches
        # the cell on its binding axis, so the ratio lands at 1.0 rather than
        # below it. What matters is that the WHOLE frame is now visible, which
        # it was not at cover.
        assert out["inside"], f"whole frame should fit in the cell: {out}"
        assert out["ratio"] < before_ratio, f"did not pull back at all: {out}"
        assert out["centredX"] < 1.5, f"pulled-back picture drifted off centre: {out}"

        # And back in: it must cover again, no gap left behind.
        for _ in range(40):
            page.mouse.wheel(0, -120)
        page.wait_for_timeout(400)
        assert measure()["ratio"] > 1
        browser.close()


def test_reset_zoom_is_offered_and_works(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        box = page.locator(".cell").first.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(6):
            page.mouse.wheel(0, -120)
        page.wait_for_timeout(300)
        assert page.locator('.cell[data-zoomed="true"]').count() == 1

        zoomed_width = page.evaluate(
            "document.querySelector('.cell iframe').getBoundingClientRect().width"
        )
        page.locator(".cell").first.click(button="right")
        page.wait_for_selector("#menu:not([hidden])", timeout=5_000)
        page.locator("#menu button", has_text="Reset zoom").first.click()
        page.wait_for_timeout(400)

        reset_width = page.evaluate(
            "document.querySelector('.cell iframe').getBoundingClientRect().width"
        )
        assert reset_width < zoomed_width, f"{zoomed_width} -> {reset_width}"
        assert page.locator('.cell[data-zoomed="true"]').count() == 0
        browser.close()


def _iframe_left(page, nth=0):
    return page.evaluate(
        f"""() => {{
            const c = document.querySelectorAll('.cell')[{nth}];
            const b = c.getBoundingClientRect();
            const f = c.querySelector('iframe').getBoundingClientRect();
            return f.left - b.left;
        }}"""
    )


def test_drag_pans_a_zoomed_cell(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        box = page.locator(".cell").first.bounding_box()
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        for _ in range(6):
            page.mouse.wheel(0, -120)
        page.wait_for_timeout(300)

        before = _iframe_left(page)
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx - 60, cy, steps=8)
        page.mouse.up()
        page.wait_for_timeout(300)

        after = _iframe_left(page)
        assert after < before - 20, f"drag did not pan: {before} -> {after}"


def test_dragging_cannot_open_a_gap(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        box = page.locator(".cell").first.bounding_box()
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        for _ in range(5):
            page.mouse.wheel(0, -120)
        page.wait_for_timeout(300)

        # Haul it far past the edge in both directions.
        page.mouse.move(cx, cy)
        page.mouse.down()
        page.mouse.move(cx + 2000, cy + 2000, steps=10)
        page.mouse.up()
        page.wait_for_timeout(300)

        covered = page.evaluate(
            """() => {
                const c = document.querySelector('.cell');
                const b = c.getBoundingClientRect();
                const f = c.querySelector('iframe').getBoundingClientRect();
                return f.left <= b.left + 1 && f.top <= b.top + 1
                    && f.right >= b.right - 1 && f.bottom >= b.bottom - 1;
            }"""
        )
        assert covered, "dragging exposed a gap in the cell"


def test_right_click_does_not_start_a_drag(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )
        page.locator(".cell").first.click(button="right")
        page.wait_for_selector("#menu:not([hidden])", timeout=5_000)
        assert page.evaluate("document.querySelector('.cell').dataset.dragging") is None


def test_reset_view_clears_zoom_and_pan_on_every_cell(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        # Pre-roll re-crops every cell, so a baseline taken before it finishes
        # is measured mid-layout -- widths narrower than the cell itself.
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        geometry = """() => [...document.querySelectorAll('.cell')].map(c => {
            const b = c.getBoundingClientRect();
            const f = c.querySelector('iframe').getBoundingClientRect();
            return [f.width, f.left - b.left, f.top - b.top];
        })"""

        # Content boxes arrive asynchronously and re-crop the cell they belong
        # to, so a baseline taken too early is stale for whichever cells had
        # not been measured yet. Wait for two identical reads.
        previous = None
        for _ in range(30):
            page.wait_for_timeout(500)
            current = page.evaluate(geometry)
            if current == previous:
                break
            previous = current
        baseline = page.evaluate(geometry)

        # Zoom two different cells and pan one of them.
        for nth in (1, 5):
            box = page.locator(".cell").nth(nth).bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            for _ in range(5):
                page.mouse.wheel(0, -120)
        page.wait_for_timeout(300)
        box = page.locator(".cell").nth(1).bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + 10, box["y"] + 10, steps=6)
        page.mouse.up()
        page.wait_for_timeout(300)

        changed = page.evaluate(geometry)
        assert changed[1][0] > baseline[1][0], f"cell 1 did not zoom: {changed[1]}"
        assert changed[5][0] > baseline[5][0], f"cell 5 did not zoom: {changed[5]}"
        assert abs(changed[1][1] - baseline[1][1]) > 5, f"cell 1 did not pan: {changed[1]}"

        page.click("#reset-view")
        page.wait_for_timeout(400)

        restored = page.evaluate(geometry)
        for i, (width, left, top) in enumerate(restored):
            assert abs(width - baseline[i][0]) < 1, f"cell {i} zoom not reset: {width}"
            assert abs(left - baseline[i][1]) < 1, f"cell {i} pan-x not reset: {left}"
            assert abs(top - baseline[i][2]) < 1, f"cell {i} pan-y not reset: {top}"
        browser.close()


def test_shuffle_draws_different_videos_from_the_same_query(running_server):
    """New videos, no new search: one query returns 50 for eight cells."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        query_before = page.evaluate("document.getElementById('status').textContent")
        searches = []
        page.on("request", lambda r: searches.append(r.url) if "/api/videos" in r.url else None)

        page.click("#shuffle")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        assert searches == [], "shuffling must not re-resolve, let alone re-search"
        # Same query, and every cell still filled.
        assert (
            page.evaluate("document.getElementById('status').textContent").split("—")[0]
            == (query_before.split("—")[0])
        )
        assert page.locator('.cell[data-empty="true"]').count() == 0
        assert page.evaluate("document.querySelectorAll('.cell iframe').length") == 8
        browser.close()


def test_shuffle_never_repeats_a_video_on_the_wall(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        for _ in range(3):
            page.click("#shuffle")
            page.wait_for_function("window.__prerolled === true", timeout=40_000)
            ids = page.evaluate(
                "[...document.querySelectorAll('.cell')].map(c => c.dataset.videoId)"
            )
            assert len(set(ids)) == len(ids), f"duplicate video on the wall: {ids}"
        browser.close()


def test_reloading_reverts_to_the_ranked_order(running_server):
    """Shuffle is deliberately ephemeral -- the server's ordering is the truth."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        def ids():
            return page.evaluate(
                "[...document.querySelectorAll('.cell')].map(c => c.dataset.videoId)"
            )

        ranked = ids()
        page.click("#shuffle")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        page.reload(wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        assert ids() == ranked, "a reload should restore the server's ranked order"
        browser.close()


def test_double_click_locks_audio_to_one_cell(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        assert page.evaluate("window.__players.every(p => p.isMuted())"), "starts muted"

        page.locator(".cell").nth(4).dblclick()
        page.wait_for_function(
            "window.__players.filter(p => !p.isMuted()).length === 1", timeout=10_000
        )
        flags = page.evaluate("window.__players.map(p => p.isMuted())")
        assert flags[4] is False, flags

        # A lock outlives the cursor leaving -- that is what makes it a lock.
        page.locator("#status").hover()
        page.wait_for_timeout(800)
        assert page.evaluate("window.__players[4].isMuted()") is False
        browser.close()


def test_double_clicking_another_cell_moves_the_lock(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        page.locator(".cell").nth(2).dblclick()
        page.wait_for_function("window.__players[2].isMuted() === false", timeout=10_000)
        page.locator(".cell").nth(6).dblclick()
        page.wait_for_function("window.__players[6].isMuted() === false", timeout=10_000)

        assert page.evaluate("window.__players[2].isMuted()") is True, "old lock not released"
        assert page.evaluate("window.__players.filter(p => !p.isMuted()).length") == 1
        browser.close()


def test_double_clicking_the_same_cell_turns_the_lock_off(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        page.locator(".cell").nth(1).dblclick()
        page.wait_for_function("window.__players[1].isMuted() === false", timeout=10_000)
        page.locator(".cell").nth(1).dblclick()
        # Back to the global state, which is muted.
        page.wait_for_function("window.__players.every(p => p.isMuted())", timeout=10_000)
        browser.close()


def test_a_lock_outranks_hover(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        page.check("#hover-unmute")
        page.locator(".cell").nth(3).dblclick()
        page.wait_for_function("window.__players[3].isMuted() === false", timeout=10_000)

        page.locator(".cell").nth(7).hover()
        page.wait_for_timeout(800)
        assert page.evaluate("window.__players[3].isMuted()") is False, "lock lost to hover"
        assert page.evaluate("window.__players[7].isMuted()") is True
        browser.close()


def test_the_status_bar_names_the_audible_video(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(1500)

        assert page.locator("#audio").text_content().strip() == "", "silent means say nothing"

        page.locator(".cell").nth(5).dblclick()
        page.wait_for_function("window.__players[5].isMuted() === false", timeout=10_000)

        label = page.locator("#audio").text_content()
        assert "locked" in label, label
        assert "6." in label, f"should name the cell it is coming from: {label}"
        video_id = page.evaluate("document.querySelectorAll('.cell')[5].dataset.videoId")
        assert video_id in label, f"should name the video: {label}"

        # Unmuting everything says so rather than naming one cell.
        page.locator(".cell").nth(5).dblclick()
        page.click("#mute")
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=10_000)
        assert "all cells" in page.locator("#audio").text_content()
        browser.close()


def test_rewind_all_sends_every_cell_back_to_the_start(running_server):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(6000)

        before = page.evaluate("window.__players.map(p => p.getCurrentTime())")
        assert max(before) > 3, f"nothing played, so rewinding proves nothing: {before}"

        page.click("#rewind")
        page.wait_for_timeout(1200)
        after = page.evaluate("window.__players.map(p => p.getCurrentTime())")
        assert all(t < 3 for t in after), f"not rewound: {after}"
        # And it keeps playing, because it was playing.
        page.wait_for_function(
            "window.__players.some(p => p.getPlayerState() === 1)", timeout=10_000
        )
        browser.close()


def test_rewind_does_not_start_a_paused_wall(running_server):
    """seekTo resumes a player that is not already paused."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)
        page.click("#play")
        page.wait_for_timeout(4000)
        page.click("#pause")
        page.wait_for_timeout(1200)

        page.click("#rewind")
        page.wait_for_timeout(2000)

        first = page.evaluate("window.__players.map(p => p.getCurrentTime())")
        page.wait_for_timeout(2000)
        second = page.evaluate("window.__players.map(p => p.getCurrentTime())")
        advanced = [(i, a, b) for i, (a, b) in enumerate(zip(first, second)) if b - a > 0.5]
        assert advanced == [], f"rewind restarted a paused wall: {advanced}"
        assert all(t < 3 for t in second), f"not rewound: {second}"
        browser.close()


def test_a_browser_that_has_not_pressed_new_query_stores_nothing(running_server):
    """Only what /api/new-query returns is stored -- never what /api/videos served.

    This is the rule the whole per-browser design turns on, and it is invisible
    to every other test in this file: they all start from a fresh context with
    empty localStorage and a cached config query, so a regression that stored
    `message.query` would leave all of their assertions passing.

    What it would break instead is quiet and slow. The shared config query would
    land in localStorage on first load and thereafter be supplied as a *client*
    query -- which the server serves cache-only and never re-searches. So
    cache.ttl_hours would go inert, the wall would stop refreshing on its own,
    and `source: "client"` in the query log would cease to distinguish anything.
    Nobody would notice for months. Hence an explicit assertion.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        # A cell holding an iframe means the videos response has already been
        # applied -- so whatever was going to be written, has been written.
        page.wait_for_selector(".cell iframe", timeout=20_000)

        assert page.evaluate("localStorage.getItem('ytmatrix.query')") is None
        browser.close()


def test_a_stored_query_the_shared_cache_no_longer_holds_is_cleared(running_server):
    """The other half of the rule: the one write on the /api/videos path is a deletion.

    A stored query is honoured only while the shared cache still holds it. Once
    it ages out the server silently answers with the config query instead, and
    this browser has to drop its own so it falls back cleanly. Storing the
    fallback instead would pin it as a client query forever -- the same bug
    wearing a different hat.

    Costs no quota: an unhonoured client query falls back to the config query,
    which this fixture has seeded, so the server searches nothing.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        # Stand in for a query that has aged out of the shared cache. Set after
        # the first load rather than via an init script, which would put it back
        # on the reload and prove nothing.
        #
        # The remembered wall is dropped alongside it, because a restored wall
        # short-circuits the request entirely -- there is nothing to detect if
        # the browser never asks. That is the point of restoring, and it just
        # moves this clean-up to the first resync that does contact the server
        # (a reconnect, or a search-affecting config change).
        page.evaluate("localStorage.setItem('ytmatrix.query', 'a query no cache ever held')")
        page.evaluate("localStorage.removeItem('ytmatrix.wall')")
        page.reload(wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        assert page.evaluate("localStorage.getItem('ytmatrix.query')") is None
        # And it fell back to the shared query rather than to an empty wall.
        assert "golden cover" in page.evaluate("document.getElementById('status').textContent")
        assert page.locator('.cell[data-empty="true"]').count() == 0
        browser.close()


def test_a_reload_restores_the_wall_without_asking_the_server(running_server):
    """The videos, not just the query that found them.

    Replaying a query made the server resolve it again on every load: a cache
    hit for the search, but select_videos still ran, which on the deployment is
    seconds of motion scoring for a set the browser had already been shown. It
    was not deterministic either -- reserves get consumed and scoring widens in
    waves -- so a reload could legitimately come back with a different eight
    videos than the ones on screen a moment earlier, which is what made
    reloading feel like it broke the wall.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        before = page.evaluate(
            "[...document.querySelectorAll('.cell')].map(c => c.dataset.videoId ?? '')"
        )
        assert any(before), "the first load put something on the wall"

        # Anything asked for on the reload would show up here.
        requested = []
        page.route(
            "**/api/videos*",
            lambda route: (requested.append(route.request.url), route.continue_())[-1],
        )
        page.reload(wait_until="load")
        page.wait_for_selector(".cell iframe", timeout=20_000)

        after = page.evaluate(
            "[...document.querySelectorAll('.cell')].map(c => c.dataset.videoId ?? '')"
        )
        assert after == before, "a reload must restore exactly the wall that was there"
        assert requested == [], "a restored wall must cost no request at all"
        browser.close()


def test_a_container_still_waking_is_waited_out_not_crashed_on(running_server):
    """The first request to a sleeping or newly deployed container fails, and it
    fails in prose rather than JSON.

    @cloudflare/containers answers a cold start with a 500 whose body is
    "Failed to start container: ..." while it boots, or a 503 saying an instance
    is still being provisioned -- its own message warns that can take minutes.
    Neither is an error in this app. Parsing either as JSON threw, left `config`
    null with nothing retrying, and hung the wall on a blank page; pressing
    Query: then spent 100 units and threw again on cellCount(config.grid).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()

        # Fail the config fetch exactly the way a waking container does, then
        # let it through -- which is what happens once the port comes up.
        attempts = []

        def flaky_config(route):
            attempts.append(1)
            if len(attempts) <= 2:
                route.fulfill(
                    status=500,
                    content_type="text/plain",
                    body="Failed to start container: container is not running",
                )
            else:
                route.continue_()

        page.route("**/api/config", flaky_config)
        page.goto(running_server, wait_until="load")

        # The wall still starts, having waited the container out.
        page.wait_for_selector(".cell iframe", timeout=30_000)
        assert len(attempts) >= 3, "the config fetch should have been retried"
        assert page.locator('.cell[data-empty="true"]').count() == 0
        browser.close()


def test_new_query_refuses_before_a_config_has_loaded(running_server):
    """The button is reachable before the first resync finishes. Generating
    without a config spends 100 units and then throws in applyVideos, which is
    the worst of both -- so it declines and says so."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()

        # Never let a config through, so `config` stays null.
        page.route(
            "**/api/config",
            lambda route: route.fulfill(
                status=500, content_type="text/plain", body="Failed to start container"
            ),
        )
        spent = []
        page.route(
            "**/api/new-query",
            lambda route: (spent.append(1), route.abort())[-1],
        )
        page.goto(running_server, wait_until="load")
        page.wait_for_timeout(1500)
        page.click("#new-query", force=True)
        page.wait_for_timeout(800)

        assert spent == [], "a generation without a config must not spend quota"
        browser.close()
