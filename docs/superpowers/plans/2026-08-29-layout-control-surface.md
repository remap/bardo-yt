# Layout Control Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, ordinary browser tab (`/layout-control`) that mirrors `/layout`'s controls and per-cell interactions over `BroadcastChannel`, so an operator can drive the broadcast-clean `/layout` page (header hidden by CSS) without touching it directly.

**Architecture:** Every local event handler in `wall-engine.js` is refactored to construct a small "intent" object and hand it to one `applyIntent()` dispatcher, instead of mutating state inline — the same function then runs whether the intent came from a real DOM event or arrived over a `BroadcastChannel`. `wall-engine.js` also gains an opt-in snapshot publisher (per-cell + global state) so a control page can render without any state of its own. `/layout-control` is a pure consumer: no independent fetch of config or geometry, everything comes from snapshots.

**Tech Stack:** Vanilla JS ES modules (as the rest of `static/`), `BroadcastChannel` (native browser API, no library), Playwright for the two-page browser test.

**Spec:** `docs/superpowers/specs/2026-08-29-layout-control-surface-design.md`

## Global Constraints

- **Zero behavior change on `/` and on `/layout` with no control tab open.** `tests/test_player_smoke.py` and `tests/test_layout_smoke.py` (both marked `browser`) must pass, unmodified, after every task in this plan.
- **`BroadcastChannel` only, no server involvement.** It connects tabs in the same browser/profile/machine, which is already required for the NDI broadcaster to work. No new backend route carries control traffic.
- Coordinates in intents that describe a pointer position are normalized (0..1, relative to the *sending* window's own cell rect) — the receiving side always converts using its own `getBoundingClientRect()`. Wheel `deltaY` travels as-is (only its sign matters to `nextZoom`).
- Every mutating context-menu action becomes a `cellMenuAction` intent; every read-only one (copy/open) executes locally wherever it was clicked, using cached snapshot data.
- `uv run ruff check . && uv run ruff format .` is not applicable to the JS-only tasks in this plan (no Python touched until none) but `node --check <file>` must be run on every JS file created or modified, and the two existing browser suites plus the new one must all pass before every commit that touches `wall-engine.js`.

---

### Task 1: Extract named global-action functions

**Files:**
- Modify: `static/wall-engine.js`

**Interfaces:**
- Consumes: nothing new.
- Produces: `rewindAll()`, `toggleMute()`, `shuffleWall()`, `resetAllViews()` — each a pure extraction of an existing inline listener body, called by that same listener and by nothing else yet (Task 3 wires them into `applyIntent`).

- [ ] **Step 1: Extract `rewindAll()`**

Replace:
```js
rewindButton.addEventListener("click", () => {
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
});
```
with:
```js
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

rewindButton.addEventListener("click", rewindAll);
```

- [ ] **Step 2: Extract `toggleMute()`**

Replace:
```js
muteButton.addEventListener("click", () => {
  muted = !muted;
  refreshMuteButton();
  // Unmuting eight players at once is only permitted off a user gesture --
  // this click is it. Doing it any other way leaves some players silent.
  applyMuteStateToAll();
});
```
with:
```js
function toggleMute() {
  muted = !muted;
  refreshMuteButton();
  // Unmuting eight players at once is only permitted off a user gesture --
  // this click is it. Doing it any other way leaves some players silent.
  applyMuteStateToAll();
}

muteButton.addEventListener("click", toggleMute);
```

- [ ] **Step 3: Extract `shuffleWall()`**

Replace:
```js
shuffleButton.addEventListener("click", () => {
  const wasPlaying = wantPlaying;
  slotState = shuffleSlots(
    [...slotState.slots, ...slotState.reserves],
    computeLayout(config).totalCells,
  );
  rebuild();
  // Shuffling is not a new query, so it should not silently stop the wall.
  // rebuild() clears wantPlaying via pre-roll; put it back if it was running.
  wantPlaying = wasPlaying;
});
```
with:
```js
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

shuffleButton.addEventListener("click", shuffleWall);
```

- [ ] **Step 4: Extract `resetAllViews()`**

Replace:
```js
resetViewButton.addEventListener("click", () => {
  views.clear();
  for (const cell of gridEl.children) {
    delete cell.dataset.zoomed;
    applyCoverFit(cell);
  }
});
```
with:
```js
function resetAllViews() {
  views.clear();
  for (const cell of gridEl.children) {
    delete cell.dataset.zoomed;
    applyCoverFit(cell);
  }
}

resetViewButton.addEventListener("click", resetAllViews);
```

- [ ] **Step 5: Verify no behavior change**

```bash
node --check static/wall-engine.js
node --test 'static/*.test.mjs'
uv run pytest tests/test_player_smoke.py -m browser -v
uv run pytest tests/test_layout_smoke.py -m browser -v
```
Expected: identical pass counts to before this task (142 node tests unaffected since none touch `wall-engine.js` directly, 40 browser tests for `/`, 3 for `/layout`).

- [ ] **Step 6: Commit**

```bash
git add static/wall-engine.js
git commit -m "refactor: name the global header-control actions"
```

---

### Task 2: Extract named per-cell action functions

**Files:**
- Modify: `static/wall-engine.js`

**Interfaces:**
- Consumes: nothing new.
- Produces: `togglePlayForCell(index)`, `toggleLockedIndex(index)`, `restartCell(index)`, `resetCellZoom(index)` — each an extraction of logic currently duplicated or inlined between the `dblclick` listener and the context menu's `run` callbacks.

- [ ] **Step 1: Add the four functions**

Insert immediately before `function playerForCell(cell) {`:

```js
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
```

- [ ] **Step 2: Use them in the context menu**

In `menuItems(cell)`, replace the four `run` callbacks:

```js
// Old:
    {
      label: playing ? "Pause this cell" : "Play this cell",
      run: () => (playing ? player?.pauseVideo?.() : player?.playVideo?.()),
    },
```
```js
// New:
    {
      label: playing ? "Pause this cell" : "Play this cell",
      run: () => togglePlayForCell(index),
    },
```

```js
// Old:
    {
      label: lockedIndex === index ? "Unlock audio" : "Lock audio to this cell",
      hint: "double-click",
      run: () => {
        lockedIndex = lockedIndex === index ? null : index;
        for (const other of gridEl.children) delete other.dataset.locked;
        if (lockedIndex !== null) cell.dataset.locked = "true";
        applyMuteStateToAll();
      },
    },
```
```js
// New:
    {
      label: lockedIndex === index ? "Unlock audio" : "Lock audio to this cell",
      hint: "double-click",
      run: () => toggleLockedIndex(index),
    },
```

```js
// Old:
    {
      label: "Restart this cell",
      run: () => player?.seekTo?.(config.playback.start_offset, true),
    },
```
```js
// New:
    {
      label: "Restart this cell",
      run: () => restartCell(index),
    },
```

```js
// Old:
    {
      label: "Reset zoom",
      hint: `${(viewFor(cell).zoom ?? 1).toFixed(2)}×`,
      run: () => {
        views.delete(index);
        cell.dataset.zoomed = "false";
        applyCoverFit(cell);
      },
    },
```
```js
// New:
    {
      label: "Reset zoom",
      hint: `${(viewFor(cell).zoom ?? 1).toFixed(2)}×`,
      run: () => resetCellZoom(index),
    },
```

- [ ] **Step 3: Use `toggleLockedIndex` in the dblclick listener**

Replace:
```js
gridEl.addEventListener("dblclick", (event) => {
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  const index = [...gridEl.children].indexOf(cell);
  lockedIndex = lockedIndex === index ? null : index;

  for (const other of gridEl.children) delete other.dataset.locked;
  if (lockedIndex !== null) cell.dataset.locked = "true";
  applyMuteStateToAll();
});
```
with:
```js
gridEl.addEventListener("dblclick", (event) => {
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  toggleLockedIndex([...gridEl.children].indexOf(cell));
});
```

- [ ] **Step 4: Verify no behavior change**

```bash
node --check static/wall-engine.js
node --test 'static/*.test.mjs'
uv run pytest tests/test_player_smoke.py -m browser -v
uv run pytest tests/test_layout_smoke.py -m browser -v
```
Expected: same pass counts as Task 1's verification — in particular `test_double_click_locks_audio_to_one_cell`, `test_double_clicking_another_cell_moves_the_lock`, `test_double_clicking_the_same_cell_turns_the_lock_off`, `test_reset_zoom_is_offered_and_works` and `test_right_click_offers_copy_url_at_time` in `tests/test_player_smoke.py` all still pass — they exercise exactly the code this task moved.

- [ ] **Step 5: Commit**

```bash
git add static/wall-engine.js
git commit -m "refactor: name the per-cell actions shared by dblclick and the context menu"
```

---

### Task 3: `applyIntent()` — the single dispatch point

This is the highest-risk task in the plan: every local listener changes shape. The binding requirement is that the *effect* of every listener is unchanged; only how it's invoked changes (directly, or through one more function call).

**Files:**
- Modify: `static/wall-engine.js`

**Interfaces:**
- Consumes: `rewindAll`, `toggleMute`, `shuffleWall`, `resetAllViews` (Task 1); `togglePlayForCell`, `toggleLockedIndex`, `restartCell`, `resetCellZoom` (Task 2).
- Produces: `applyIntent(intent)` (async), `queueZoom(cell, index, width, height, deltaY, x, y)`, `applyCellWheel(index, deltaY, xFraction, yFraction)`, `applyCellPan(index, dxFraction, dyFraction)`, `applyCellMenuAction(index, action)`. The full intent vocabulary is exactly the one in the design spec's "Intents" section.

- [ ] **Step 1: Extract `queueZoom` from the wheel listener, and add the three new dispatch helpers**

Insert immediately before `gridEl.addEventListener("wheel", ...)` (i.e., right after `let pendingZoom = null;`):

```js
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
```

- [ ] **Step 2: Rewire the wheel listener**

Replace:
```js
gridEl.addEventListener(
  "wheel",
  (event) => {
    const cell = event.target.closest(".cell");
    if (!cell || cell.dataset.empty === "true") return;
    event.preventDefault();

    const index = [...gridEl.children].indexOf(cell);
    const bounds = cell.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;

    if (pendingZoom && pendingZoom.index === index) {
      // Same cell, same frame: sum the deltas so no scrolling is lost, and
      // track the cursor to wherever it ended up.
      pendingZoom.deltaY += event.deltaY;
      pendingZoom.x = x;
      pendingZoom.y = y;
      return;
    }

    // A different cell mid-frame: land the queued one first rather than
    // dropping it.
    if (pendingZoom) flushZoom();

    pendingZoom = {
      cell,
      index,
      width: bounds.width,
      height: bounds.height,
      deltaY: event.deltaY,
      x,
      y,
    };
    requestAnimationFrame(flushZoom);
  },
  { passive: false },
);
```
with:
```js
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
```

- [ ] **Step 3: Rewire drag (pointerdown / pointermove / endDrag)**

Replace:
```js
gridEl.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return; // left button only; right opens the menu
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  drag = {
    cell,
    index: [...gridEl.children].indexOf(cell),
    x: event.clientX,
    y: event.clientY,
  };
  cell.dataset.dragging = "true";
  try {
    cell.setPointerCapture(event.pointerId);
  } catch {
    // Capture is a convenience; the document-level pointerup still ends it.
  }
});

gridEl.addEventListener("pointermove", (event) => {
  if (!drag) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  drag.x = event.clientX;
  drag.y = event.clientY;
  views.set(drag.index, panBy(views.get(drag.index) ?? IDENTITY_VIEW, dx, dy));
  applyCoverFit(drag.cell);
});

function endDrag(event) {
  if (!drag) return;
  try {
    drag.cell.releasePointerCapture(event.pointerId);
  } catch {
    // Already released, or never captured.
  }
  delete drag.cell.dataset.dragging;
  drag = null;
}
```
with:
```js
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
```

- [ ] **Step 4: Rewire dblclick and hover**

Replace:
```js
gridEl.addEventListener("dblclick", (event) => {
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  toggleLockedIndex([...gridEl.children].indexOf(cell));
});

gridEl.addEventListener("pointerover", (event) => {
  if (!hoverUnmuteCheckbox.checked) return;
  const cell = event.target.closest(".cell");
  if (!cell || cell.dataset.empty === "true") return;
  setAudibleCell([...gridEl.children].indexOf(cell));
});

// pointerleave on the grid, not per cell: moving between adjacent cells would
// otherwise blip the audio off and on again between them.
gridEl.addEventListener("pointerleave", () => {
  if (!hoverUnmuteCheckbox.checked) return;
  setAudibleCell(null);
});
```
with:
```js
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
```

- [ ] **Step 5: Rewire the header buttons and checkboxes**

Replace:
```js
playButton.addEventListener("click", startAll);
pauseButton.addEventListener("click", pauseAll);

rewindButton.addEventListener("click", rewindAll);

muteButton.addEventListener("click", toggleMute);
```
with:
```js
playButton.addEventListener("click", () => applyIntent({ type: "play" }));
pauseButton.addEventListener("click", () => applyIntent({ type: "pause" }));
rewindButton.addEventListener("click", () => applyIntent({ type: "rewind" }));
muteButton.addEventListener("click", () => applyIntent({ type: "muteToggle" }));
```

Replace:
```js
hoverUnmuteCheckbox.addEventListener("change", () => {
  // Leaving the mode must not strand a cell audible or the whole wall silent.
  setAudibleCell(null);
  applyMuteStateToAll();
});
```
with:
```js
hoverUnmuteCheckbox.addEventListener("change", () =>
  applyIntent({ type: "hoverUnmuteToggle", checked: hoverUnmuteCheckbox.checked }),
);
```

Replace:
```js
shuffleButton.addEventListener("click", shuffleWall);
```
with:
```js
shuffleButton.addEventListener("click", () => applyIntent({ type: "shuffle" }));
```

Replace:
```js
resetViewButton.addEventListener("click", resetAllViews);
```
with:
```js
resetViewButton.addEventListener("click", () => applyIntent({ type: "resetView" }));
```

Replace:
```js
newQueryButton.addEventListener("click", async () => {
  const prompt = promptInput.value.trim();
  promptInput.disabled = true;
  try {
    await requestNewQuery(prompt || null);
  } finally {
    promptInput.disabled = false;
  }
});
```
with:
```js
newQueryButton.addEventListener("click", async () => {
  const prompt = promptInput.value.trim();
  promptInput.disabled = true;
  try {
    await applyIntent({ type: "newQuery", prompt: prompt || null });
  } finally {
    promptInput.disabled = false;
  }
});
```

`followCheckbox` has no existing listener to replace — this task does not add one (nothing currently reacts live to it; `/layout-control`'s own checkbox will drive it via a `followToggle` intent once Task 6 exists, and `applyIntent`'s `followToggle` case already handles that from Step 1).

- [ ] **Step 6: Verify no behavior change**

```bash
node --check static/wall-engine.js
node --test 'static/*.test.mjs'
uv run pytest tests/ -v
uv run pytest tests/test_player_smoke.py -m browser -v
uv run pytest tests/test_layout_smoke.py -m browser -v
```
Expected: every suite passes at its pre-existing count — this is the proof that routing every listener through one more function call changed nothing observable. Pay particular attention to `test_scroll_wheel_zooms_toward_the_pointer`, `test_zooming_out_pulls_back_to_the_whole_frame`, `test_drag_pans_a_zoomed_cell`, and `test_dragging_cannot_open_a_gap` — these exercise exactly the normalize-then-rescale round trip Step 2/3 introduced, and on `/` that round trip must be a no-op (dividing and re-multiplying by the same cell's own bounds).

- [ ] **Step 7: Commit**

```bash
git add static/wall-engine.js
git commit -m "refactor: route every local interaction through one applyIntent dispatcher"
```

---

### Task 4: Snapshot publishing + `BroadcastChannel` wiring

**Files:**
- Modify: `static/wall-engine.js`

**Interfaces:**
- Consumes: `applyIntent` (Task 3), `computeLayout` (existing).
- Produces: `startWall({ computeLayout, controlChannel })` — `controlChannel` is `null` by default (nothing new activates) or a channel-name string. `buildSnapshot()`, `publishSnapshot()`.

- [ ] **Step 1: Add the option and the snapshot machinery**

Change the `startWall` signature:
```js
// Old:
export function startWall({ computeLayout = defaultComputeLayout } = {}) {
```
```js
// New:
export function startWall({ computeLayout = defaultComputeLayout, controlChannel = null } = {}) {
```

Insert, immediately before `function refreshControls() {`:

```js
// null until startWall({ controlChannel }) opens one -- publishSnapshot()
// below is a no-op until then, so every call site can call it unconditionally
// without checking whether a control page exists.
let broadcastChannel = null;

function buildSnapshot() {
  const layout = computeLayout(config);
  const cells = slotState.slots.map((videoId, index) => {
    const player = players[index];
    let currentTime = 0;
    let playing = false;
    try {
      currentTime = player?.getCurrentTime?.() ?? 0;
      playing = (player?.getPlayerState?.() ?? -1) === 1;
    } catch {
      // A player mid-teardown; snapshot with the defaults above.
    }
    return {
      index,
      videoId: videoId ?? null,
      title: videoId ? (titles.get(videoId) ?? videoId) : null,
      empty: !videoId,
      zoom: views.get(index)?.zoom ?? 1,
      locked: lockedIndex === index,
      audible: isAudible(index, currentAudioTarget()),
      playing,
      currentTime,
      // Percentages, same object shape buildCells() already applies to this
      // page's own cell -- /layout-control positions its rectangle from this
      // directly, so the two pages can never disagree about geometry.
      rect: layout.cellRect ? layout.cellRect(index) : null,
    };
  });

  return {
    type: "snapshot",
    global: {
      status: statusEl.textContent,
      statusState: statusEl.dataset.state ?? "",
      audioIndicatorText: audioEl.textContent,
      audioLocked: audioEl.dataset.locked === "true",
      muted,
      prerolled,
      wantPlaying,
      playDisabled: playButton.disabled,
      hoverUnmuteChecked: hoverUnmuteCheckbox.checked,
      followChecked: followCheckbox.checked,
      newQueryVisible: !newQueryButton.hidden,
      newQueryDisabled: newQueryButton.disabled,
      reservesLeft: slotState.reserves.length,
    },
    cells,
  };
}

function publishSnapshot() {
  if (!broadcastChannel) return;
  try {
    broadcastChannel.postMessage(buildSnapshot());
  } catch {
    // A channel can throw if it has already been closed; nothing useful to do.
  }
}
```

- [ ] **Step 2: Publish after the moments that matter, plus a heartbeat**

At the end of `applyIntent`'s switch (immediately before its closing `}`), i.e. change:
```js
// Old:
    default:
      wlog(`applyIntent: unknown intent type ${intent.type}`);
      return;
  }
}
```
```js
// New:
    default:
      wlog(`applyIntent: unknown intent type ${intent.type}`);
      return;
  }
  publishSnapshot();
}
```

At the end of `finishPreroll`, change:
```js
// Old:
  // A new set starts paused unless the user asked it to follow the play state.
  if (wantPlaying && followCheckbox.checked) {
    startAll();
  } else {
    wantPlaying = false;
    refreshControls();
    setStatus(`${statusPrefix} · ready — press Play`);
  }
}
```
```js
// New:
  // A new set starts paused unless the user asked it to follow the play state.
  if (wantPlaying && followCheckbox.checked) {
    startAll();
  } else {
    wantPlaying = false;
    refreshControls();
    setStatus(`${statusPrefix} · ready — press Play`);
  }
  publishSnapshot();
}
```

- [ ] **Step 3: Open the channel and subscribe, only when asked**

Immediately before the final `connectSocket({` call at the bottom of `startWall`, insert:

```js
if (controlChannel) {
  broadcastChannel = new BroadcastChannel(controlChannel);
  broadcastChannel.addEventListener("message", (event) => {
    applyIntent(event.data);
  });
  // A heartbeat, not the only source of truth: applyIntent and finishPreroll
  // already publish on every discrete change. This just guarantees a
  // late-joining or reconnecting control tab is never more than a second
  // stale, without instrumenting every low-level mutation site.
  setInterval(publishSnapshot, 1000);
}

```

- [ ] **Step 4: Verify no behavior change on `/` or default `/layout`**

Neither `player.js` nor `layout-page.js` passes `controlChannel` yet (that's Task 5), so `broadcastChannel` stays `null` everywhere in the app today and `publishSnapshot()` is a no-op.

```bash
node --check static/wall-engine.js
node --test 'static/*.test.mjs'
uv run pytest tests/ -v
uv run pytest tests/test_player_smoke.py -m browser -v
uv run pytest tests/test_layout_smoke.py -m browser -v
```
Expected: identical pass counts to Task 3's verification.

- [ ] **Step 5: Commit**

```bash
git add static/wall-engine.js
git commit -m "feat: opt-in BroadcastChannel snapshot publishing and intent intake"
```

---

### Task 5: `/layout` hides its chrome and opts in

**Files:**
- Modify: `static/layout.html`
- Modify: `static/layout-page.js`

**Interfaces:**
- Consumes: `startWall`'s `controlChannel` option (Task 4).
- Produces: nothing consumed elsewhere in this plan except the literal channel name string, which Task 6 must match exactly: `"yt-matrix-layout-control"`.

- [ ] **Step 1: Hide the header, keep it in the DOM**

In `static/layout.html`'s `<style>` block, add (anywhere inside the block; placing it next to the existing `header { ... }` rule keeps the two together):

```css
  /* Hidden, not removed: wall-engine.js's getElementById lookups for every
     header control must keep succeeding, since /layout-control drives them
     remotely. This is what keeps the NDI broadcast clean -- nothing here is
     a JS change. */
  header { display: none; }
```

- [ ] **Step 2: Opt into the channel**

In `static/layout-page.js`, change:
```js
// Old:
await loadScreens();
startWall({ computeLayout });
```
```js
// New:
await loadScreens();
startWall({ computeLayout, controlChannel: "yt-matrix-layout-control" });
```

- [ ] **Step 3: Verify**

```bash
node --check static/layout-page.js
uv run pytest tests/test_layout_smoke.py -m browser -v
```
Expected: 3/3 pass — the existing test never asserts on header visibility or `BroadcastChannel` traffic, so this is a spot check that nothing broke, not proof of the new behavior (Task 8 covers that).

Then a manual check that the header is really gone from view without breaking the page:
```bash
bash scripts/build-dist.sh
YOUTUBE_API_KEY=dummy_verification_key YTMATRIX_HOST=127.0.0.1 YTMATRIX_PORT=8444 uv run python -m ytmatrix.main &
sleep 3
curl -sk https://localhost:8444/layout | grep -o "display: none"
kill %1
```
Expected: `display: none` appears in the served HTML (proof the CSS shipped).

- [ ] **Step 4: Commit**

```bash
git add static/layout.html static/layout-page.js
git commit -m "feat: hide /layout's chrome and open the control channel"
```

---

### Task 6: The `/layout-control` page

**Files:**
- Create: `static/layout-control.html`
- Create: `static/layout-control.js`

**Interfaces:**
- Consumes: the `"yt-matrix-layout-control"` `BroadcastChannel` (Task 5), `videoUrl`/`formatTimecode` from `static/grid-logic.js` (existing, unchanged).
- Produces: nothing consumed elsewhere in this plan.

- [ ] **Step 1: Create `static/layout-control.html`**

Copy `static/player.html`, then apply these changes: change `<title>yt matrix</title>` to `<title>yt matrix — layout control</title>`; remove the `<script src="https://www.youtube.com/iframe_api"></script>` line entirely (no players here); change the final script tag to `<script type="module" src="/static/layout-control.js"></script>`; change `<a href="/config">config →</a>` to `<a href="/layout">← broadcast</a>`. Additionally, inside the existing `<style>` block, add (the rest of the block — header, buttons, `#status`, `#menu` and its children — is copied verbatim and needs no change):

```css
  /* .cell here is a plain rectangle, not a player mount: no iframe, no
     pointer-events trick needed (there is nothing to protect the cursor
     from) -- just a label and a state-driven outline so an operator can see
     at a glance which cell is currently audible or locked. */
  #grid { flex: 1; position: relative; min-height: 0; }
  .cell {
    position: absolute; background: #000; overflow: hidden;
    border: 1px solid #26262b; cursor: grab;
  }
  .cell[data-dragging="true"] { cursor: grabbing; }
  .cell[data-audible="true"] { outline: 2px solid #5fb87d; outline-offset: -2px; }
  .cell[data-locked="true"] { outline: 2px solid #d8a657; outline-offset: -2px; }
  .cell .label {
    position: absolute; left: 4px; right: 4px; bottom: 4px;
    font-size: 11px; color: #9a9aa4; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; pointer-events: none;
  }
  .cell[data-empty="true"]::after {
    content: "empty"; position: absolute; inset: 0; display: grid;
    place-items: center; color: #55555e; font-size: 12px;
  }
```

- [ ] **Step 2: Create `static/layout-control.js`**

```js
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

function setStatus(text, state = "") {
  statusEl.textContent = text;
  statusEl.dataset.state = state;
}

setStatus("waiting for /layout to connect…", "busy");

// The last snapshot's cells, kept only so the context menu can be built
// without a round trip -- every mutating action still goes back over the
// channel as an intent.
let latestCells = [];
let staleTimer = null;

function renderFromSnapshot(snapshot) {
  latestCells = snapshot.cells;
  const g = snapshot.global;
  setStatus(g.status, g.statusState);
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
    setStatus("no update from /layout in a while — is it still open?", "error");
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
    setStatus(`copied ${label}`, "busy");
  } catch {
    setStatus("clipboard blocked by the browser", "error");
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
    { label: "Replace with next reserve", run: relay("replaceReserve") },
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
```

- [ ] **Step 3: Verify**

```bash
node --check static/layout-control.js
```
Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add static/layout-control.html static/layout-control.js
git commit -m "feat: the /layout-control page, a pure BroadcastChannel consumer"
```

---

### Task 7: Serve `/layout-control` locally and ship it in `dist/`

**Files:**
- Modify: `ytmatrix/main.py`
- Modify: `scripts/build-dist.sh`

**Interfaces:**
- Consumes: `static/layout-control.html`, `static/layout-control.js` (Task 6).
- Produces: `GET /layout-control` locally; `dist/layout-control.html` in the built bundle.

- [ ] **Step 1: Add the route**

In `ytmatrix/main.py`, immediately after the existing `_layout_page` route, add:

```python
        @app.get("/layout-control", include_in_schema=False)
        async def _layout_control_page() -> FileResponse:
            return FileResponse(dist / "layout-control.html")
```

- [ ] **Step 2: Ship it in the build**

In `scripts/build-dist.sh`, after the existing `cp "$root/static/layout.html" "$dist/layout.html"` line, add:

```bash
cp "$root/static/layout-control.html" "$dist/layout-control.html"
```

`layout-control.js` is already picked up by the existing `cp "$root"/static/*.js "$dist/static/"` line.

- [ ] **Step 3: Verify**

```bash
bash scripts/build-dist.sh
ls dist/layout-control.html dist/static/layout-control.js
YOUTUBE_API_KEY=dummy_verification_key YTMATRIX_HOST=127.0.0.1 YTMATRIX_PORT=8444 uv run python -m ytmatrix.main &
sleep 3
curl -sk -o /dev/null -w "STATUS:%{http_code}\n" https://localhost:8444/layout-control
curl -sk -o /dev/null -w "STATUS:%{http_code}\n" https://localhost:8444/layout
curl -sk -o /dev/null -w "STATUS:%{http_code}\n" https://localhost:8444/
kill %1
```
Expected: every path exists and every status is `200`.

- [ ] **Step 4: Commit**

```bash
git add ytmatrix/main.py scripts/build-dist.sh
git commit -m "feat: serve /layout-control locally and ship it in dist/"
```

---

### Task 8: Two-page browser test

**Files:**
- Create: `tests/test_layout_control_smoke.py`

**Interfaces:**
- Consumes: everything above. Mirrors `tests/test_layout_smoke.py`'s fixture shape exactly (self-contained per gotcha 14), but opens two pages from one `BrowserContext`.

- [ ] **Step 1: Write the test**

```python
# tests/test_layout_control_smoke.py
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
        broadcast.wait_for_function(
            "window.__players.every(p => !p.isMuted())", timeout=10_000
        )
        control.wait_for_function(
            "document.getElementById('mute').textContent.trim() === 'Mute'", timeout=10_000
        )
        browser.close()
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_layout_control_smoke.py -m browser -v
```
Expected: 3/3 pass.

- [ ] **Step 3: Full regression sweep**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/ -v
node --test 'static/*.test.mjs'
uv run pytest tests/test_player_smoke.py -m browser -v
uv run pytest tests/test_layout_smoke.py -m browser -v
uv run pytest tests/test_layout_control_smoke.py -m browser -v
```
Expected: everything passes, including the full, unmodified `test_player_smoke.py` and `test_layout_smoke.py` suites — the proof neither existing page's behavior ever changed.

- [ ] **Step 4: Commit**

```bash
git add tests/test_layout_control_smoke.py
git commit -m "test: two-page browser coverage for the layout control surface"
```

---

## Self-Review

**Spec coverage:**
- Every intent and snapshot field the spec lists → Tasks 3 and 4.
- Header hidden by CSS only, elements kept in the DOM → Task 5.
- Read-only vs. relayed menu actions → Task 6.
- `/`'s and default-`/layout`'s behavior unchanged → verified at the end of every task from 1 through 5, and again in Task 8's full sweep.
- Two-page interaction proof → Task 8.

**Type/name consistency check:** the intent `type` strings, the snapshot's `global`/`cells` field names, and the channel name `"yt-matrix-layout-control"` are used identically across Tasks 3, 4, 5, 6, and 8's test.

**No placeholders:** every step shows literal file content or an exact, unambiguous old/new diff anchored to the real current file content (quoted above from the actual `static/wall-engine.js` in this worktree).
