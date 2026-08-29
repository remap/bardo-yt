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
  rectFor,
  zoomAt,
  panBy,
  shuffleSlots,
  audioTarget,
  isAudible,
  AUDIO_ALL,
  AUDIO_NONE,
  IDENTITY_VIEW,
  needsRefetch,
  overridesStoredQuery,
} from "./grid-logic.js";
import { connectSocket } from "./socket.js";

// --- diagnostics -----------------------------------------------------------
//
// Deliberately verbose and deliberately always on. The wall is slow in ways
// that only show up on the deployment -- a container waking from sleep, a
// Gemini call that takes fifteen seconds, a resync racing a generation -- and
// none of that is visible from a laptop. Timestamps are relative to page load,
// so a log can be read as a timeline rather than a pile of events.
const T0 = performance.now();
const since = () => ((performance.now() - T0) / 1000).toFixed(2).padStart(6);
function wlog(...args) {
  console.log(`[wall ${since()}s]`, ...args);
}

/** fetch, with the round trip timed and both ends logged. */
async function tfetch(label, url, init) {
  const started = performance.now();
  wlog(`-> ${label}`, url);
  try {
    const response = await fetch(url, init);
    const ms = Math.round(performance.now() - started);
    wlog(`<- ${label} ${response.status} in ${ms}ms`);
    return response;
  } catch (error) {
    const ms = Math.round(performance.now() - started);
    wlog(`<- ${label} FAILED after ${ms}ms`, error);
    throw error;
  }
}

wlog("wall-engine.js loaded");

// How long to keep asking while the container wakes.
//
// A sleeping or newly deployed container cannot answer immediately, and
// @cloudflare/containers says so in prose rather than JSON: a 500 whose body is
// "Failed to start container: ..." while it boots, or a 503 explaining that an
// instance is still being provisioned, which its own message warns "may take a
// few minutes". Neither is an error in this app and neither should be shown as
// one -- but the first load used to parse the body as JSON, throw, and leave
// the wall hung on a blank page with nothing retrying and nothing explaining.
const WAKE_DELAYS_MS = [0, 400, 800, 1500, 2500, 4000, 6000, 8000, 10000, 10000];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// fetchJsonPatiently below has to report through the status line, but the real
// setStatus() is defined inside startWall()'s scope, further down this file --
// this module-level function stays outside it, so it cannot close over
// setStatus directly. startWall() installs the real implementation here as
// its first act; until then this is a no-op, which only matters in the window
// before startWall() has run.
let reportStatus = () => {};

/** GET a JSON endpoint, waiting out a container that is still starting. */
async function fetchJsonPatiently(label, url) {
  for (let attempt = 0; attempt < WAKE_DELAYS_MS.length; attempt += 1) {
    if (WAKE_DELAYS_MS[attempt]) await sleep(WAKE_DELAYS_MS[attempt]);
    let response;
    try {
      response = await tfetch(label, url);
    } catch (error) {
      wlog(`${label} could not be reached (attempt ${attempt + 1}), retrying`, error);
      reportStatus("waiting for the server…", "busy");
      continue;
    }
    if (response.ok) {
      try {
        return await response.json();
      } catch (error) {
        // A 200 that is not JSON is not something waiting will fix.
        wlog(`${label} returned 200 but not JSON -- giving up`, error);
        return null;
      }
    }
    const body = await response.text().catch(() => "");
    wlog(
      `${label} ${response.status} (attempt ${attempt + 1}/${WAKE_DELAYS_MS.length}): ` +
        `${body.slice(0, 160)}`,
    );
    reportStatus(
      response.status === 503 ? "starting the server, this can take a minute…" : "waiting for the server…",
      "busy",
    );
  }
  return null;
}
import {
  clearQuery,
  clearWall,
  loadHistory,
  loadQuery,
  loadWall,
  pushHistory,
  saveQuery,
  saveWall,
} from "./wallstate.js";

// The grid-mode default: one CSS grid, sized directly from config.grid.
// /layout (static/layout-page.js) supplies a different computeLayout that
// positions cells against the six real layout-driver screens instead --
// everything else in this file is identical either way.
function defaultComputeLayout(config) {
  return {
    totalCells: cellCount(config.grid),
    containerStyle: {
      display: "grid",
      gridTemplateColumns: `repeat(${config.grid.cols}, 1fr)`,
      gridTemplateRows: `repeat(${config.grid.rows}, 1fr)`,
    },
    cellRect: null,
  };
}

