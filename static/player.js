import {
  splitSlots,
  substituteFailedSlot,
  classifyConfigChange,
  cellCount,
  coverRect,
  shouldRestart,
  prerollComplete,
  videoUrl,
  formatTimecode,
} from "./grid-logic.js";
import { connectSocket } from "./socket.js";

const gridEl = document.getElementById("grid");
const statusEl = document.getElementById("status");
const playButton = document.getElementById("play");
const pauseButton = document.getElementById("pause");
const muteButton = document.getElementById("mute");
const newQueryButton = document.getElementById("new-query");
const followCheckbox = document.getElementById("follow");
const promptInput = document.getElementById("prompt");
const menuEl = document.getElementById("menu");

let config = null;
let slotState = { slots: [], reserves: [] };
let players = [];
let apiReady = false;

// What the user wants playback to be doing. Distinct from what the players are
// actually doing, which lags behind while a new set pre-rolls.
let wantPlaying = false;
// True once every player in the current set has buffered. Until then nothing
// is allowed to start, so eight videos begin together instead of trickling in.
let prerolled = false;
// Bumped on every rebuild so a pre-roll for a discarded set cannot start
// players belonging to the set that replaced it.
let generation = 0;

const PREROLL_TIMEOUT_MS = 25000;
const PREROLL_POLL_MS = 250;
const LOOP_POLL_MS = 500;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function livePlayers() {
  return players.filter(Boolean);
}
// Runtime mute state, seeded from config on first load and thereafter owned by
// the button. Kept separate from config.playback.muted so a config push does
// not silently undo what the user just clicked.
let muted = true;

function applyMuteState(player) {
  if (!player?.mute) return;
  if (muted) player.mute();
  else player.unMute();
}

function refreshMuteButton() {
  muteButton.textContent = muted ? "Unmute all" : "Mute all";
  muteButton.dataset.muted = String(muted);
}

// cc_load_policy=0 only means "do not turn captions on for me"; a video whose
// own default is captions-on still shows them, and the captions module is not
// even loaded until playback begins. So this runs again on PLAYING, not only
// on ready. Both module names are tried because the API has used each.
//
// This cannot touch text burned into the video's own pixels -- creator-added
// subtitles are part of the picture and no API reaches them.
function suppressCaptions(player) {
  for (const module of ["captions", "cc"]) {
    try {
      player.unloadModule(module);
    } catch {
      // Not every player exposes both; whichever exists is enough.
    }
  }
  try {
    player.setOption("captions", "track", {});
  } catch {
    // Only valid once the module has loaded; harmless when it has not.
  }
}

// Re-crop whenever a cell changes size: window resize, or a grid change that
// reshapes every cell at once.
const cellResizeObserver = new ResizeObserver((entries) => {
  for (const entry of entries) applyCoverFit(entry.target);
});

// Where the real picture sits inside each video's 16:9 frame, keyed by video
// id. Populated asynchronously; until it arrives a cell simply uses plain
// cover-fit, which is what it did before this existed.
const contentBoxes = new Map();

// video id -> title, so the context menu can name what you right-clicked
// without another round trip.
const titles = new Map();

async function loadContentBox(videoId, cell) {
  if (!videoId || contentBoxes.has(videoId)) return;
  try {
    const response = await fetch(`/api/content-box/${encodeURIComponent(videoId)}`);
    if (!response.ok) return;
    contentBoxes.set(videoId, await response.json());
    applyCoverFit(cell);
  } catch {
    // No box means no extra zoom -- the cell still fills, just with bars.
  }
}

function applyCoverFit(cell) {
  const iframe = cell?.querySelector("iframe");
  if (!iframe) return;
  const { width, height } = cell.getBoundingClientRect();
  if (!width || !height) return;
  const rect = coverRect(width, height, contentBoxes.get(cell.dataset.videoId));
  iframe.style.width = `${rect.width}px`;
  iframe.style.height = `${rect.height}px`;
  iframe.style.left = `${rect.left}px`;
  iframe.style.top = `${rect.top}px`;
}

// The IFrame API signals readiness by calling this global exactly once, when
// www-widgetapi.js finishes loading. That is a race we lose more often than
// not: player.js is a module, so it is deferred until after the document
// parses, while the API script is injected during parsing and often finishes
// first -- especially when cached. When it wins, it finds no callback
// registered, never calls one, and the page sits blank with no error at all.
//
// So check for an already-loaded API first, and only register the callback if
// it genuinely has not arrived yet.
function whenYouTubeApiReady(callback) {
  if (window.YT?.Player) {
    callback();
    return;
  }
  const previous = window.onYouTubeIframeAPIReady;
  window.onYouTubeIframeAPIReady = () => {
    previous?.();
    callback();
  };
}

