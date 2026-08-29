import { videoUrl, formatTimecode } from "./grid-logic.js";

const CHANNEL_NAME = "yt-matrix-layout-control";
const STALE_AFTER_MS = 2500;

const channel = new BroadcastChannel(CHANNEL_NAME);

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

function send(intent) {
  channel.postMessage(intent);
}

// How long a message this page wrote itself ("copied URL", "clipboard
// blocked") is protected from the next snapshot. /layout's heartbeat arrives
// every second, so without this window a copy confirmation can be painted over
// before it has been read -- and it is the only feedback the action gives.
const LOCAL_STATUS_HOLD_MS = 1500;

let localStatusAt = 0;

function setStatus(text, state = "") {
  statusEl.textContent = text;
  statusEl.dataset.state = state;
}

// Status this page owns, rather than /layout's own line relayed through.
function setLocalStatus(text, state = "") {
  localStatusAt = Date.now();
  setStatus(text, state);
}

// BroadcastChannel only bridges tabs/windows within the SAME browser process
// and profile -- it cannot reach a /layout running in a different browser, a
// different profile, or (the production case) the broadcaster's own headed-
// but-off-screen Chrome instance. Say so loudly here, since the only other
// symptom is this page sitting on "waiting" forever with nothing to click.
console.log(
  `[layout-control] connecting on BroadcastChannel("${CHANNEL_NAME}"). ` +
    "This only reaches a /layout tab open in this SAME browser process/profile " +
    "-- not a different browser, a different profile, or the NDI broadcaster's " +
    "own off-screen Chrome instance, which is a separate process even if it is " +
    "the same browser application.",
);
setStatus("waiting for /layout to connect… (must be open in this same browser)", "busy");

// The last snapshot's cells, kept only so the context menu can be built
// without a round trip -- every mutating action still goes back over the
// channel as an intent.
let latestCells = [];
// The last snapshot's `global` block, for the same reason: the reserve count
// the menu reports lives there.
let latestGlobal = {};
let staleTimer = null;

function renderFromSnapshot(snapshot) {
  latestCells = snapshot.cells;
  latestGlobal = snapshot.global;
  const g = snapshot.global;
  if (Date.now() - localStatusAt >= LOCAL_STATUS_HOLD_MS) setStatus(g.status, g.statusState);
  audioEl.textContent = g.audioIndicatorText;
  audioEl.dataset.locked = String(g.audioLocked);
  muteButton.textContent = g.muted ? "Unmute" : "Mute";
  muteButton.dataset.muted = String(g.muted);
  playButton.disabled = g.playDisabled;
  hoverUnmuteCheckbox.checked = g.hoverUnmuteChecked;
  followCheckbox.checked = g.followChecked;
  newQueryButton.hidden = !g.newQueryVisible;
  newQueryButton.disabled = g.newQueryDisabled;

  if (gridEl.children.length !== snapshot.cells.length) {
    gridEl.replaceChildren();
    for (let i = 0; i < snapshot.cells.length; i += 1) {
      const cell = document.createElement("div");
      cell.className = "cell";
      const label = document.createElement("span");
      label.className = "label";
      cell.appendChild(label);
      gridEl.appendChild(cell);
    }
  }
  snapshot.cells.forEach((cellData, index) => {
    const cell = gridEl.children[index];
    if (!cell) return;
    if (cellData.rect) Object.assign(cell.style, cellData.rect);
    cell.dataset.empty = cellData.empty ? "true" : "false";
    cell.dataset.audible = String(cellData.audible);
    cell.dataset.locked = String(cellData.locked);
    if (cellData.videoId) cell.dataset.videoId = cellData.videoId;
    else delete cell.dataset.videoId;
    cell.querySelector(".label").textContent = cellData.title ?? "";
  });

  clearTimeout(staleTimer);
  staleTimer = setTimeout(() => {
    setStatus(
      "no update from /layout in a while — is it open in this same browser?",
      "error",
    );
  }, STALE_AFTER_MS);
}

channel.addEventListener("message", (event) => {
  if (event.data?.type === "snapshot") renderFromSnapshot(event.data);
});

playButton.addEventListener("click", () => send({ type: "play" }));
pauseButton.addEventListener("click", () => send({ type: "pause" }));
muteButton.addEventListener("click", () => send({ type: "muteToggle" }));
rewindButton.addEventListener("click", () => send({ type: "rewind" }));
shuffleButton.addEventListener("click", () => send({ type: "shuffle" }));
resetViewButton.addEventListener("click", () => send({ type: "resetView" }));
hoverUnmuteCheckbox.addEventListener("change", () =>
  send({ type: "hoverUnmuteToggle", checked: hoverUnmuteCheckbox.checked }),
);
followCheckbox.addEventListener("change", () =>
  send({ type: "followToggle", checked: followCheckbox.checked }),
);
promptInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || newQueryButton.disabled || newQueryButton.hidden) return;
  event.preventDefault();
  newQueryButton.click();
});
newQueryButton.addEventListener("click", () => {
  const prompt = promptInput.value.trim();
  // Disable optimistically. /layout's dispatcher is the real guard against a
  // double 100-unit spend, but its answer only arrives with the next snapshot
  // -- up to a heartbeat away -- and a button that stays live for a second
  // after a click invites the second click.
  newQueryButton.disabled = true;
  send({ type: "newQuery", prompt: prompt || null });
});

