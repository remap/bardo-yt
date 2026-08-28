# Layout-driver wall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, opt-in front end (`/layout`) that spreads videos across the six real layout-driver screens instead of one flat grid, with a config-driven per-screen/total video budget — with zero change to the existing `/` grid or anything already deployed.

**Architecture:** A shared player engine (`static/wall-engine.js`, extracted from today's `static/player.js`) drives DOM/YouTube-player mechanics behind one pluggable seam — "how many cells, and where does each one go." `/` keeps its CSS-grid strategy unchanged; a new `/layout` page supplies a screens-based strategy built from a vendored snapshot of `../layout-driver`'s real screen geometry and a new optional `layout` config section. All allocation/fitting math is pure and node-tested; the DOM wiring itself is untouched.

**Tech Stack:** Python 3.13 / FastAPI / pydantic (backend, untouched except one new optional config section). Vanilla JS ES modules, `node --test` (frontend). Playwright for the browser smoke test.

**Spec:** `docs/superpowers/specs/2026-08-28-layout-driver-wall-design.md`

## Global Constraints

- **Branch:** all work happens on `feat/layout-driver-wall`, branched from `feat/cloudflare-deploy` (the branch that is actually deployed). Do not rebase onto or merge from `main`.
- **Zero behavior change on `/`.** `tests/test_player_smoke.py` (marked `browser`) must pass, unmodified, against `/` both before and after every task that touches `static/player.js` or `static/wall-engine.js`.
- **No dependency on `../layout-driver` at runtime.** `static/layout/screens.json` is a vendored, hand-copied snapshot — no code in this plan reads from `../layout-driver` at build or run time.
- **`MAX_SEARCH_RESULTS = 50`** (from `ytmatrix/config.py`) bounds `layout.total`, exactly as it already bounds `grid.cells`.
- Default suite must keep passing with no network access (`uv run pytest tests/ -v`), plus `node --test 'static/*.test.mjs'` and `uv run ruff check . && uv run ruff format .` clean, before every commit.
- Every new pure function (allocation, fitting, geometry) is node-tested; every new pydantic validator is pytest-tested. No test reaches the network.

---

### Task 1: Vendor screen geometry and pixel-rect math

**Files:**
- Create: `static/layout/screens.json`
- Create: `static/layout-fit.js`
- Test: `static/layout-fit.test.mjs`

**Interfaces:**
- Consumes: nothing.
- Produces: `screenRectPx(grid, moduleSize, offset) -> {x, y, width, height}`, exported from `static/layout-fit.js`.

- [ ] **Step 1: Vendor the geometry**

Create `static/layout/screens.json`, copied from `../layout-driver/config/screens.yaml` (canvas size, `module_size`, `layout_offset`, and all six screens' grid-unit rects — verified against that file directly, not re-derived):

```json
{
  "canvas": { "width": 3840, "height": 2160 },
  "module_size": 200,
  "layout_offset": { "x": 0, "y": 0 },
  "screens": [
    { "id": "F", "name": "Screen F", "grid": { "col": 0, "row": 0, "cols": 9, "rows": 7 } },
    { "id": "B", "name": "Screen B", "grid": { "col": 9, "row": 0, "cols": 6, "rows": 3 } },
    { "id": "C", "name": "Screen C", "grid": { "col": 9, "row": 3, "cols": 6, "rows": 3 } },
    { "id": "D", "name": "Screen D", "grid": { "col": 9, "row": 6, "cols": 8, "rows": 2 } },
    { "id": "A", "name": "Screen A", "grid": { "col": 9, "row": 8, "cols": 8, "rows": 2 } },
    { "id": "E", "name": "Screen E", "grid": { "col": 1, "row": 7, "cols": 8, "rows": 2 } }
  ]
}
```

- [ ] **Step 2: Write the failing test**

```js
// static/layout-fit.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { screenRectPx } from "./layout-fit.js";

test("screenRectPx converts grid units to pixels, matching layout-driver's compute_rect", () => {
  // col*module_size + offset.x, row*module_size + offset.y, cols*module_size, rows*module_size --
  // this is the exact formula in ../layout-driver/layout_server/config.py:compute_rect.
  const rect = screenRectPx({ col: 9, row: 3, cols: 6, rows: 3 }, 200, { x: 0, y: 0 });
  assert.deepEqual(rect, { x: 1800, y: 600, width: 1200, height: 600 });
});

test("screenRectPx applies a nonzero layout_offset", () => {
  const rect = screenRectPx({ col: 0, row: 0, cols: 9, rows: 7 }, 200, { x: 220, y: 80 });
  assert.deepEqual(rect, { x: 220, y: 80, width: 1800, height: 1400 });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `node --test static/layout-fit.test.mjs`
Expected: FAIL — `Cannot find module './layout-fit.js'`

- [ ] **Step 4: Implement**

```js
// static/layout-fit.js
/**
 * Pure geometry and allocation math for the layout-driver wall (/layout).
 * No DOM, no fetch -- node-testable like grid-logic.js.
 */

// Mirrors ../layout-driver/layout_server/config.py:compute_rect exactly, so a
// screens.json vendored from that repo's screens.yaml produces the same
// pixel rects that project's own page would draw.
export function screenRectPx(grid, moduleSize, offset) {
  return {
    x: offset.x + grid.col * moduleSize,
    y: offset.y + grid.row * moduleSize,
    width: grid.cols * moduleSize,
    height: grid.rows * moduleSize,
  };
}
```

- [ ] **Step 5: Run it to verify it passes**

Run: `node --test static/layout-fit.test.mjs`
Expected: PASS (2 tests)

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add static/layout/screens.json static/layout-fit.js static/layout-fit.test.mjs
git commit -m "feat: vendor layout-driver screen geometry and pixel-rect math"
```

---

### Task 2: Per-screen budget allocation

**Files:**
- Modify: `static/layout-fit.js`
- Modify: `static/layout-fit.test.mjs`

**Interfaces:**
- Consumes: nothing new.
- Produces: `allocateScreenCounts({ total, maxPerScreen, screens, screenAreas }) -> { [screenId]: count }`, where `screens` is `{ [screenId]: number | "auto" | "none" }` with every id already present (no defaulting inside this function), and `screenAreas` is `{ [screenId]: number }`.

- [ ] **Step 1: Write the failing tests**

```js
// static/layout-fit.test.mjs -- add below the existing tests
import { allocateScreenCounts } from "./layout-fit.js";

test("explicit counts are kept exactly and none screens get zero", () => {
  const counts = allocateScreenCounts({
    total: 20,
    maxPerScreen: 10,
    screens: { A: 4, B: "none", C: "auto" },
    screenAreas: { A: 100, B: 100, C: 100 },
  });
  assert.equal(counts.A, 4);
  assert.equal(counts.B, 0);
  assert.equal(counts.C, 16); // all remaining budget, one auto screen
});

test("remaining budget splits across auto screens by area", () => {
  const counts = allocateScreenCounts({
    total: 12,
    maxPerScreen: 10,
    screens: { A: "auto", B: "auto" },
    screenAreas: { A: 200, B: 100 }, // A is twice B's area
  });
  assert.equal(counts.A, 8);
  assert.equal(counts.B, 4);
});

test("auto screens are clamped to maxPerScreen", () => {
  const counts = allocateScreenCounts({
    total: 20,
    maxPerScreen: 3,
    screens: { A: "auto", B: "auto" },
    screenAreas: { A: 100, B: 100 },
  });
  assert.equal(counts.A, 3);
  assert.equal(counts.B, 3);
});

test("rounding leftover goes to the largest screens first", () => {
  // total=8 over 6 equal-area auto screens: floor(8/6)=1 each, remainder 2
  // goes to the first two screens in iteration order (ties keep insertion order).
  const screens = { F: "auto", B: "auto", C: "auto", D: "auto", A: "auto", E: "auto" };
  const screenAreas = { F: 100, B: 100, C: 100, D: 100, A: 100, E: 100 };
  const counts = allocateScreenCounts({ total: 8, maxPerScreen: 3, screens, screenAreas });
  assert.equal(Object.values(counts).reduce((a, b) => a + b, 0), 8);
  assert.equal(counts.F, 2);
  assert.equal(counts.B, 2);
  assert.equal(counts.C, 1);
  assert.equal(counts.D, 1);
  assert.equal(counts.A, 1);
  assert.equal(counts.E, 1);
});

test("the real six-screen default (total 8, max 3) matches the real geometry", () => {
  // F is much larger than the other five, so its proportional share alone
  // would exceed maxPerScreen -- this is the exact scenario the default
  // config produces against static/layout/screens.json.
  const screenAreas = { F: 1800 * 1400, B: 1200 * 600, C: 1200 * 600, D: 1600 * 400, A: 1600 * 400, E: 1600 * 400 };
  const screens = { F: "auto", B: "auto", C: "auto", D: "auto", A: "auto", E: "auto" };
  const counts = allocateScreenCounts({ total: 8, maxPerScreen: 3, screens, screenAreas });
  assert.deepEqual(counts, { F: 3, B: 1, C: 1, D: 1, A: 1, E: 1 });
});

test("zero total area falls back to zero for every auto screen", () => {
  const counts = allocateScreenCounts({
    total: 5,
    maxPerScreen: 3,
    screens: { A: "auto", B: "auto" },
    screenAreas: { A: 0, B: 0 },
  });
  assert.deepEqual(counts, { A: 0, B: 0 });
});

test("explicit counts summing to the total leave nothing for auto screens", () => {
  const counts = allocateScreenCounts({
    total: 4,
    maxPerScreen: 10,
    screens: { A: 4, B: "auto" },
    screenAreas: { A: 100, B: 100 },
  });
  assert.equal(counts.B, 0);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test static/layout-fit.test.mjs`
Expected: FAIL — `allocateScreenCounts is not a function`

- [ ] **Step 3: Implement**

```js
// static/layout-fit.js -- append

/**
 * Split a total video budget across screens.
 *
 * Explicit counts are authoritative and are subtracted from `total` first;
 * whatever remains is split across the "auto" screens proportional to their
 * own pixel area (a bigger physical screen gets more), floored, then any
 * leftover from rounding goes to the largest screens first until the budget
 * is exhausted or every auto screen is at `maxPerScreen`. `Config`
 * (ytmatrix/config.py) already validates that explicit counts individually
 * fit `maxPerScreen` and together fit `total` -- this function still clamps
 * defensively so a stale/unvalidated config degrades rather than going
 * negative.
 */
export function allocateScreenCounts({ total, maxPerScreen, screens, screenAreas }) {
  const counts = {};
  const autoIds = [];
  let explicitSum = 0;

  for (const [id, value] of Object.entries(screens)) {
    if (value === "none") {
      counts[id] = 0;
    } else if (value === "auto") {
      autoIds.push(id);
    } else {
      counts[id] = value;
      explicitSum += value;
    }
  }

  const remaining = Math.max(0, total - explicitSum);
  const totalArea = autoIds.reduce((sum, id) => sum + (screenAreas[id] ?? 0), 0);

  const shares = {};
  let allocated = 0;
  for (const id of autoIds) {
    const share =
      totalArea > 0 ? Math.floor((remaining * (screenAreas[id] ?? 0)) / totalArea) : 0;
    shares[id] = Math.min(share, maxPerScreen);
    allocated += shares[id];
  }

  // Largest screens first, skipping any already at the cap. The guard bounds
  // the loop at "one full pass per unit of leftover" so a maxPerScreen of 0
  // (nothing left to give) or every screen already capped cannot spin forever.
  const byAreaDesc = [...autoIds].sort((a, b) => (screenAreas[b] ?? 0) - (screenAreas[a] ?? 0));
  let leftover = remaining - allocated;
  let guard = byAreaDesc.length * maxPerScreen + 1;
  while (leftover > 0 && guard > 0 && byAreaDesc.some((id) => shares[id] < maxPerScreen)) {
    for (const id of byAreaDesc) {
      if (leftover <= 0) break;
      if (shares[id] < maxPerScreen) {
        shares[id] += 1;
        leftover -= 1;
      }
    }
    guard -= 1;
  }

  for (const id of autoIds) counts[id] = shares[id];
  return counts;
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `node --test static/layout-fit.test.mjs`
Expected: PASS (9 tests)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff format .   # no Python touched, but keeps the habit consistent
git add static/layout-fit.js static/layout-fit.test.mjs
git commit -m "feat: per-screen video budget allocation"
```

---

### Task 3: Per-screen cols×rows fit

**Files:**
- Modify: `static/layout-fit.js`
- Modify: `static/layout-fit.test.mjs`

**Interfaces:**
- Consumes: nothing new.
- Produces: `fitGrid(width, height, count) -> { cols, rows }`.

- [ ] **Step 1: Write the failing tests**

```js
// static/layout-fit.test.mjs -- add below
import { fitGrid } from "./layout-fit.js";

test("fitGrid on a near-16:9 box with a perfect-square count picks the square factor pair", () => {
  // 1600x900 box, 4 cells: 2x2 gives 800x450 cells, exactly 16:9.
  assert.deepEqual(fitGrid(1600, 900, 4), { cols: 2, rows: 2 });
});

test("fitGrid handles a count with no clean factor pair by allowing a partial last row", () => {
  // 1200x600, N=3: candidates are 1x3 (cellAspect 1200/200=6), 2x2 (600/300=2),
  // 3x1 (400/600=0.667) against a 16:9 (1.778) target -- 2x2 is closest, and
  // its 4th cell is simply never rendered (buildCells only creates N cells).
  assert.deepEqual(fitGrid(1200, 600, 3), { cols: 2, rows: 2 });
});

test("fitGrid on a single cell is always 1x1", () => {
  assert.deepEqual(fitGrid(1800, 1400, 1), { cols: 1, rows: 1 });
});

test("fitGrid on a wide screen favors more columns than rows", () => {
  // 1800x1400 box (roughly square-ish, 1.286:1), N=3: verified against the
  // default allocation's F screen -- 2x2 is the closest fit (see Task 2's
  // "real six-screen default" test for how N=3 arises here).
  assert.deepEqual(fitGrid(1800, 1400, 3), { cols: 2, rows: 2 });
});

test("fitGrid returns 0x0 for a non-positive count", () => {
  assert.deepEqual(fitGrid(1000, 1000, 0), { cols: 0, rows: 0 });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test static/layout-fit.test.mjs`
Expected: FAIL — `fitGrid is not a function`

- [ ] **Step 3: Implement**

```js
// static/layout-fit.js -- append

const TARGET_CELL_ASPECT = 16 / 9;

/**
 * The cols x rows layout, among cols in 1..count, whose resulting cell
 * aspect ratio is closest to 16:9 for a box of the given pixel size.
 *
 * rows = ceil(count / cols) rather than requiring an exact factor pair, so a
 * prime count (5, 7, 11...) still gets a sensible rectangle instead of being
 * forced into a single row or column -- the last row is simply short by
 * however many cells cols*rows exceeds count, and the caller (buildCells)
 * only ever creates `count` real cells, so that shortfall is an unfilled gap
 * in the layout rather than an empty placeholder cell.
 */
export function fitGrid(width, height, count) {
  if (count <= 0) return { cols: 0, rows: 0 };
  let best = null;
  for (let cols = 1; cols <= count; cols += 1) {
    const rows = Math.ceil(count / cols);
    const cellAspect = width / cols / (height / rows);
    const distance = Math.abs(Math.log(cellAspect / TARGET_CELL_ASPECT));
    if (!best || distance < best.distance) best = { cols, rows, distance };
  }
  return { cols: best.cols, rows: best.rows };
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `node --test static/layout-fit.test.mjs`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add static/layout-fit.js static/layout-fit.test.mjs
git commit -m "feat: fit cols x rows to a screen's own aspect ratio"
```

---

### Task 4: Compose into one resolved layout

**Files:**
- Modify: `static/layout-fit.js`
- Modify: `static/layout-fit.test.mjs`

**Interfaces:**
- Consumes: `screenRectPx`, `allocateScreenCounts`, `fitGrid` (Tasks 1-3, same file).
- Produces: `resolveLayout(screensData, layoutConfig) -> { canvas: {width, height}, totalCells, placements: [{screenId, left, top, width, height}, ...] }`, where `screensData` is the shape of `static/layout/screens.json` and `layoutConfig` is `{ total, max_per_screen, screens }` (the shape of `Config.layout`, or the page's own default when `Config.layout` is `null`). `placements` is ordered screen-by-screen (in `screensData.screens` order), row-major within each screen -- this order IS the flat cell index space `wall-engine.js` uses.

- [ ] **Step 1: Write the failing test**

```js
// static/layout-fit.test.mjs -- add below
import { resolveLayout } from "./layout-fit.js";

const REAL_SCREENS = {
  canvas: { width: 3840, height: 2160 },
  module_size: 200,
  layout_offset: { x: 0, y: 0 },
  screens: [
    { id: "F", name: "Screen F", grid: { col: 0, row: 0, cols: 9, rows: 7 } },
    { id: "B", name: "Screen B", grid: { col: 9, row: 0, cols: 6, rows: 3 } },
    { id: "C", name: "Screen C", grid: { col: 9, row: 3, cols: 6, rows: 3 } },
    { id: "D", name: "Screen D", grid: { col: 9, row: 6, cols: 8, rows: 2 } },
    { id: "A", name: "Screen A", grid: { col: 9, row: 8, cols: 8, rows: 2 } },
    { id: "E", name: "Screen E", grid: { col: 1, row: 7, cols: 8, rows: 2 } },
  ],
};

test("resolveLayout with the default config produces 8 cells across the six real screens", () => {
  const result = resolveLayout(REAL_SCREENS, { total: 8, max_per_screen: 3, screens: {} });
  assert.equal(result.totalCells, 8);
  assert.deepEqual(result.canvas, { width: 3840, height: 2160 });
  const perScreen = {};
  for (const p of result.placements) perScreen[p.screenId] = (perScreen[p.screenId] ?? 0) + 1;
  assert.deepEqual(perScreen, { F: 3, B: 1, C: 1, D: 1, A: 1, E: 1 });
});

test("resolveLayout places cells inside their screen's real pixel rect", () => {
  const result = resolveLayout(REAL_SCREENS, { total: 1, max_per_screen: 1, screens: { F: 1, B: "none", C: "none", D: "none", A: "none", E: "none" } });
  assert.equal(result.totalCells, 1);
  const [cell] = result.placements;
  assert.equal(cell.screenId, "F");
  assert.deepEqual(cell, { screenId: "F", left: 0, top: 0, width: 1800, height: 1400 });
});

test("resolveLayout defaults a screen id missing from layoutConfig.screens to auto", () => {
  const result = resolveLayout(REAL_SCREENS, { total: 6, max_per_screen: 6, screens: {} });
  assert.equal(result.totalCells, 6);
});

test("resolveLayout skips a screen with zero resolved cells entirely", () => {
  const result = resolveLayout(REAL_SCREENS, {
    total: 1,
    max_per_screen: 1,
    screens: { F: 1, B: "none", C: "none", D: "none", A: "none", E: "none" },
  });
  assert.ok(result.placements.every((p) => p.screenId === "F"));
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test static/layout-fit.test.mjs`
Expected: FAIL — `resolveLayout is not a function`

- [ ] **Step 3: Implement**

```js
// static/layout-fit.js -- append

/**
 * Turn a screens.json snapshot plus a layout config into an ordered, flat
 * list of pixel placements -- one per cell, in the order wall-engine.js's
 * flat cell index space uses (screen-by-screen in screensData order,
 * row-major within each screen).
 */
export function resolveLayout(screensData, layoutConfig) {
  const { canvas, module_size: moduleSize, layout_offset: offset, screens } = screensData;

  const rects = screens.map((screen) => ({
    id: screen.id,
    rect: screenRectPx(screen.grid, moduleSize, offset),
  }));
  const screenAreas = Object.fromEntries(rects.map((s) => [s.id, s.rect.width * s.rect.height]));
  const selections = Object.fromEntries(
    screens.map((screen) => [screen.id, layoutConfig.screens?.[screen.id] ?? "auto"]),
  );

  const counts = allocateScreenCounts({
    total: layoutConfig.total,
    maxPerScreen: layoutConfig.max_per_screen,
    screens: selections,
    screenAreas,
  });

  const placements = [];
  for (const { id, rect } of rects) {
    const count = counts[id] ?? 0;
    if (count <= 0) continue;
    const { cols, rows } = fitGrid(rect.width, rect.height, count);
    const cellWidth = rect.width / cols;
    const cellHeight = rect.height / rows;
    for (let index = 0; index < count; index += 1) {
      const col = index % cols;
      const row = Math.floor(index / cols);
      placements.push({
        screenId: id,
        left: rect.x + col * cellWidth,
        top: rect.y + row * cellHeight,
        width: cellWidth,
        height: cellHeight,
      });
    }
  }

  return { canvas, totalCells: placements.length, placements };
}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `node --test static/layout-fit.test.mjs`
Expected: PASS (18 tests)

- [ ] **Step 5: Commit**

```bash
git add static/layout-fit.js static/layout-fit.test.mjs
git commit -m "feat: resolve screens.json + layout config into flat cell placements"
```

---

### Task 5: `LayoutConfig` on the backend

**Files:**
- Modify: `ytmatrix/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing new (pydantic, already imported).
- Produces: `LayoutConfig` model with fields `total: int` (default 8), `max_per_screen: int` (default 3), `screens: dict[str, int | Literal["auto", "none"]]` (default `{}`). `Config.layout: LayoutConfig | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py -- add below the existing grid/quota tests

def test_layout_is_absent_by_default():
    assert Config.model_validate(VALID).layout is None


def test_layout_accepts_auto_and_none_and_explicit_counts():
    data = {**VALID, "layout": {"screens": {"F": "auto", "D": "none", "C": 2}}}
    layout = Config.model_validate(data).layout
    assert layout.screens == {"F": "auto", "D": "none", "C": 2}
    assert layout.total == 8  # default
    assert layout.max_per_screen == 3  # default


def test_layout_rejects_an_unknown_screen_value():
    data = {**VALID, "layout": {"screens": {"F": "sometimes"}}}
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_layout_total_cannot_exceed_max_search_results():
    data = {**VALID, "layout": {"total": 51}}
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_layout_rejects_an_explicit_count_over_max_per_screen():
    data = {**VALID, "layout": {"max_per_screen": 3, "screens": {"C": 4}}}
    with pytest.raises(ValidationError, match="max_per_screen"):
        Config.model_validate(data)


def test_layout_rejects_explicit_counts_summing_past_total():
    data = {**VALID, "layout": {"total": 5, "max_per_screen": 10, "screens": {"C": 3, "D": 3}}}
    with pytest.raises(ValidationError, match="total"):
        Config.model_validate(data)


def test_layout_rejects_unknown_keys():
    data = {**VALID, "layout": {"colour": "blue"}}
    with pytest.raises(ValidationError):
        Config.model_validate(data)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'layout'`

- [ ] **Step 3: Implement**

In `ytmatrix/config.py`, add `Literal` to the existing `typing`-adjacent imports (there is no `typing` import yet -- add `from typing import Literal`) and `Annotated` alongside it, then add the model after `QuotaConfig` and before `Config`:

```python
from typing import Annotated, Literal
```

```python
class LayoutConfig(Strict):
    """Per-screen video counts for the /layout front end.

    Entirely optional on Config -- its absence means "layout mode has never
    been configured," and the /layout page falls back to its own client-side
    defaults (see static/layout-page.js), which mirror the defaults here.
    Screen ids are whatever static/layout/screens.json defines; this model
    does not know or care what they are, so an id here that screens.json
    does not have is silently ignored by the page, and a screen.json id
    absent from `screens` defaults to "auto" there.
    """

    total: int = Field(default=8, ge=1, le=MAX_SEARCH_RESULTS)
    max_per_screen: int = Field(default=3, ge=1)
    screens: dict[str, Annotated[int, Field(ge=0)] | Literal["auto", "none"]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _explicit_counts_fit_the_budget(self) -> "LayoutConfig":
        explicit = {
            screen_id: value for screen_id, value in self.screens.items() if isinstance(value, int)
        }
        for screen_id, count in explicit.items():
            if count > self.max_per_screen:
                raise ValueError(
                    f"screen {screen_id!r} requests {count} but max_per_screen is "
                    f"{self.max_per_screen}"
                )
        explicit_sum = sum(explicit.values())
        if explicit_sum > self.total:
            raise ValueError(
                f"explicit screen counts sum to {explicit_sum} but total is {self.total}"
            )
        return self
```

Then add the field to `Config`, after `quota: QuotaConfig = QuotaConfig()`:

```python
    layout: LayoutConfig | None = None
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all tests, including the 7 new ones)

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/ -v
git add ytmatrix/config.py tests/test_config.py
git commit -m "feat: optional LayoutConfig section for the layout-driver wall"
```

---

### Task 6: Extract `wall-engine.js` with a pluggable cell-layout seam

This is the one task with real regression risk to `/`. The transform is deliberately small and mechanical: wrap the existing file's body in an exported function, then generalize the three places it hardcodes "how many cells, arranged how" behind one injected `computeLayout(config)` function. Nothing else in the file changes.

**Files:**
- Create: `static/wall-engine.js` (copy of today's `static/player.js`, then the diffs below)
- Modify: `static/player.js` (replaced with a 2-line bootstrap)

**Interfaces:**
- Consumes: nothing new.
- Produces: `export function startWall({ computeLayout = defaultComputeLayout } = {})` from `static/wall-engine.js`, where `computeLayout(config)` returns `{ totalCells: number, containerStyle: object, cellRect: ((index: number) => object) | null }`. `cellRect: null` means "let CSS position the cell" (the grid-mode default); when it is a function, `buildCells()` applies its return value as inline styles on that cell.

- [ ] **Step 1: Copy the file unchanged**

```bash
cp static/player.js static/wall-engine.js
```

- [ ] **Step 2: Apply these exact diffs to `static/wall-engine.js`**

Insert this function, and the `export function startWall(...)` opening, immediately before the line `const gridEl = document.getElementById("grid");` (currently line 113 — everything from that line through the end of the file, i.e. through the closing `});` of the `connectSocket({...})` call, moves one function-scope deeper; nothing inside it changes except the two diffs below):

```js
// Old:
const gridEl = document.getElementById("grid");
```

```js
// New:
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

export function startWall({ computeLayout = defaultComputeLayout } = {}) {

const gridEl = document.getElementById("grid");
```

At the very end of the file, after the closing of the `connectSocket({...});` call (the file's last line), add one closing brace to close `startWall`:

```js
// New, appended as the final line of the file:
}
```

Inside `buildCells()`, replace:

```js
// Old:
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
```

with:

```js
// New:
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
```

In `applyVideos()`, replace:

```js
// Old:
  slotState = splitSlots(message.video_ids.concat(message.reserves), cellCount(config.grid));
```

with:

```js
// New:
  slotState = splitSlots(message.video_ids.concat(message.reserves), computeLayout(config).totalCells);
```

In the `shuffleButton` click handler, replace:

```js
// Old:
shuffleButton.addEventListener("click", () => {
  const wasPlaying = wantPlaying;
  slotState = shuffleSlots(
    [...slotState.slots, ...slotState.reserves],
    cellCount(config.grid),
  );
```

with:

```js
// New:
shuffleButton.addEventListener("click", () => {
  const wasPlaying = wantPlaying;
  slotState = shuffleSlots(
    [...slotState.slots, ...slotState.reserves],
    computeLayout(config).totalCells,
  );
```

- [ ] **Step 3: Replace `static/player.js` with the bootstrap**

```js
// static/player.js -- entire file
import { startWall } from "./wall-engine.js";

startWall();
```

- [ ] **Step 4: Verify nothing about `/` changed**

Run:
```bash
node --test 'static/*.test.mjs'
uv run pytest tests/ -v
uv run pytest tests/test_player_smoke.py -m browser -v
```
Expected: all PASS, identically to before this task. `test_player_smoke.py` is the proof: it asserts on `window.__players`, `.cell` DOM structure, pre-roll, mute, zoom, pan, the context menu and shuffle — none of that changed for `/`, because `defaultComputeLayout` reproduces the exact same `gridTemplateColumns`/`gridTemplateRows` assignment and the exact same `cellCount(config.grid)` value the old code computed inline.

- [ ] **Step 5: Commit**

```bash
uv run ruff check . && uv run ruff format .
git add static/wall-engine.js static/player.js
git commit -m "refactor: extract wall-engine.js with a pluggable cell-layout seam"
```

---

### Task 7: The `/layout` page

**Files:**
- Create: `static/layout.html`
- Create: `static/layout-page.js`

**Interfaces:**
- Consumes: `startWall` (Task 6), `resolveLayout` (Task 4), `static/layout/screens.json` (Task 1) fetched at `/static/layout/screens.json`.
- Produces: nothing consumed elsewhere in this plan.

**Design note on scaling:** cells are positioned as CSS **percentages** of the vendored canvas dimensions (3840×2160), not fixed pixels or a CSS `transform: scale()`. `#grid` simply fills its flex area (matching how `/` already lets the browser handle `1fr` grid sizing) and each cell's percentage is fixed relative to the canvas. This means `/layout` fills whatever aspect ratio the browser window has — on a non-16:9 window the wall's proportions stretch slightly relative to the real venue. That is an accepted, documented tradeoff: `/layout` is a preview of the autofit behavior, not a pixel-accurate broadcast composite — that job belongs to `../layout-driver`'s own `ndi_broadcaster`, not this page. It also means `getBoundingClientRect()` inside `wall-engine.js`'s existing cover-fit/zoom/pan math (unchanged by this plan) always reflects each cell's real, current, un-transformed pixel size — avoiding an entire class of coordinate-space bugs a `transform: scale()` approach would introduce there.

- [ ] **Step 1: Create `static/layout.html`**

Copy `static/player.html` to `static/layout.html`, then apply these changes: change `<title>yt matrix</title>` to `<title>yt matrix — layout</title>`; change `<a href="/config">config →</a>` to `<a href="/">← wall</a> <a href="/config">config →</a>`; change the final `<script type="module" src="/static/player.js"></script>` to `<script type="module" src="/static/layout-page.js"></script>`. Every other line — the entire `<style>` block, the header buttons, `#grid`, `#menu`, the `iframe_api` script tag — is copied verbatim: `wall-engine.js` looks up the exact same element ids regardless of which page loaded it.

- [ ] **Step 2: Create `static/layout-page.js`**

```js
import { startWall } from "./wall-engine.js";
import { resolveLayout } from "./layout-fit.js";

// Mirrors ytmatrix/config.py's LayoutConfig defaults (total=8,
// max_per_screen=3, screens={}) -- used only when config.layout is entirely
// absent, which it is until someone edits it on the config page.
const DEFAULT_LAYOUT_CONFIG = { total: 8, max_per_screen: 3, screens: {} };

let screensData = null;

async function loadScreens() {
  const response = await fetch("/static/layout/screens.json");
  screensData = await response.json();
}

function computeLayout(config) {
  const layoutConfig = config.layout ?? DEFAULT_LAYOUT_CONFIG;
  const resolved = resolveLayout(screensData, layoutConfig);
  return {
    totalCells: resolved.totalCells,
    containerStyle: {
      display: "block",
      position: "relative",
      width: "100%",
      height: "100%",
    },
    cellRect: (index) => {
      const placement = resolved.placements[index];
      return {
        position: "absolute",
        left: `${(placement.left / resolved.canvas.width) * 100}%`,
        top: `${(placement.top / resolved.canvas.height) * 100}%`,
        width: `${(placement.width / resolved.canvas.width) * 100}%`,
        height: `${(placement.height / resolved.canvas.height) * 100}%`,
      };
    },
  };
}

await loadScreens();
startWall({ computeLayout });
```

- [ ] **Step 3: Manual check (no automated test yet — Task 10 adds one)**

There is no server route for `/layout` until Task 9, so this cannot be exercised end to end yet. Confirm only that the two new files have no syntax errors:

```bash
node --check static/layout-page.js
```
Expected: no output (success).

- [ ] **Step 4: Commit**

```bash
git add static/layout.html static/layout-page.js
git commit -m "feat: the /layout page, driven by the shared wall engine"
```

---

### Task 8: Layout section on the config page

**Files:**
- Modify: `static/config.html`
- Modify: `static/config.js`

**Interfaces:**
- Consumes: `GET /static/layout/screens.json` (Task 1), the existing `/api/config`, `/api/cache-status`, `PUT /api/config` routes (all unchanged).
- Produces: nothing consumed elsewhere in this plan.

- [ ] **Step 1: Add the section markup**

In `static/config.html`, add after the `<h2>Playback</h2>` block's three fields and before `<footer>`:

```html
  <h2>Layout (/layout)</h2>
  <div class="row">
    <label>Total videos <input id="layout_total" type="number" min="1" max="50"></label>
    <label>Max per screen <input id="layout_max_per_screen" type="number" min="1"></label>
  </div>
  <div id="layout-screens"></div>
```

- [ ] **Step 2: Fetch the real screen ids and render one field per screen**

In `static/config.js`, add near the top, after the existing `field`/`quotaEl`/`resultEl` constants:

```js
const layoutScreensEl = field("layout-screens");
let screenIds = [];

async function loadScreenIds() {
  const response = await fetch("/static/layout/screens.json");
  const data = await response.json();
  screenIds = data.screens.map((screen) => screen.id);
  layoutScreensEl.replaceChildren();
  for (const id of screenIds) {
    const label = document.createElement("label");
    label.textContent = `Screen ${id}`;
    const input = document.createElement("input");
    input.id = `layout_screen_${id}`;
    input.type = "text";
    input.placeholder = "auto";
    input.addEventListener("input", refreshQuota);
    label.appendChild(input);
    layoutScreensEl.appendChild(label);
  }
}
```

- [ ] **Step 3: Wire it into `fill()` and `collect()`**

In `fill(config)`, add at the end of the function body:

```js
  const layout = config.layout ?? { total: 8, max_per_screen: 3, screens: {} };
  field("layout_total").value = layout.total;
  field("layout_max_per_screen").value = layout.max_per_screen;
  for (const id of screenIds) {
    const el = field(`layout_screen_${id}`);
    if (el) el.value = String(layout.screens?.[id] ?? "auto");
  }
```

In `collect()`, add a `layout` key to the returned object (alongside `query`, `grid`, `search`, `playback`):

```js
    layout: {
      total: Number(field("layout_total").value),
      max_per_screen: Number(field("layout_max_per_screen").value),
      screens: Object.fromEntries(
        screenIds.map((id) => {
          const raw = field(`layout_screen_${id}`).value.trim();
          return [id, raw === "" || raw === "auto" ? "auto" : raw === "none" ? "none" : Number(raw)];
        }),
      ),
    },
```

- [ ] **Step 4: Call `loadScreenIds()` before the first fill**

At the bottom of `static/config.js`, change:

```js
// Old:
async function resync() {
  fill(await (await fetch("/api/config")).json());
  refreshQuota();
}
```

to:

```js
// New:
async function resync() {
  await loadScreenIds();
  fill(await (await fetch("/api/config")).json());
  refreshQuota();
}
```

- [ ] **Step 5: Manual check**

```bash
node --check static/config.js
```
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add static/config.html static/config.js
git commit -m "feat: edit the layout-driver wall's per-screen video budget on the config page"
```

---

### Task 9: Serve `/layout` locally and ship it in `dist/`

**Files:**
- Modify: `ytmatrix/main.py`
- Modify: `scripts/build-dist.sh`

**Interfaces:**
- Consumes: `static/layout.html`, `static/layout-page.js`, `static/wall-engine.js`, `static/layout-fit.js`, `static/layout/screens.json` (Tasks 1-7).
- Produces: `GET /layout` locally; `dist/layout.html` and `dist/static/layout/screens.json` in the built bundle (the Worker's static-asset layer serves these the same way it already serves `dist/index.html` and `dist/config.html`).

- [ ] **Step 1: Add the local route**

In `ytmatrix/main.py`, immediately after the existing `_config_page` route (inside the `if dist.is_dir():` block), add:

```python
        @app.get("/layout", include_in_schema=False)
        async def _layout_page() -> FileResponse:
            return FileResponse(dist / "layout.html")
```

- [ ] **Step 2: Ship the new static files in the build**

In `scripts/build-dist.sh`, after the existing `cp "$root/static/config.html" "$dist/config.html"` line, add:

```bash
cp "$root/static/layout.html" "$dist/layout.html"
mkdir -p "$dist/static/layout"
cp "$root/static/layout/screens.json" "$dist/static/layout/screens.json"
```

The existing `cp "$root"/static/*.js "$dist/static/"` line already picks up `wall-engine.js`, `layout-page.js`, and `layout-fit.js` — they are `.js` files directly under `static/`, matched by the glob exactly like `player.js`, `config.js`, `grid-logic.js` already are. `layout-fit.test.mjs` is excluded the same way `grid-logic.test.mjs` already is (`.mjs`, not `.js`).

- [ ] **Step 3: Verify the build and local route**

```bash
bash scripts/build-dist.sh
ls dist/layout.html dist/static/layout/screens.json dist/static/wall-engine.js dist/static/layout-fit.js dist/static/layout-page.js
```
Expected: every path listed exists, no error.

```bash
./run.sh &
sleep 2
curl -sk https://localhost:8444/layout | grep -o '<title>[^<]*</title>'
curl -sk https://localhost:8444/static/layout/screens.json | head -c 80
kill %1
```
Expected: `<title>yt matrix — layout</title>`, then the start of the vendored JSON.

- [ ] **Step 4: Commit**

```bash
git add ytmatrix/main.py scripts/build-dist.sh
git commit -m "feat: serve /layout locally and ship it in dist/"
```

---

### Task 10: Browser smoke test for `/layout`

**Files:**
- Create: `tests/test_layout_smoke.py`

**Interfaces:**
- Consumes: `tests/test_player_smoke.py`'s pattern (fixture shape, port-finding, dist rebuild) — not imported, mirrored, since browser test files in this repo are self-contained per `CLAUDE.md` gotcha 14.

- [ ] **Step 1: Write the test**

```python
# tests/test_layout_smoke.py
"""Browser smoke test for /layout: the wall must build across the real
screens, the same class of check test_player_smoke.py does for /.

Marked `browser`, excluded from the default suite -- launches Chromium, no
quota spent (pre-seeded cache, same technique as test_player_smoke.py).
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

# No `layout` key: this exercises the "never configured" fallback in both
# ytmatrix/config.py (Config.layout is None) and static/layout-page.js
# (DEFAULT_LAYOUT_CONFIG). With the real six screens in static/layout/screens.json,
# total=8/max_per_screen=3 resolves to F=3, and B/C/D/A/E=1 each -- verified in
# static/layout-fit.test.mjs's "real six-screen default" test.
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
        yield f"https://localhost:{port}/layout"
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_the_layout_wall_builds_one_player_per_resolved_cell(running_server):
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(running_server, wait_until="load")

        page.wait_for_selector(".cell iframe", timeout=20_000)
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        assert page.locator(".cell").count() == 8
        assert page.locator('.cell[data-empty="true"]').count() == 0
        browser.close()

    assert errors == [], f"page errors: {errors}"


def test_cells_are_positioned_within_the_canvas_bounds(running_server):
    """Every cell must land inside the visible grid area -- proof the percentage
    math (Task 4/7) is wired correctly, not just that 8 iframes exist somewhere."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function(
            "document.querySelectorAll('.cell iframe').length === 8", timeout=20_000
        )

        bounds = page.evaluate(
            """() => {
                const grid = document.getElementById('grid').getBoundingClientRect();
                return [...document.querySelectorAll('.cell')].map(cell => {
                    const c = cell.getBoundingClientRect();
                    return c.left >= grid.left - 1 && c.top >= grid.top - 1
                        && c.right <= grid.right + 1 && c.bottom <= grid.bottom + 1
                        && c.width > 0 && c.height > 0;
                });
            }"""
        )
        assert len(bounds) == 8
        assert all(bounds), f"a cell fell outside the grid: {bounds}"
        browser.close()


def test_pre_roll_and_mute_work_the_same_as_the_grid_page(running_server):
    """The shared engine's behavior, exercised once here as a spot check --
    the exhaustive coverage lives in test_player_smoke.py against /."""
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(running_server, wait_until="load")
        page.wait_for_function("window.__prerolled === true", timeout=40_000)

        assert page.evaluate("window.__players.every(p => p.isMuted())"), "should start muted"
        page.click("#play")
        page.click("#mute")
        page.wait_for_function("window.__players.every(p => !p.isMuted())", timeout=15_000)
        browser.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_layout_smoke.py -m browser -v`
Expected: FAIL — `404` on `/layout` if Task 9 has not landed, or a genuine assertion failure if it has but Tasks 1-8 have not; run this only after Tasks 1-9 are all committed, in which case it should already PASS on the first run (there is no red-first step here in the usual TDD sense, since this task validates the integration of everything preceding it rather than driving new production code).

- [ ] **Step 3: Run the full regression sweep**

```bash
uv run ruff check . && uv run ruff format .
uv run pytest tests/ -v
node --test 'static/*.test.mjs'
uv run pytest tests/test_player_smoke.py -m browser -v
uv run pytest tests/test_layout_smoke.py -m browser -v
```
Expected: everything PASSES, including the full, unmodified `test_player_smoke.py` suite — the proof `/` was never touched behaviorally.

- [ ] **Step 4: Commit**

```bash
git add tests/test_layout_smoke.py
git commit -m "test: browser smoke coverage for /layout"
```

---

## Self-Review

**Spec coverage:**
- "Where this lives" (branch, new files, `/layout` route, build-dist changes) → Tasks 6, 7, 9.
- Config schema (`total`, `max_per_screen`, `screens`, validation) → Task 5.
- Allocation algorithm → Task 2.
- `fitGrid` factor/partial-row behavior → Task 3.
- Front-end architecture (shared engine, seam) → Task 6.
- The composition of geometry + allocation + fit into placements → Task 4.
- Config-page editing → Task 8.
- Testing (node tests for pure logic, pydantic tests, browser smoke, full regression on `/`) → Tasks 1-5, 10.

**Type/name consistency check:** `computeLayout`, `containerStyle`, `cellRect`, `totalCells` are used with the same names and shapes across Tasks 6, 7, and their tests. `resolveLayout`, `allocateScreenCounts`, `screenRectPx`, `fitGrid` are each defined once (Tasks 1-4) and consumed with matching signatures in Task 7. `LayoutConfig`'s field names (`total`, `max_per_screen`, `screens`) match `layout-page.js`'s `DEFAULT_LAYOUT_CONFIG` and `config.js`'s `collect()`/`fill()` field names exactly.

**No placeholders:** every step above either shows the literal file content or gives an exact, unambiguous old/new diff anchored to real, already-quoted current file content.
