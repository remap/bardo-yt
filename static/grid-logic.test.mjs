import { test } from "node:test";
import assert from "node:assert/strict";
import {
  splitSlots,
  substituteFailedSlot,
  classifyConfigChange,
  cellCount,
  coverRect,
} from "./grid-logic.js";

// Cover-fit: fill the cell in both axes and crop the overflow, centered.
// A YouTube iframe is always 16:9 internally, so any cell that is not 16:9
// letterboxes unless the iframe is deliberately oversized and clipped.

test("coverRect fills exactly when the cell is already 16:9", () => {
  const r = coverRect(1600, 900);
  assert.deepEqual(r, { width: 1600, height: 900, left: 0, top: 0 });
});

test("coverRect overflows vertically for a cell wider than 16:9", () => {
  // 1600x400 is wider than 16:9, so width binds and height spills over.
  const r = coverRect(1600, 400);
  assert.equal(r.width, 1600);
  assert.equal(r.height, 900);
  assert.equal(r.left, 0);
  assert.equal(r.top, -250); // (400 - 900) / 2 -- centered crop
});

test("coverRect overflows horizontally for a cell taller than 16:9", () => {
  const r = coverRect(900, 900);
  assert.equal(r.height, 900);
  assert.equal(r.width, 1600);
  assert.equal(r.top, 0);
  assert.equal(r.left, -350); // (900 - 1600) / 2
});

test("coverRect never leaves a gap in either axis", () => {
  for (const [w, h] of [[400, 200], [200, 400], [1920, 1080], [333, 777], [1000, 1]]) {
    const r = coverRect(w, h);
    assert.ok(r.width >= w - 0.001, `width ${r.width} < cell ${w}`);
    assert.ok(r.height >= h - 0.001, `height ${r.height} < cell ${h}`);
  }
});

test("coverRect centers the overflow evenly", () => {
  const r = coverRect(600, 200);
  assert.equal(r.left * 2 + r.width, 600 + (r.width - 600) - (r.width - 600));
  // Overflow above equals overflow below.
  assert.equal(r.top, (200 - r.height) / 2);
});

test("coverRect handles a zero-sized cell without producing NaN", () => {
  const r = coverRect(0, 0);
  for (const v of Object.values(r)) assert.ok(Number.isFinite(v), `${v} is not finite`);
});

// `cells` is a Python @property on the Grid model, so it is absent from the
// serialized config. Reading config.grid.cells returned undefined, splitSlots
// built zero slots, and the wall rendered blank with no error.
test("cellCount derives the total from cols and rows", () => {
  assert.equal(cellCount({ cols: 4, rows: 2 }), 8);
  assert.equal(cellCount({ cols: 3, rows: 1 }), 3);
});

test("cellCount does not depend on a `cells` key being serialized", () => {
  const grid = { cols: 2, rows: 2 };
  assert.equal("cells" in grid, false);
  assert.equal(cellCount(grid), 4);
});

test("splitSlots with an undefined count builds nothing -- the blank-wall bug", () => {
  // Guards the contract cellCount exists to satisfy: passing undefined here
  // silently produces an empty grid rather than throwing.
  const { slots } = splitSlots(["a", "b"], undefined);
  assert.deepEqual(slots, []);
});

const ids = (n, prefix = "v") =>
  Array.from({ length: n }, (_, i) => `${prefix}${String(i).padStart(3, "0")}`);

test("splitSlots fills the grid and keeps the rest as reserves", () => {
  const { slots, reserves } = splitSlots(ids(50), 8);
  assert.equal(slots.length, 8);
  assert.equal(reserves.length, 42);
  assert.equal(slots[0], "v000");
  assert.equal(reserves[0], "v008");
});

test("splitSlots pads with null when there are too few results", () => {
  const { slots, reserves } = splitSlots(ids(3), 8);
  assert.deepEqual(slots.slice(3), [null, null, null, null, null]);
  assert.deepEqual(reserves, []);
});

test("substituteFailedSlot pulls the next reserve into the failed cell", () => {
  const state = splitSlots(ids(10), 8);
  const next = substituteFailedSlot(state, 2);
  assert.equal(next.replaced, true);
  assert.equal(next.slots[2], "v008");
  assert.deepEqual(next.reserves, ["v009"]);
});

test("substituteFailedSlot leaves the other cells untouched", () => {
  const state = splitSlots(ids(10), 8);
  const next = substituteFailedSlot(state, 2);
  for (const i of [0, 1, 3, 4, 5, 6, 7]) {
    assert.equal(next.slots[i], state.slots[i]);
  }
});

test("substituteFailedSlot empties the cell once reserves are exhausted", () => {
  const state = splitSlots(ids(8), 8);
  const next = substituteFailedSlot(state, 5);
  assert.equal(next.replaced, false);
  assert.equal(next.slots[5], null);
});

test("substituteFailedSlot does not mutate the state it was given", () => {
  const state = splitSlots(ids(10), 8);
  substituteFailedSlot(state, 0);
  assert.equal(state.slots[0], "v000");
  assert.equal(state.reserves.length, 2);
});

const base = () => ({
  grid: { cols: 4, rows: 2 },
  playback: { muted: true, autoplay_on_change: true, start_offset: 0, loop: true },
});

test("classifyConfigChange asks for a rebuild when there is no previous config", () => {
  assert.equal(classifyConfigChange(null, base()), "rebuild");
});

test("classifyConfigChange asks for a rebuild when the grid changes", () => {
  const next = { ...base(), grid: { cols: 2, rows: 2 } };
  assert.equal(classifyConfigChange(base(), next), "rebuild");
});

test("classifyConfigChange applies a start offset in place", () => {
  const next = { ...base(), playback: { ...base().playback, start_offset: 30 } };
  assert.equal(classifyConfigChange(base(), next), "in-place");
});

test("classifyConfigChange applies a loop toggle in place", () => {
  const next = { ...base(), playback: { ...base().playback, loop: false } };
  assert.equal(classifyConfigChange(base(), next), "in-place");
});

test("classifyConfigChange reports no change for an identical config", () => {
  assert.equal(classifyConfigChange(base(), base()), "none");
});

test("classifyConfigChange ignores fields the player does not render", () => {
  const previous = { ...base(), query: "a" };
  const next = { ...base(), query: "b" };
  // A query change arrives as a separate `videos` message; the config diff
  // alone must not trigger a second, redundant rebuild.
  assert.equal(classifyConfigChange(previous, next), "none");
});