function cellIndexOf(target) {
  const cell = target.closest(".cell");
  return cell ? [...gridEl.children].indexOf(cell) : -1;
}

gridEl.addEventListener("pointerover", (event) => {
  const index = cellIndexOf(event.target);
  if (index >= 0) send({ type: "cellHoverEnter", index });
});
gridEl.addEventListener("pointerleave", () => send({ type: "cellHoverLeave" }));

gridEl.addEventListener(
  "wheel",
  (event) => {
    const cell = event.target.closest(".cell");
    if (!cell) return;
    event.preventDefault();
    const bounds = cell.getBoundingClientRect();
    send({
      type: "cellWheel",
      index: [...gridEl.children].indexOf(cell),
      deltaY: event.deltaY,
      x: bounds.width ? (event.clientX - bounds.left) / bounds.width : 0,
      y: bounds.height ? (event.clientY - bounds.top) / bounds.height : 0,
    });
  },
  { passive: false },
);

let drag = null;
gridEl.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  const cell = event.target.closest(".cell");
  if (!cell) return;
  const index = [...gridEl.children].indexOf(cell);
  drag = { cell, index, x: event.clientX, y: event.clientY };
  try {
    cell.setPointerCapture(event.pointerId);
  } catch {
    // Convenience only.
  }
  send({ type: "cellDragStart", index });
});
gridEl.addEventListener("pointermove", (event) => {
  if (!drag) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  const bounds = drag.cell.getBoundingClientRect();
  drag.x = event.clientX;
  drag.y = event.clientY;
  send({
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
    // Already released.
  }
  send({ type: "cellDragEnd", index: drag.index });
  drag = null;
}
document.addEventListener("pointerup", endDrag);
document.addEventListener("pointercancel", endDrag);

gridEl.addEventListener("dblclick", (event) => {
  const index = cellIndexOf(event.target);
  if (index >= 0) send({ type: "cellDblclick", index });
});

// --- context menu, built from the last snapshot ----------------------------
//
// Copy/open actions execute right here -- the snapshot already carries
// everything they need. Everything that mutates a real player only the
// broadcast page owns goes back over the channel as a cellMenuAction intent.

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    setLocalStatus(`copied ${label}`, "busy");
  } catch {
    setLocalStatus("clipboard blocked by the browser", "error");
  }
}

function menuItemsFor(cellData) {
  if (!cellData || cellData.empty) return [];
  const { index, videoId, title, currentTime, locked, playing, zoom } = cellData;
  const name = title ?? videoId;
  const relay = (action) => () => send({ type: "cellMenuAction", index, action });
  return [
    {
      label: "Copy video URL at time",
      hint: formatTimecode(currentTime),
      run: () => copyText(videoUrl(videoId, currentTime), `URL at ${formatTimecode(currentTime)}`),
    },
    { label: "Copy video URL", run: () => copyText(videoUrl(videoId), "URL") },
    { label: "Copy video ID", hint: videoId, run: () => copyText(videoId, "video ID") },
    { label: "Copy title", run: () => copyText(name, "title") },
    {
      label: "Open on YouTube at time",
      run: () => window.open(videoUrl(videoId, currentTime), "_blank", "noopener"),
    },
    { label: playing ? "Pause this cell" : "Play this cell", run: relay("togglePlay") },
    {
      label: locked ? "Unlock audio" : "Lock audio to this cell",
      hint: "double-click",
      run: relay("toggleLock"),
    },
    { label: "Restart this cell", run: relay("restart") },
    { label: "Reset zoom", hint: `${(zoom ?? 1).toFixed(2)}×`, run: relay("resetZoom") },
    {
      label: "Replace with next reserve",
      hint: `${latestGlobal.reservesLeft ?? 0} left`,
      run: relay("replaceReserve"),
    },
  ];
}

function closeMenu() {
  menuEl.hidden = true;
}

function openMenu(index, x, y) {
  const cellData = latestCells[index];
  const items = menuItemsFor(cellData);
  if (items.length === 0) return;

  menuEl.replaceChildren();
  const head = document.createElement("div");
  head.className = "head";
  head.textContent = cellData.title ?? cellData.videoId;
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

  menuEl.hidden = false;
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
  const index = cellIndexOf(event.target);
  if (index < 0) return;
  event.preventDefault();
  openMenu(index, event.clientX, event.clientY);
});
document.addEventListener("click", (event) => {
  if (!menuEl.hidden && !menuEl.contains(event.target)) closeMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMenu();
});