whenYouTubeApiReady(() => {
  apiReady = true;
  rebuild();
});

// The "<query> — N playing · cached · 900/5000 units today" line. Kept around
// so transient states (pre-rolling, ready) can be appended without losing it.
let statusPrefix = "loading…";

function setStatus(text, state = "") {
  statusEl.textContent = text;
  statusEl.dataset.state = state;
}

function destroyPlayers() {
  for (const player of players) {
    try {
      player?.destroy();
    } catch {
      // A player torn down mid-load throws; nothing useful to do about it.
    }
  }
  players = [];
}

function buildCells() {
  cellResizeObserver.disconnect();
  gridEl.replaceChildren();
  gridEl.style.gridTemplateColumns = `repeat(${config.grid.cols}, 1fr)`;
  gridEl.style.gridTemplateRows = `repeat(${config.grid.rows}, 1fr)`;

  return slotState.slots.map((videoId, index) => {
    const cell = document.createElement("div");
    cell.className = "cell";
    cell.dataset.empty = videoId ? "false" : "true";
    if (videoId) cell.dataset.videoId = videoId;
    const mount = document.createElement("div");
    mount.id = `slot-${index}`;
    cell.appendChild(mount);
    gridEl.appendChild(cell);
    cellResizeObserver.observe(cell);
    return { cell, mount, videoId };
  });
}

function makePlayer({ mount, videoId }, index) {
  if (!videoId) return null;
  return new YT.Player(mount, {
    videoId,
    playerVars: {
      // Always start muted regardless of config: browsers only autoplay muted
      // video. The configured/desired state is applied on ready, below.
      mute: 1,
      controls: 0,
      // Related videos cannot be removed, only restricted to the same channel.
      rel: 0,
      playsinline: 1,
      // Annotations and cards off. Unlike modestbranding and showinfo -- both
      // deprecated and silently ignored -- this one still does something.
      iv_load_policy: 3,
      // Do not turn captions on by default. Burned-in captions on a wall of
      // eight videos is noise, and it is the one piece of overlay text a
      // player parameter can actually reach.
      cc_load_policy: 0,
      disablekb: 1,
      fs: 0,
      start: config.playback.start_offset,
    },
    events: {
      onReady: (event) => {
        applyMuteState(event.target);
        suppressCaptions(event.target);
        // The iframe only exists once the player is ready, so this is the
        // earliest point the crop can be applied. Ask the player for its own
        // iframe: `mount` was replaced by it and is detached by now.
        const cell = event.target.getIframe()?.closest(".cell");
        applyCoverFit(cell);
        // Detect this video's black bars and re-crop past them once known.
        loadContentBox(cell?.dataset.videoId, cell);
        // Starting is not this handler's job -- prerollCurrentSet() owns it,
        // so that nothing begins until every cell is buffered.
      },
      onError: () => handlePlayerError(index),
      onStateChange: (event) => {
        // The captions module only exists once playback has begun, so the
        // onReady attempt above is often too early to have had any effect.
        if (event.data === YT.PlayerState.PLAYING) suppressCaptions(event.target);
        // Backstop only. The loop guard above should restart a video before it
        // ever reaches ENDED; if one slips through, recover rather than leave
        // a cell sitting on the end screen.
        if (event.data === YT.PlayerState.ENDED && config.playback.loop) {
          event.target.seekTo(config.playback.start_offset, true);
          if (wantPlaying) event.target.playVideo();
        }
      },
    },
  });
}

function handlePlayerError(index) {
  const next = substituteFailedSlot(slotState, index);
  slotState = next;
  const cell = gridEl.children[index];
  if (!cell) return;

  try {
    players[index]?.destroy();
  } catch {
    // Already gone.
  }

  const videoId = slotState.slots[index];
  cell.dataset.empty = videoId ? "false" : "true";
  // The substitute is a different video with its own bars, so the cell must
  // stop claiming the failed video's content box.
  if (videoId) cell.dataset.videoId = videoId;
  else delete cell.dataset.videoId;
  cell.replaceChildren();
  const mount = document.createElement("div");
  mount.id = `slot-${index}`;
  cell.appendChild(mount);
  cellResizeObserver.observe(cell);
  players[index] = makePlayer({ mount, videoId }, index);

  if (!next.replaced) {
    setStatus("reserve pool exhausted for one or more cells", "error");
  }
}

