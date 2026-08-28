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
