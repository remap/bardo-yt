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

  return {
    canvas,
    totalCells: placements.length,
    placements,
    // Keyed by screen id, for cellTransformStyle -- a per-screen shift/scale
    // needs each screen's own true pixel boundary to clip against, which a
    // flat placements list alone does not carry (a screen with zero
    // resolved cells this round would otherwise have no rect available at
    // all for its still-configured shift).
    screenRects: Object.fromEntries(rects.map(({ id, rect }) => [id, rect])),
  };
}

/**
 * Per-cell CSS transform + clip-path for one screen's operator-adjustable
 * shift/stretch/squish (LayoutConfig.screen_transforms, keyed by screen id).
 *
 * The scale is anchored at the SCREEN's own center in absolute canvas
 * coordinates, not each cell's own center, by pointing transform-origin at
 * the screen's center expressed in that cell's local box coordinates --
 * every cell belonging to the same screen therefore scales together as one
 * rigid block. This deliberately avoids introducing a per-screen wrapper
 * element: wall-engine.js indexes #grid's children directly and flatly
 * (gridEl.children[index]) in a few dozen places, and nesting cells under a
 * screen container would break every one of them.
 *
 * clip-path is computed in the cell's own PRE-transform coordinate space,
 * because clip-path is applied before the transform in the CSS painting
 * model -- so this inverts the shift+scale to find which pre-transform
 * positions would land outside the screen's true pixel boundary once
 * transformed, and clips exactly that much off. A cell fully spilled past
 * the boundary clamps to a 100% inset (fully clipped) rather than an
 * inset beyond the box.
 *
 * The identity transform (the default -- an unconfigured screen) short-
 * circuits to "none"/"none": cheaper, and avoids clip-path rounding
 * artifacts landing a hairline off 0% on an otherwise-untouched screen.
 */
export function cellTransformStyle(placement, screenRect, transform) {
  // snake_case, matching ScreenTransform's own field names (ytmatrix/config.py)
  // and every other config field at this boundary (offset_x, max_per_screen,
  // ...) -- this object is the config's screen_transforms[screenId] entry
  // passed straight through with no renaming, not a JS-side type of its own.
  const { x: dx = 0, y: dy = 0, scale_x: scaleX = 1, scale_y: scaleY = 1 } = transform ?? {};
  if (dx === 0 && dy === 0 && scaleX === 1 && scaleY === 1) {
    return { transform: "none", clipPath: "none" };
  }

  const anchorX = screenRect.x + screenRect.width / 2;
  const anchorY = screenRect.y + screenRect.height / 2;

  const pct = (value) => Number(value.toFixed(3));
  const clamp100 = (value) => Math.min(100, Math.max(0, value));

  const translateXPercent = pct((dx / placement.width) * 100);
  const translateYPercent = pct((dy / placement.height) * 100);
  const originXPercent = pct(((anchorX - placement.left) / placement.width) * 100);
  const originYPercent = pct(((anchorY - placement.top) / placement.height) * 100);

  // Where, in absolute pre-transform canvas coordinates, does the screen's
  // own boundary come from? Inverting newAbs = anchor + d + scale*(abs - anchor).
  const leftBoundary = anchorX + (screenRect.x - anchorX - dx) / scaleX;
  const rightBoundary = anchorX + (screenRect.x + screenRect.width - anchorX - dx) / scaleX;
  const topBoundary = anchorY + (screenRect.y - anchorY - dy) / scaleY;
  const bottomBoundary = anchorY + (screenRect.y + screenRect.height - anchorY - dy) / scaleY;

  const leftInset = pct(clamp100(((leftBoundary - placement.left) / placement.width) * 100));
  const rightInset = pct(
    clamp100(((placement.left + placement.width - rightBoundary) / placement.width) * 100),
  );
  const topInset = pct(clamp100(((topBoundary - placement.top) / placement.height) * 100));
  const bottomInset = pct(
    clamp100(((placement.top + placement.height - bottomBoundary) / placement.height) * 100),
  );

  return {
    transform: `translate(${translateXPercent}%, ${translateYPercent}%) scale(${scaleX}, ${scaleY})`,
    transformOrigin: `${originXPercent}% ${originYPercent}%`,
    clipPath: `inset(${topInset}% ${rightInset}% ${bottomInset}% ${leftInset}%)`,
  };
}