function rebuild() {
  if (!apiReady || !config) return;
  // Every index the open menu is holding is about to become meaningless.
  closeMenu();
  destroyPlayers();
  const cells = buildCells();
  players = cells.map((cell, index) => makePlayer(cell, index));
  // Exposed for the browser smoke test to assert against.
  window.__players = players;
  prerolled = false;
  window.__prerolled = false;
  refreshControls();
  prerollCurrentSet(++generation);
}

function refreshControls() {
  playButton.disabled = !prerolled;
  window.__wantPlaying = wantPlaying;
}

/**
 * Buffer every player before letting any of them start.
 *
 * Without this the wall trickles in -- one cell plays while three are still
 * fetching -- which looks broken and, on a new query, means the first video
 * is several seconds ahead of the last. Pre-rolling is done muted regardless
 * of the mute button, because a muted play is the only kind a browser will
 * start without a gesture; the real mute state is restored afterwards.
 */
async function prerollCurrentSet(token) {
  const set = livePlayers();
  if (set.length === 0) {
    prerolled = true;
    window.__prerolled = true;
    refreshControls();
    return;
  }

  setStatus(`${statusPrefix} · pre-rolling…`, "busy");

  // Wait for the API to attach its methods before touching any of them.
  const deadline = Date.now() + PREROLL_TIMEOUT_MS;
  while (Date.now() < deadline && generation === token) {
    if (set.every((player) => typeof player.getVideoLoadedFraction === "function")) break;
    await sleep(PREROLL_POLL_MS);
  }
  if (generation !== token) return;

  for (const player of set) {
    try {
      player.mute();
      player.playVideo();
    } catch {
      // A player torn down mid-preroll; the generation check catches it.
    }
  }

  while (Date.now() < deadline && generation === token) {
    const fractions = set.map((player) => {
      try {
        return player.getVideoLoadedFraction();
      } catch {
        return 0;
      }
    });
    if (prerollComplete(fractions)) break;
    await sleep(PREROLL_POLL_MS);
  }
  if (generation !== token) return;

  // Park every player back at the start, together.
  for (const player of set) {
    try {
      player.pauseVideo();
      player.seekTo(config.playback.start_offset, true);
      applyMuteState(player);
    } catch {
      // Same as above.
    }
  }

  prerolled = true;
  window.__prerolled = true;
  refreshControls();

  // A new set starts paused unless the user asked it to follow the play state.
  if (wantPlaying && followCheckbox.checked) {
    startAll();
  } else {
    wantPlaying = false;
    refreshControls();
    setStatus(`${statusPrefix} · ready — press Play`);
  }
}

function startAll() {
  if (!prerolled) return;
  wantPlaying = true;
  refreshControls();
  for (const player of livePlayers()) {
    try {
      player.playVideo();
    } catch {
      // Nothing useful to do.
    }
  }
  setStatus(statusPrefix);
}

function pauseAll() {
  wantPlaying = false;
  refreshControls();
  for (const player of livePlayers()) {
    try {
      player.pauseVideo();
    } catch {
      // Nothing useful to do.
    }
  }
}

/**
 * Restart each video shortly before it ends.
 *
 * Looping on the ENDED event is too late: YouTube draws its end-screen
 * suggestion grid over the video as it finishes, so by the time the event
 * fires the cards are already on screen. Never letting playback reach the end
 * is the only way to keep them away.
 */
setInterval(() => {
  if (!config?.playback?.loop || !wantPlaying) return;
  for (const player of livePlayers()) {
    try {
      if (typeof player.getDuration !== "function") continue;
      if (shouldRestart(player.getCurrentTime(), player.getDuration())) {
        player.seekTo(config.playback.start_offset, true);
        player.playVideo();
      }
    } catch {
      // A player mid-teardown; skip it.
    }
  }
}, LOOP_POLL_MS);

function applyInPlace() {
  for (const player of players) {
    if (!player?.seekTo) continue;
    player.seekTo(config.playback.start_offset);
  }
}

