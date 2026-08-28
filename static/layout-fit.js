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
