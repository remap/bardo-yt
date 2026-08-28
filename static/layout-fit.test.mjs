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