function applyVideos(message) {
  for (const [videoId, title] of Object.entries(message.titles ?? {})) {
    titles.set(videoId, title);
  }
  slotState = splitSlots(message.video_ids.concat(message.reserves), cellCount(config.grid));
  const notes = {
    quota_exceeded_stale: "quota spent — showing cached results",
    budget_exceeded_stale: "daily budget spent — showing cached results",
    no_results: "no results for that query",
  };
  const spent = message.units_spent_today ?? 0;
  const limit = message.daily_limit_units ?? 0;
  const budgetText = limit ? ` · ${spent}/${limit} units today` : ` · ${spent} units today`;
  const relaxed = message.static_relaxed
    ? ` · ${message.static_relaxed} still${message.static_relaxed === 1 ? "" : "s"}`
    : "";
  statusPrefix =
    notes[message.note] ??
    `“${message.query}” — ${message.video_ids.filter(Boolean).length} videos · ${
      message.from_cache ? "cached" : "fresh search"
    }${relaxed}${budgetText}`;
  setStatus(statusPrefix, message.note ? "error" : "");
  rebuild();
}

// Generating costs 100 quota units, so it never happens implicitly. A plain
// reload restores the persisted query for free; a new one takes an explicit
// act -- the New query button, or ?new=true.
let requestedNewThisPageLoad = false;
let seededMuteFromConfig = false;
let seededFollowFromConfig = false;

async function requestNewQuery(prompt = null) {
  newQueryButton.disabled = true;
  setStatus(prompt ? `inventing a query from “${prompt}”…` : "inventing a query…", "busy");
  try {
    const response = await fetch("/api/new-query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(prompt ? { prompt } : {}),
    });
    if (response.ok) {
      applyVideos(await response.json());
      return true;
    }
    // A failed generation must leave the wall working, not blank.
    const body = await response.json().catch(() => ({}));
    setStatus(`query generation failed: ${body.detail ?? response.status}`, "error");
    return false;
  } finally {
    newQueryButton.disabled = false;
  }
}

async function resync() {
  config = await (await fetch("/api/config")).json();

  // Seed once. On a later reconnect the button, not the file, is the truth.
  if (!seededMuteFromConfig) {
    seededMuteFromConfig = true;
    muted = config.playback.muted;
    refreshMuteButton();
  }

  newQueryButton.hidden = !config.query_generation?.enabled;

  // Seed the checkbox from config once; after that it is the user's switch.
  if (!seededFollowFromConfig) {
    seededFollowFromConfig = true;
    followCheckbox.checked = Boolean(config.playback.autoplay_on_change);
  }

  const wantsNew = new URLSearchParams(window.location.search).get("new") === "true";
  if (wantsNew && config.query_generation?.enabled && !requestedNewThisPageLoad) {
    requestedNewThisPageLoad = true;
    // Strip the parameter so a stray refresh -- or the browser restoring the
    // tab -- does not silently spend another 100 units. Getting a new query
    // should always be a thing you chose to do.
    window.history.replaceState({}, "", window.location.pathname);
    if (await requestNewQuery()) return;
  }

  const videosResponse = await fetch("/api/videos");
  if (!videosResponse.ok) {
    const body = await videosResponse.json().catch(() => ({}));
    setStatus(body.detail ?? `error ${videosResponse.status}`, "error");
    return;
  }
  applyVideos(await videosResponse.json());
}

newQueryButton.addEventListener("click", () => requestNewQuery());

// The prompt box is a metaprompt, not a raw search: what you type goes to
// Gemini together with the app's standing guidance, so "sadder, more piano"
// comes back as a query that actually returns a wall's worth of moving video.
promptInput.addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  const prompt = promptInput.value.trim();
  if (!prompt) return;
  promptInput.disabled = true;
  try {
    await requestNewQuery(prompt);
  } finally {
    promptInput.disabled = false;
    promptInput.focus();
  }
});

// --- per-cell context menu -------------------------------------------------
//
// The iframe has pointer-events:none (so hovering cannot summon YouTube's own
// overlay), which leaves the cell itself free to receive the right-click. That
// is the only reason a custom menu is possible here at all.

let menuTarget = null;

function closeMenu() {
  menuEl.hidden = true;
  menuTarget = null;
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    setStatus(`copied ${label}`, "busy");
    setTimeout(() => setStatus(statusPrefix), 1600);
  } catch {
    setStatus("clipboard blocked by the browser", "error");
  }
}

function playerForCell(cell) {
  const index = [...gridEl.children].indexOf(cell);
  return { index, player: index >= 0 ? players[index] : null };
}

