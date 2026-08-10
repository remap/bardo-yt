import {
  splitSlots,
  substituteFailedSlot,
  classifyConfigChange,
  cellCount,
  coverRect,
} from "./grid-logic.js";
import { connectSocket } from "./socket.js";

const gridEl = document.getElementById("grid");
const statusEl = document.getElementById("status");
const playButton = document.getElementById("play");
const pauseButton = document.getElementById("pause");
const muteButton = document.getElementById("mute");

let config = null;
let slotState = { slots: [], reserves: [] };
let players = [];
let apiReady = false;
let hasPlayed = false;
let paused = false;
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

function applyCoverFit(cell) {
  const iframe = cell?.querySelector("iframe");
  if (!iframe) return;
  const { width, height } = cell.getBoundingClientRect();
  if (!width || !height) return;
  const rect = coverRect(width, height);
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
        applyCoverFit(event.target.getIframe()?.closest(".cell"));
        // Do not resurrect playback the user deliberately paused.
        if (hasPlayed && !paused && config.playback.autoplay_on_change) {
          event.target.playVideo();
        }
      },
      onError: () => handlePlayerError(index),
      onStateChange: (event) => {
        // The captions module only exists once playback has begun, so the
        // onReady attempt above is often too early to have had any effect.
        if (event.data === YT.PlayerState.PLAYING) suppressCaptions(event.target);
        if (event.data === YT.PlayerState.ENDED && config.playback.loop) {
          event.target.seekTo(config.playback.start_offset);
          event.target.playVideo();
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
  destroyPlayers();
  const cells = buildCells();
  players = cells.map((cell, index) => makePlayer(cell, index));
  // Exposed for the browser smoke test to assert against.
  window.__players = players;
}

function applyInPlace() {
  for (const player of players) {
    if (!player?.seekTo) continue;
    player.seekTo(config.playback.start_offset);
  }
}

function applyVideos(message) {
  slotState = splitSlots(message.video_ids.concat(message.reserves), cellCount(config.grid));
  const notes = {
    quota_exceeded_stale: "quota spent — showing cached results",
    budget_exceeded_stale: "daily budget spent — showing cached results",
    no_results: "no results for that query",
  };
  const spent = message.units_spent_today ?? 0;
  const limit = message.daily_limit_units ?? 0;
  const budgetText = limit ? ` · ${spent}/${limit} units today` : ` · ${spent} units today`;
  setStatus(
    notes[message.note] ??
      `“${message.query}” — ${message.video_ids.filter(Boolean).length} playing · ${
        message.from_cache ? "cached" : "fresh search"
      }${budgetText}`,
    message.note ? "error" : "",
  );
  rebuild();
}

// Generation happens once per page load. resync() also runs on every WebSocket
// reconnect, and a reconnect is a network hiccup -- spending 100 units each
// time the wifi blinks would drain the day's budget with nothing to show.
let generatedThisPageLoad = false;
let seededMuteFromConfig = false;

async function resync() {
  config = await (await fetch("/api/config")).json();

  // Seed once. On a later reconnect the button, not the file, is the truth.
  if (!seededMuteFromConfig) {
    seededMuteFromConfig = true;
    muted = config.playback.muted;
    refreshMuteButton();
  }

  if (config.query_generation?.enabled && !generatedThisPageLoad) {
    generatedThisPageLoad = true;
    setStatus("inventing a query…");
    const generated = await fetch("/api/new-query", { method: "POST" });
    if (generated.ok) {
      applyVideos(await generated.json());
      return;
    }
    // Fall through to the existing query: a failed generation should leave the
    // wall working, not blank.
    const body = await generated.json().catch(() => ({}));
    setStatus(`query generation failed: ${body.detail ?? generated.status}`, "error");
  }

  const videosResponse = await fetch("/api/videos");
  if (!videosResponse.ok) {
    const body = await videosResponse.json().catch(() => ({}));
    setStatus(body.detail ?? `error ${videosResponse.status}`, "error");
    return;
  }
  applyVideos(await videosResponse.json());
}

playButton.addEventListener("click", () => {
  hasPlayed = true;
  paused = false;
  // Eight muted players starting at once is exactly what browsers throttle.
  // This click is the user gesture that makes them all start.
  for (const player of players) player?.playVideo?.();
});

pauseButton.addEventListener("click", () => {
  paused = true;
  for (const player of players) player?.pauseVideo?.();
});

muteButton.addEventListener("click", () => {
  muted = !muted;
  refreshMuteButton();
  // Unmuting eight players at once is only permitted off a user gesture --
  // this click is it. Doing it any other way leaves some players silent.
  for (const player of players) applyMuteState(player);
});

refreshMuteButton();

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
