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
  if (totalArea > 0) {
    let allocated = 0;
    for (const id of autoIds) {
      const share = Math.floor((remaining * (screenAreas[id] ?? 0)) / totalArea);
      shares[id] = Math.min(share, maxPerScreen);
      allocated += shares[id];
    }

    // Distribute leftover to largest screens first, respecting maxPerScreen.
    // The guard bounds the loop at "one full pass per unit of leftover" so a
    // maxPerScreen of 0 (nothing left to give) or every screen already capped
    // cannot spin forever.
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
  } else {
    // totalArea is 0, so no screens have area - allocate nothing to each auto screen
    for (const id of autoIds) {
      shares[id] = 0;
    }
  }

  for (const id of autoIds) counts[id] = shares[id];
  return counts;
}

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
