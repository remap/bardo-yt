import { test } from "node:test";
import assert from "node:assert/strict";
import { screenRectPx, allocateScreenCounts, fitGrid, resolveLayout } from "./layout-fit.js";

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
  assert.equal(counts.C, 10); // clamped to maxPerScreen; remaining 6 units unused
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
