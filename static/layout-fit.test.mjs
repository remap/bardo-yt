import { test } from "node:test";
import assert from "node:assert/strict";
import { screenRectPx, allocateScreenCounts } from "./layout-fit.js";

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