function menuItems(cell) {
  const { index, player } = playerForCell(cell);
  const videoId = cell.dataset.videoId;
  if (!videoId) return [];

  let time = 0;
  let isMuted = muted;
  let state = -1;
  try {
    time = player?.getCurrentTime?.() ?? 0;
    isMuted = player?.isMuted?.() ?? muted;
    state = player?.getPlayerState?.() ?? -1;
  } catch {
    // A player mid-teardown still gets a useful, if plainer, menu.
  }
  const playing = state === 1;

  return [
    {
      label: "Copy video URL at time",
      hint: formatTimecode(time),
      run: () => copyText(videoUrl(videoId, time), `URL at ${formatTimecode(time)}`),
    },
    {
      label: "Copy video URL",
      run: () => copyText(videoUrl(videoId), "URL"),
    },
    {
      label: "Copy video ID",
      hint: videoId,
      run: () => copyText(videoId, "video ID"),
    },
    {
      label: "Copy title",
      run: () => copyText(titles.get(videoId) ?? videoId, "title"),
    },
    {
      label: "Open on YouTube at time",
      run: () => window.open(videoUrl(videoId, time), "_blank", "noopener"),
    },
    {
      label: playing ? "Pause this cell" : "Play this cell",
      run: () => (playing ? player?.pauseVideo?.() : player?.playVideo?.()),
    },
    {
      label: isMuted ? "Unmute this cell" : "Mute this cell",
      run: () => (isMuted ? player?.unMute?.() : player?.mute?.()),
    },
    {
      label: "Restart this cell",
      run: () => player?.seekTo?.(config.playback.start_offset, true),
    },
    {
      label: "Replace with next reserve",
      hint: `${slotState.reserves.length} left`,
      run: () => handlePlayerError(index),
    },
  ];
}

function openMenu(cell, x, y) {
  const items = menuItems(cell);
  if (items.length === 0) return;

  menuEl.replaceChildren();
  const head = document.createElement("div");
  head.className = "head";
  head.textContent = titles.get(cell.dataset.videoId) ?? cell.dataset.videoId;
  menuEl.appendChild(head);

  for (const item of items) {
    const button = document.createElement("button");
    const label = document.createElement("span");
    label.textContent = item.label;
    button.appendChild(label);
    if (item.hint) {
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = item.hint;
      button.appendChild(hint);
    }
    button.addEventListener("click", () => {
      item.run();
      closeMenu();
    });
    menuEl.appendChild(button);
  }

  menuTarget = cell;
  menuEl.hidden = false;
  // Place it, then nudge back inside the viewport if it would overhang.
  menuEl.style.left = `${x}px`;
  menuEl.style.top = `${y}px`;
  const rect = menuEl.getBoundingClientRect();
  if (rect.right > window.innerWidth) {
    menuEl.style.left = `${Math.max(0, window.innerWidth - rect.width - 4)}px`;
  }
  if (rect.bottom > window.innerHeight) {
    menuEl.style.top = `${Math.max(0, window.innerHeight - rect.height - 4)}px`;
  }
}

gridEl.addEventListener("contextmenu", (event) => {
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  event.preventDefault();
  openMenu(cell, event.clientX, event.clientY);
});

document.addEventListener("click", (event) => {
  if (!menuEl.hidden && !menuEl.contains(event.target)) closeMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMenu();
});
window.addEventListener("blur", closeMenu);
// A rebuild invalidates every index the open menu was holding.
window.addEventListener("resize", closeMenu);

// Eight players starting at once is exactly what browsers throttle; this click
// is the user gesture that makes them all start. It is disabled until the set
// has pre-rolled, so there is no window where pressing it starts only some.
playButton.addEventListener("click", startAll);
pauseButton.addEventListener("click", pauseAll);

muteButton.addEventListener("click", () => {
  muted = !muted;
  refreshMuteButton();
  // Unmuting eight players at once is only permitted off a user gesture --
  // this click is it. Doing it any other way leaves some players silent.
  for (const player of players) applyMuteState(player);
});

refreshMuteButton();
refreshControls();

connectSocket({
  onReconnect: resync,
  onMessage: (message) => {
    if (message.type === "config") {
      const change = classifyConfigChange(config, message.config);
      config = message.config;
      if (change === "rebuild") rebuild();
      else if (change === "in-place") applyInPlace();
    } else if (message.type === "videos") {
      applyVideos(message);
    }
  },
});