// The body below is deliberately NOT indented a level for being inside this
// function. It used to be the whole of player.js at module scope; wrapping it
// in startWall() so /layout could reuse it was meant to be a pure extraction,
// and re-indenting every line would have turned that into a diff nobody could
// audit line-by-line. Leave it flush left.
export function startWall({ computeLayout = defaultComputeLayout } = {}) {

const gridEl = document.getElementById("grid");
const statusEl = document.getElementById("status");
const playButton = document.getElementById("play");
const pauseButton = document.getElementById("pause");
const muteButton = document.getElementById("mute");
const newQueryButton = document.getElementById("new-query");
const followCheckbox = document.getElementById("follow");
const promptInput = document.getElementById("prompt");
const hoverUnmuteCheckbox = document.getElementById("hover-unmute");
const menuEl = document.getElementById("menu");
const resetViewButton = document.getElementById("reset-view");
const shuffleButton = document.getElementById("shuffle");
const audioEl = document.getElementById("audio");
const rewindButton = document.getElementById("rewind");

let config = null;
let slotState = { slots: [], reserves: [] };
let players = [];
let apiReady = false;
// Set once a `videos` payload has actually been applied. Until then there is
// nothing to build and nothing to pre-roll.
let haveVideos = false;

// What the user wants playback to be doing. Distinct from what the players are
// actually doing, which lags behind while a new set pre-rolls.
let wantPlaying = false;
// True once every player in the current set has buffered. Until then nothing
// is allowed to start, so eight videos begin together instead of trickling in.
let prerolled = false;
// Bumped on every rebuild so a pre-roll for a discarded set cannot start
// players belonging to the set that replaced it.
let generation = 0;

// Which video set is allowed to land. Two paths can be in flight at once --
// requestNewQuery() waiting on Gemini, and resync() waiting on /api/videos --
// and either can take seconds. Without this, whichever ANSWERS last won, not
// whichever was ASKED for last, so a WebSocket reconnect mid-generation would
// snap the wall back to the previous query.
//
// It also protected nothing on the way out: resync ends by clearing a stored
// query that no longer matches what the server served, so a late resync landing
// after a new query had been saved would erase it. That is what made the wall
// look like it was picking queries at random.
//
// Separate from `generation` above deliberately: that one guards players
// mid-preroll (gotcha 18), this one guards which response may touch the wall
// at all. Same idea, different lifetimes -- a single counter would make a
// preroll cancel a legitimate resync.
let applySeq = 0;

// True while a New query is waiting on Gemini. An automatic resync -- a
// WebSocket reconnect, a config push -- must not supersede an explicit act the
// operator paid 100 units for. Without this, a generation that took longer than
// the reconnect it raced was discarded silently: the units were spent, the query
// went into the avoid-list, and the wall never changed. That is the "having it
// outstanding confuses things" case, and the fix is precedence rather than
// ordering: whoever asked LAST wins among equals, but a deliberate request
// outranks a housekeeping one.
let generating = false;

// A page load restores its stored wall once. Later resyncs are reconnects and
// config pushes, which exist to go and look rather than to show what was
// already there.
let restoredThisLoad = false;

const PREROLL_TIMEOUT_MS = 25000;
const PREROLL_POLL_MS = 250;
// pauseVideo() is not synchronous, and a seek can knock a player back into
// playing, so the pause is confirmed rather than assumed.
const PAUSE_CONFIRM_ATTEMPTS = 8;
const PAUSE_CONFIRM_MS = 120;
const LOOP_POLL_MS = 500;


function livePlayers() {
  return players.filter(Boolean);
}
// Runtime mute state, seeded from config on first load and thereafter owned by
// the button. Kept separate from config.playback.muted so a config push does
// not silently undo what the user just clicked.
let muted = true;

// The cell the cursor is over while hover-to-unmute is on, or null.
let audibleIndex = null;
// The cell double-clicked to hold the audio. Outranks hover and the global
// mute button, and survives the cursor leaving the grid entirely.
let lockedIndex = null;

function currentAudioTarget() {
  return audioTarget({
    locked: lockedIndex,
    hovered: audibleIndex,
    hoverEnabled: hoverUnmuteCheckbox?.checked ?? false,
    muted,
  });
}

function applyMuteState(player, index = null) {
  if (!player?.mute) return;
  if (isAudible(index, currentAudioTarget())) player.unMute();
  else player.mute();
}

function applyMuteStateToAll() {
  players.forEach((player, index) => applyMuteState(player, index));
  refreshAudioIndicator();
}

// Say out loud which video you are hearing -- with eight of them playing,
// tracing a sound back to a cell by ear is hopeless.
function refreshAudioIndicator() {
  const target = currentAudioTarget();
  if (target === AUDIO_NONE) {
    audioEl.textContent = "";
    delete audioEl.dataset.locked;
    return;
  }
  if (target === AUDIO_ALL) {
    audioEl.textContent = "audio: all cells";
    delete audioEl.dataset.locked;
    return;
  }
  const videoId = gridEl.children[target]?.dataset.videoId;
  const name = titles.get(videoId) ?? videoId ?? `cell ${target + 1}`;
  const locked = Number.isInteger(lockedIndex);
  audioEl.textContent = `${locked ? "audio locked" : "audio"} · ${target + 1}. ${name}`;
  audioEl.dataset.locked = String(locked);
}

function setAudibleCell(index) {
  if (audibleIndex === index) return;
  audibleIndex = index;
  for (const cell of gridEl.children) delete cell.dataset.audible;
  if (index !== null && gridEl.children[index]) {
    gridEl.children[index].dataset.audible = "true";
  }
  applyMuteStateToAll();
}

function refreshMuteButton() {
  muteButton.textContent = muted ? "Unmute" : "Mute";
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

// Per-cell zoom/pan, keyed by cell index. Cleared on rebuild -- a new video
// in that slot starts fresh rather than inheriting someone else's framing.
const views = new Map();

function viewFor(cell) {
  return views.get([...gridEl.children].indexOf(cell)) ?? IDENTITY_VIEW;
}

function applyCoverFit(cell) {
  const iframe = cell?.querySelector("iframe");
  if (!iframe) return;
  const { width, height } = cell.getBoundingClientRect();
  if (!width || !height) return;
  const rect = rectFor(width, height, contentBoxes.get(cell.dataset.videoId), viewFor(cell));
  iframe.style.width = `${rect.width}px`;
  iframe.style.height = `${rect.height}px`;
  iframe.style.left = `${rect.left}px`;
  iframe.style.top = `${rect.top}px`;
}

// The IFrame API signals readiness by calling this global exactly once, when
// www-widgetapi.js finishes loading. That is a race we lose more often than
// not: this module is deferred until after the document parses (both
// player.js and layout-page.js load it as an ES module), while the API script
// is injected during parsing and often finishes first -- especially when
// cached. When it wins, it finds no callback registered, never calls one, and
// the page sits blank with no error at all.
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
// Give fetchJsonPatiently (module scope, above) a way to reach this instance.
reportStatus = setStatus;

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
  const layout = computeLayout(config);
  Object.assign(gridEl.style, layout.containerStyle);

  return slotState.slots.map((videoId, index) => {
    const cell = document.createElement("div");
    cell.className = "cell";
    cell.dataset.empty = videoId ? "false" : "true";
    if (videoId) cell.dataset.videoId = videoId;
    if (layout.cellRect) Object.assign(cell.style, layout.cellRect(index));
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
        applyMuteState(event.target, index);
        suppressCaptions(event.target);
        // The iframe only exists once the player is ready, so this is the
        // earliest point the crop can be applied. Ask the player for its own
        // iframe: `mount` was replaced by it and is detached by now.
        const cell = event.target.getIframe()?.closest(".cell");
        applyCoverFit(cell);
        // Detect this video's black bars and re-crop past them once known.
        loadContentBox(cell?.dataset.videoId, cell);
        // Starting is not this handler's job during pre-roll --
        // prerollCurrentSet() owns that. The one exception is a cell rebuilt
        // *after* the set is already running (a failed embed swapped for a
        // reserve): it has missed pre-roll and would otherwise sit frozen
        // while its neighbours play.
        if (prerolled && wantPlaying) event.target.playVideo();
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
  // haveVideos matters as much as the other two: resync() sets `config`
  // before it fetches the video list, so an API-ready callback landing in
  // between would build an empty grid and immediately declare it pre-rolled
  // and ready -- a brief, wrong "ready" that the real set then replaces.
  if (!apiReady || !config || !haveVideos) return;
  // Every index the open menu is holding is about to become meaningless.
  closeMenu();
  audibleIndex = null;
  lockedIndex = null;
  views.clear();
  // Hide before the new cells exist, not after: the flag must already be set
  // by the time anything can paint.
  gridEl.dataset.preroll = "true";
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
/**
 * Buffer one player and stop it again as soon as *it* is ready.
 *
 * Deliberately per-player rather than a barrier across the set: waiting for
 * the slowest cell before pausing any of them leaves the fast ones visibly
 * running for seconds. Each one here plays for as little time as it takes to
 * buffer, then parks.
 */
async function prerollOne(player, index, token, deadline) {
  const alive = () => generation === token && Date.now() < deadline;
  const read = (fn, fallback) => {
    try {
      return fn();
    } catch {
      return fallback;
    }
  };

  while (alive() && typeof player.getVideoLoadedFraction !== "function") {
    await sleep(PREROLL_POLL_MS);
  }
  if (!alive()) return;

  read(() => player.mute());
  read(() => player.playVideo());

  // Either signal is enough: PLAYING means it has data to show, and a loaded
  // fraction means bytes are in hand even if the state has not flipped yet.
  while (alive()) {
    const fraction = read(() => player.getVideoLoadedFraction(), 0);
    const playing = read(() => player.getPlayerState(), -1) === 1;
    if (playing || prerollComplete([fraction])) break;
    await sleep(PREROLL_POLL_MS);
  }
  if (generation !== token) return;

  // Stop it immediately -- this is the "pause earlier" that keeps a new query
  // from turning into eight videos briefly bursting into life.
  //
  // Order matters, and it is not obvious: seekTo() RESUMES a player that is
  // not already paused, and pauseVideo() does not take effect synchronously.
  // Pausing first then seeking therefore restarts roughly half the wall --
  // observed as four of eight cells still playing. Seek first, pause last,
  // then confirm, because the seek itself can put it back into playing.
  read(() => player.seekTo(config.playback.start_offset, true));
  read(() => player.pauseVideo());

  // 1 is PLAYING and 3 is BUFFERING -- buffering is not a resting state, it is
  // a player on its way to playing, so both have to be chased down. Treating
  // only state 1 as "still running" leaves cells that quietly start a moment
  // later, which is exactly the unclean start this pre-roll exists to prevent.
  const RUNNING_STATES = [1, 3];
  for (let attempt = 0; attempt < PAUSE_CONFIRM_ATTEMPTS; attempt += 1) {
    if (!RUNNING_STATES.includes(read(() => player.getPlayerState(), 2))) break;
    await sleep(PAUSE_CONFIRM_MS);
    read(() => player.pauseVideo());
  }

  read(() => applyMuteState(player, index));
}

async function prerollCurrentSet(token) {
  const prerollStarted = performance.now();
  wlog(`preroll start (generation ${token}) -- nothing plays until every cell has buffered`);
  const set = livePlayers();
  gridEl.dataset.preroll = "true";

  if (set.length === 0) {
    finishPreroll(token);
    return;
  }

  setStatus(`${statusPrefix} · pre-rolling…`, "busy");
  const deadline = Date.now() + PREROLL_TIMEOUT_MS;
  await Promise.all(
    players.map((player, index) =>
      player ? prerollOne(player, index, token, deadline) : null,
    ),
  );
  if (generation !== token) {
    wlog(`preroll ${token} abandoned -- generation is now ${generation}`);
    return;
  }

  wlog(
    `preroll complete in ${Math.round(performance.now() - prerollStarted)}ms ` +
      `(${set.length} cells) -- the wall becomes visible now`,
  );
  finishPreroll(token);
}

function finishPreroll(token) {
  if (generation !== token) return;
  prerolled = true;
  window.__prerolled = true;
  gridEl.dataset.preroll = "false";
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

function rewindAll() {
  for (const player of livePlayers()) {
    try {
      player.seekTo(config.playback.start_offset, true);
      // seekTo resumes a player that is not already paused, so a paused wall
      // would quietly start playing. Put it back.
      if (!wantPlaying) player.pauseVideo();
    } catch {
      // A player mid-teardown; skip it.
    }
  }
}

function toggleMute() {
  muted = !muted;
  refreshMuteButton();
  // Unmuting eight players at once is only permitted off a user gesture --
  // this click is it. Doing it any other way leaves some players silent.
  applyMuteStateToAll();
}

function shuffleWall() {
  const wasPlaying = wantPlaying;
  slotState = shuffleSlots(
    [...slotState.slots, ...slotState.reserves],
    computeLayout(config).totalCells,
  );
  rebuild();
  // Shuffling is not a new query, so it should not silently stop the wall.
  // rebuild() clears wantPlaying via pre-roll; put it back if it was running.
  wantPlaying = wasPlaying;
}

function resetAllViews() {
  views.clear();
  for (const cell of gridEl.children) {
    delete cell.dataset.zoomed;
    applyCoverFit(cell);
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
  // Remembered before anything else, so a reload restores exactly this set
  // rather than asking the server to resolve the query again and possibly
  // choose differently.
  saveWall(message);
  // Logged in full, on purpose. The query that reaches YouTube is not the text
  // typed into the box -- an empty box generates from the theme, and a steer is
  // a metaprompt -- so when the wall shows something unexpected, this is the
  // only place that says what was actually searched. `timings` says where the
  // time went: `origin` at or near 15.00 means its httpx timeout expired, which
  // is swallowed by design and silently changes which videos get chosen.
  console.log("[wall] query=%o from_cache=%o timings=%o", message.query, message.from_cache, message.timings ?? {});
  for (const [videoId, title] of Object.entries(message.titles ?? {})) {
    titles.set(videoId, title);
  }
  haveVideos = true;
  slotState = splitSlots(message.video_ids.concat(message.reserves), computeLayout(config).totalCells);
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
  refreshAudioIndicator();
}

// Generating costs 100 quota units, so it never happens implicitly. A plain
// reload restores the persisted query for free; a new one takes an explicit
// act -- the New query button, or ?new=true.
let requestedNewThisPageLoad = false;
let seededMuteFromConfig = false;
let seededFollowFromConfig = false;

async function requestNewQuery(prompt = null) {
  if (!config) {
    // The button is reachable before the first resync finishes -- and if that
    // resync failed on a waking container, config is still null. Generating
    // now would spend 100 units and then throw in applyVideos on
    // computeLayout(config).totalCells, which is exactly what happened on a
    // cold start.
    wlog("refusing to generate: no config yet");
    setStatus("still waiting for the server — try again in a moment", "error");
    return false;
  }
  const seq = ++applySeq;
  generating = true;
  const history = loadHistory();
  wlog(
    `NEW QUERY start (seq ${seq}) steer=${JSON.stringify(prompt)} ` +
      `history=${history.length} entries -- this is the only thing that spends 100 units`,
  );
  newQueryButton.disabled = true;
  setStatus(prompt ? `inventing a query from “${prompt}”…` : "inventing a query…", "busy");
  try {
    const response = await tfetch("POST /api/new-query", "/api/new-query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        ...(prompt ? { prompt } : {}),
        // The avoid-list lives here now, so it has to travel with the request.
        history,
      }),
    });
    if (response.ok) {
      const message = await response.json();
      // History first, and unconditionally: those 100 units were spent and
      // Gemini said this, so the avoid-list should know even if the result is
      // no longer wanted on screen.
      pushHistory(message.query);
      if (seq !== applySeq) {
        wlog(
          `NEW QUERY DISCARDED (seq ${seq}, now ${applySeq}) -- ${JSON.stringify(message.query)} ` +
            `was generated and PAID FOR, but something newer superseded it before it landed`,
        );
        return true;
      }
      // Remember it before applying: a reload must land on the same wall, and
      // replaying a stored query costs nothing (the server serves it from
      // cache or falls back -- it never re-searches).
      saveQuery(message.query);
      applyVideos(message);
      return true;
    }
    // A failed generation must leave the wall working, not blank.
    const body = await response.json().catch(() => ({}));
    wlog(`NEW QUERY FAILED ${response.status}`, body);
    setStatus(`query generation failed: ${body.detail ?? response.status}`, "error");
    return false;
  } finally {
    generating = false;
    newQueryButton.disabled = false;
  }
}

async function resync() {
  const seqAtEntry = applySeq + 1;
  wlog(`resync start (seq ${seqAtEntry})`);
  const fetchedConfig = await fetchJsonPatiently("GET /api/config", "/api/config");
  if (!fetchedConfig) {
    // Nothing below can run without it -- computeLayout(config).totalCells is
    // the first thing applyVideos does. Better to say so than to hang on a
    // blank page.
    wlog("could not load config -- the wall cannot start");
    setStatus("could not reach the server — reload to try again", "error");
    return;
  }
  config = fetchedConfig;

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

  // A generation is waiting on Gemini, and it outranks this. Claiming a
  // sequence here would supersede it, throwing away a result the operator asked
  // for and paid for; refetching the videos it is about to replace is pointless
  // besides. The config above has already been refreshed, which is the half of
  // a resync that is still worth doing.
  if (generating) {
    wlog("resync: a New query is in flight and outranks this -- leaving the videos to it");
    return;
  }

  // Taken here, not at the top of resync: the ?new=true branch above may have
  // run requestNewQuery, which takes a sequence of its own. Claiming ours after
  // that keeps a failed generation from leaving this resync permanently stale
  // and the wall unrendered.
  const seq = ++applySeq;
  const stored = loadQuery();

  // A wall this browser has already been shown is restored as it was, with no
  // request at all. Re-resolving the query was never free -- the search was a
  // cache hit but select_videos still ran, which on the deployment is seconds
  // of motion scoring -- and it was not deterministic either: reserves get
  // consumed and scoring widens in waves, so a reload could legitimately come
  // back with a different eight videos than the ones on screen a moment ago.
  //
  // Only on the FIRST resync of a page load. A later one is a reconnect or a
  // config change, and those exist precisely to go and look.
  if (!restoredThisLoad) {
    restoredThisLoad = true;
    const remembered = loadWall();
    if (remembered) {
      wlog(
        `restored ${(remembered.video_ids ?? []).filter(Boolean).length} videos from this browser ` +
          `for ${JSON.stringify(remembered.query)} -- no request needed`,
      );
      applyVideos(remembered);
      return;
    }
  }

  wlog(
    stored
      ? `resync: replaying stored query ${JSON.stringify(stored)}`
      : "resync: no stored query, asking for the shared config query",
  );
  const videosResponse = await tfetch(
    "GET /api/videos",
    stored ? `/api/videos?query=${encodeURIComponent(stored)}` : "/api/videos",
  );
  if (!videosResponse.ok) {
    const body = await videosResponse.json().catch(() => ({}));
    setStatus(body.detail ?? `error ${videosResponse.status}`, "error");
    return;
  }
  const message = await videosResponse.json();
  // Store ONLY what New query hands us -- never what /api/videos served.
  //
  // A browser that has never pressed New query must keep sending no query at
  // all, so the server uses the shared config query on its normal path where
  // cache.ttl_hours still applies. Storing whatever came back would put the
  // config query onto the client path too, which is served cache-only and
  // never re-searches -- the wall would then never refresh on its own, and
  // `source: "client"` in the query log would stop distinguishing anything.
  //
  // The one thing worth writing here is a deletion: if we sent a stored query
  // and the server answered with a different one, our query has aged out of
  // the shared cache and is gone. Clearing lets this browser fall back to the
  // shared query cleanly. Storing the fallback instead would pin it as a
  // client query forever, which is the same bug wearing a different hat.
  // Superseded while we were waiting -- a New query started after this resync
  // did. Returning here is what stops the clearQuery() below from erasing the
  // query that generation just saved.
  if (seq !== applySeq) {
    wlog(`resync DISCARDED (seq ${seq}, now ${applySeq}) -- something newer was asked for`);
    return;
  }
  if (stored && message.query !== stored) {
    wlog(`resync: stored query is gone from the cache, clearing it (server served ${JSON.stringify(message.query)})`);
    clearQuery();
    clearWall();
  }
  applyVideos(message);
}

// The one way to spend 100 units. It reads whatever is in the prompt, and an
// empty box means "generate from the standing theme alone" -- which the server
// records as `generated` rather than `manual`.
//
// The box is a metaprompt, not a raw search: what you type goes to Gemini
// together with the app's standing guidance, so "sadder, more piano" comes
// back as a query that actually returns a wall's worth of moving video.
// Enter in the box is the same act as clicking the button, so it IS the click
// rather than a second path to the same code -- the button owns the disabled
// state, the empty-box meaning and the spend, and a parallel handler would
// eventually disagree with it about one of them.
promptInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || newQueryButton.disabled || newQueryButton.hidden) return;
  event.preventDefault();
  newQueryButton.click();
});

