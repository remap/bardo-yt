import {
  splitSlots,
  substituteFailedSlot,
  classifyConfigChange,
  cellCount,
} from "./grid-logic.js";
import { connectSocket } from "./socket.js";

const gridEl = document.getElementById("grid");
const statusEl = document.getElementById("status");
const playButton = document.getElementById("play");

let config = null;
let slotState = { slots: [], reserves: [] };
let players = [];
let apiReady = false;
let hasPlayed = false;

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
    return { cell, mount, videoId };
  });
}

function makePlayer({ mount, videoId }, index) {
  if (!videoId) return null;
  return new YT.Player(mount, {
    videoId,
    playerVars: {
      mute: 1,
      controls: 0,
      // Neither rel=0 nor modestbranding removes YouTube's chrome any more --
      // see spec section 5.1. These are the best available, not a clean frame.
      rel: 0,
      playsinline: 1,
      start: config.playback.start_offset,
    },
    events: {
      onReady: (event) => {
        event.target.mute();
        if (hasPlayed && config.playback.autoplay_on_change) event.target.playVideo();
      },
      onError: () => handlePlayerError(index),
      onStateChange: (event) => {
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
    no_results: "no results for that query",
  };
  setStatus(
    notes[message.note] ??
      `${message.video_ids.filter(Boolean).length} playing · ${
        message.from_cache ? "cached" : "fresh search"
      }`,
    message.note ? "error" : "",
  );
  rebuild();
}

async function resync() {
  const [configResponse, videosResponse] = await Promise.all([
    fetch("/api/config"),
    fetch("/api/videos"),
  ]);
  config = await configResponse.json();

  if (!videosResponse.ok) {
    const body = await videosResponse.json().catch(() => ({}));
    setStatus(body.detail ?? `error ${videosResponse.status}`, "error");
    return;
  }

  applyVideos(await videosResponse.json());
}

playButton.addEventListener("click", () => {
  hasPlayed = true;
  // Eight muted players starting at once is exactly what browsers throttle.
  // This click is the user gesture that makes them all start.
  for (const player of players) player?.playVideo?.();
});

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