newQueryButton.addEventListener("click", async () => {
  const prompt = promptInput.value.trim();
  promptInput.disabled = true;
  try {
    await applyIntent({ type: "newQuery", prompt: prompt || null });
  } finally {
    promptInput.disabled = false;
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

function togglePlayForCell(index) {
  const player = players[index];
  let state = -1;
  try {
    state = player?.getPlayerState?.() ?? -1;
  } catch {
    // A player mid-teardown; nothing useful to do.
  }
  if (state === 1) player?.pauseVideo?.();
  else player?.playVideo?.();
}

function toggleLockedIndex(index) {
  lockedIndex = lockedIndex === index ? null : index;
  for (const other of gridEl.children) delete other.dataset.locked;
  const cell = gridEl.children[index];
  if (lockedIndex !== null && cell) cell.dataset.locked = "true";
  applyMuteStateToAll();
}

function restartCell(index) {
  players[index]?.seekTo?.(config.playback.start_offset, true);
}

function resetCellZoom(index) {
  views.delete(index);
  const cell = gridEl.children[index];
  if (!cell) return;
  cell.dataset.zoomed = "false";
  applyCoverFit(cell);
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
      run: () => togglePlayForCell(index),
    },
    {
      label: lockedIndex === index ? "Unlock audio" : "Lock audio to this cell",
      hint: "double-click",
      run: () => toggleLockedIndex(index),
    },
    {
      label: "Restart this cell",
      run: () => restartCell(index),
    },
    {
      label: "Reset zoom",
      hint: `${(viewFor(cell).zoom ?? 1).toFixed(2)}×`,
      run: () => resetCellZoom(index),
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
playButton.addEventListener("click", () => applyIntent({ type: "play" }));
pauseButton.addEventListener("click", () => applyIntent({ type: "pause" }));
rewindButton.addEventListener("click", () => applyIntent({ type: "rewind" }));
muteButton.addEventListener("click", () => applyIntent({ type: "muteToggle" }));

// Hover to unmute: point at a cell to hear only that one. The iframe has
// pointer-events:none, so the cell itself receives the hover -- the same CSS
// the context menu depends on.
hoverUnmuteCheckbox.addEventListener("change", () =>
  applyIntent({ type: "hoverUnmuteToggle", checked: hoverUnmuteCheckbox.checked }),
);

// Scroll to zoom, anchored on the pointer. passive:false because the page
// must not scroll underneath -- the wall is a fixed-height layout and a
// bubbling wheel event would fight the zoom.
// Wheel events arrive far faster than the browser paints -- a trackpad emits
// them in bursts of dozens -- and each one used to do its own layout write, so
// the compositor was handed more work than it could land in a frame and the
// zoom visibly stuttered. The deltas are accumulated instead and applied once
// per animation frame, which is the rate the screen can actually show.
//
// Reading the cell's bounds is deliberately still done per event: a
// getBoundingClientRect is a read, not a write, and the cursor position it is
// measured against is what makes the zoom cursor-anchored.
let pendingZoom = null;

function flushZoom() {
  const job = pendingZoom;
  pendingZoom = null;
  if (!job) return;
  const { cell, index, width, height, deltaY, x, y } = job;
  // The cell may have been rebuilt out from under a queued frame.
  if (!cell.isConnected) return;
  const view = zoomAt(
    views.get(index) ?? IDENTITY_VIEW,
    width,
    height,
    contentBoxes.get(cell.dataset.videoId),
    deltaY,
    x,
    y,
  );
  views.set(index, view);
  applyCoverFit(cell);
  cell.dataset.zoomed = view.zoom > 1.001 ? "true" : "false";
}

function queueZoom(cell, index, width, height, deltaY, x, y) {
  if (pendingZoom && pendingZoom.index === index) {
    // Same cell, same frame: sum the deltas so no scrolling is lost, and
    // track the cursor to wherever it ended up.
    pendingZoom.deltaY += deltaY;
    pendingZoom.x = x;
    pendingZoom.y = y;
    return;
  }
  // A different cell mid-frame: land the queued one first rather than
  // dropping it.
  if (pendingZoom) flushZoom();
  pendingZoom = { cell, index, width, height, deltaY, x, y };
  requestAnimationFrame(flushZoom);
}

// x, y arrive normalized (0..1) -- from a real local event on THIS page's own
// cell, or relayed from /layout-control's differently-sized rectangle for
// the same cell. Either way this converts to this page's own real pixel
// space before reusing the exact zoom math a local wheel event already used.
function applyCellWheel(index, deltaY, xFraction, yFraction) {
  const cell = gridEl.children[index];
  if (!cell || cell.dataset.empty === "true") return;
  const bounds = cell.getBoundingClientRect();
  queueZoom(cell, index, bounds.width, bounds.height, deltaY, xFraction * bounds.width, yFraction * bounds.height);
}

// dx, dy arrive as a fraction of the SENDER's own cell size (already an
// incremental delta, not an absolute position) -- rescaling by this page's
// own bounds is what makes a drag feel proportionally the same regardless of
// how big /layout-control's rectangle happens to be.
function applyCellPan(index, dxFraction, dyFraction) {
  const cell = gridEl.children[index];
  if (!cell) return;
  const bounds = cell.getBoundingClientRect();
  views.set(
    index,
    panBy(views.get(index) ?? IDENTITY_VIEW, dxFraction * bounds.width, dyFraction * bounds.height),
  );
  applyCoverFit(cell);
}

function applyCellMenuAction(index, action) {
  switch (action) {
    case "togglePlay":
      togglePlayForCell(index);
      break;
    case "toggleLock":
      toggleLockedIndex(index);
      break;
    case "restart":
      restartCell(index);
      break;
    case "resetZoom":
      resetCellZoom(index);
      break;
    case "replaceReserve":
      handlePlayerError(index);
      break;
    default:
      wlog(`applyCellMenuAction: unknown action ${action}`);
  }
}

// Every user action reachable from the header or a cell -- a real DOM event
// on THIS page, or one relayed from /layout-control over BroadcastChannel --
// funnels through here. That symmetry is the whole point: a control-window
// message and a local click must produce identical effects, so this is the
// only place either kind is handled. publishSnapshot() (added when
// BroadcastChannel wiring lands) is a no-op until then.
async function applyIntent(intent) {
  switch (intent.type) {
    case "play":
      startAll();
      break;
    case "pause":
      pauseAll();
      break;
    case "muteToggle":
      toggleMute();
      break;
    case "rewind":
      rewindAll();
      break;
    case "shuffle":
      shuffleWall();
      break;
    case "resetView":
      resetAllViews();
      break;
    case "newQuery":
      await requestNewQuery(intent.prompt ?? null);
      break;
    case "hoverUnmuteToggle":
      hoverUnmuteCheckbox.checked = intent.checked;
      setAudibleCell(null);
      applyMuteStateToAll();
      break;
    case "followToggle":
      followCheckbox.checked = intent.checked;
      break;
    case "cellHoverEnter":
      if (hoverUnmuteCheckbox.checked) setAudibleCell(intent.index);
      break;
    case "cellHoverLeave":
      if (hoverUnmuteCheckbox.checked) setAudibleCell(null);
      break;
    case "cellWheel":
      applyCellWheel(intent.index, intent.deltaY, intent.x, intent.y);
      break;
    case "cellDragStart":
      // Purely a cursor affordance; kept for parity with a locally-driven drag.
      gridEl.children[intent.index]?.setAttribute("data-dragging", "true");
      break;
    case "cellDragMove":
      applyCellPan(intent.index, intent.dx, intent.dy);
      break;
    case "cellDragEnd":
      gridEl.children[intent.index]?.removeAttribute("data-dragging");
      break;
    case "cellDblclick":
      toggleLockedIndex(intent.index);
      break;
    case "cellMenuAction":
      applyCellMenuAction(intent.index, intent.action);
      break;
    default:
      wlog(`applyIntent: unknown intent type ${intent.type}`);
      return;
  }
}

gridEl.addEventListener(
  "wheel",
  (event) => {
    const cell = event.target.closest(".cell");
    if (!cell || cell.dataset.empty === "true") return;
    event.preventDefault();
    const index = [...gridEl.children].indexOf(cell);
    const bounds = cell.getBoundingClientRect();
    applyIntent({
      type: "cellWheel",
      index,
      deltaY: event.deltaY,
      x: bounds.width ? (event.clientX - bounds.left) / bounds.width : 0,
      y: bounds.height ? (event.clientY - bounds.top) / bounds.height : 0,
    });
  },
  { passive: false },
);

// Click and drag to pan. The iframe has pointer-events:none, so the cell gets
// the pointer -- the same reason the context menu and hover-unmute work.
let drag = null;

gridEl.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return; // left button only; right opens the menu
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  const index = [...gridEl.children].indexOf(cell);
  drag = { cell, index, x: event.clientX, y: event.clientY };
  try {
    cell.setPointerCapture(event.pointerId);
  } catch {
    // Capture is a convenience; the document-level pointerup still ends it.
  }
  applyIntent({ type: "cellDragStart", index });
});

gridEl.addEventListener("pointermove", (event) => {
  if (!drag) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  const bounds = drag.cell.getBoundingClientRect();
  drag.x = event.clientX;
  drag.y = event.clientY;
  applyIntent({
    type: "cellDragMove",
    index: drag.index,
    dx: bounds.width ? dx / bounds.width : 0,
    dy: bounds.height ? dy / bounds.height : 0,
  });
});

function endDrag(event) {
  if (!drag) return;
  try {
    drag.cell.releasePointerCapture(event.pointerId);
  } catch {
    // Already released, or never captured.
  }
  applyIntent({ type: "cellDragEnd", index: drag.index });
  drag = null;
}

// On the document, not the grid: a drag that leaves the window must still end.
document.addEventListener("pointerup", endDrag);
document.addEventListener("pointercancel", endDrag);

// New videos from the same query. One search returns 50 for eight cells, so
// there is plenty behind the wall -- and reshuffling what we already paid for
// costs nothing. Not persisted: a reload restores the server's ranked order,
// which is relevance, country spread and stills-to-the-back.
shuffleButton.addEventListener("click", () => applyIntent({ type: "shuffle" }));

resetViewButton.addEventListener("click", () => applyIntent({ type: "resetView" }));

// Double-click to hold the audio on one cell. Again on the same cell turns it
// off; on a different cell it moves there. Unlike hover, this survives the
// cursor leaving the grid -- which is the point of locking.
gridEl.addEventListener("dblclick", (event) => {
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  applyIntent({ type: "cellDblclick", index: [...gridEl.children].indexOf(cell) });
});

gridEl.addEventListener("pointerover", (event) => {
  if (!hoverUnmuteCheckbox.checked) return;
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  applyIntent({ type: "cellHoverEnter", index: [...gridEl.children].indexOf(cell) });
});

// pointerleave on the grid, not per cell: moving between adjacent cells would
// otherwise blip the audio off and on again between them.
gridEl.addEventListener("pointerleave", () => {
  if (!hoverUnmuteCheckbox.checked) return;
  applyIntent({ type: "cellHoverLeave" });
});

refreshMuteButton();
refreshControls();

connectSocket({
  onReconnect: resync,
  // Config is the only thing broadcast. Nothing pushes a video set any more:
  // the server does not know what query any given browser is watching, so a
  // wall only ever changes its own videos -- on its own resync, or off its own
  // New query. Deciding whether a config change means this browser has to
  // refetch is therefore the client's job, done right here.
  onMessage: (message) => {
    wlog(`socket message type=${message.type}`);
    if (message.type !== "config") return;
    const previous = config;
    const change = classifyConfigChange(previous, message.config);
    config = message.config;
    wlog(`config pushed: change=${change}`);
    if (change === "rebuild") rebuild();
    else if (change === "in-place") applyInPlace();
    // Someone typed a query on the config page: that is an explicit
    // override and it beats whatever this browser had stored.
    if (overridesStoredQuery(previous, message.config)) {
      wlog("config's query field was edited -- that overrides this browser's stored query");
      clearQuery();
      clearWall();
    }
    // Pass this page's own computeLayout so a layout-only change (total,
    // max_per_screen, per-screen counts -- none of which touch config.grid)
    // is recognised as structural on /layout without making / (whose
    // computeLayout is defaultComputeLayout, totalCells === cellCount(grid))
    // refetch on anything it did not already refetch on.
    if (needsRefetch(previous, message.config, (config) => computeLayout(config).totalCells)) {
      wlog("config change affects the search -- refetching");
      resync();
    }
  },
});
}
