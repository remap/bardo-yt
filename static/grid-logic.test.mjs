import { test } from "node:test";
import assert from "node:assert/strict";
import {
  splitSlots,
  substituteFailedSlot,
  classifyConfigChange,
  cellCount,
  coverRect,
  shouldRestart,
  prerollComplete,
  LOOP_GUARD_SECONDS,
  videoUrl,
  formatTimecode,
} from "./grid-logic.js";

test("videoUrl includes the timestamp in whole seconds", () => {
  assert.equal(videoUrl("abc123", 96.7), "https://youtu.be/abc123?t=96");
});

test("videoUrl omits the timestamp at the very start", () => {
  // ?t=0 is noise; a bare link means the same thing.
  assert.equal(videoUrl("abc123", 0), "https://youtu.be/abc123");
  assert.equal(videoUrl("abc123", 0.4), "https://youtu.be/abc123");
});

test("videoUrl omits the timestamp when there is no reading", () => {
  assert.equal(videoUrl("abc123"), "https://youtu.be/abc123");
  assert.equal(videoUrl("abc123", null), "https://youtu.be/abc123");
  assert.equal(videoUrl("abc123", NaN), "https://youtu.be/abc123");
});

test("formatTimecode renders minutes and seconds", () => {
  assert.equal(formatTimecode(0), "0:00");
  assert.equal(formatTimecode(9), "0:09");
  assert.equal(formatTimecode(96.7), "1:36");
  assert.equal(formatTimecode(600), "10:00");
});

test("formatTimecode renders hours only when there are some", () => {
  assert.equal(formatTimecode(3661), "1:01:01");
  assert.equal(formatTimecode(3599), "59:59");
});

test("formatTimecode survives a missing reading", () => {
  assert.equal(formatTimecode(NaN), "0:00");
  assert.equal(formatTimecode(-5), "0:00");
});

// Looping before the end, so end-screen cards never get drawn.

test("shouldRestart is false in the middle of a video", () => {
  assert.equal(shouldRestart(30, 240), false);
});

test("shouldRestart becomes true just before the end", () => {
  assert.equal(shouldRestart(240 - LOOP_GUARD_SECONDS, 240), true);
  assert.equal(shouldRestart(239.9, 240), true);
});

test("shouldRestart fires before the ENDED event would", () => {
  // The whole point: never reach the end, because that is when YouTube draws
  // the suggestion grid over the video.
  assert.equal(shouldRestart(238.9, 240), true, "should restart with time to spare");
});

test("shouldRestart is false while duration is still unknown", () => {
  // duration reads 0 until metadata loads; restarting then would loop forever.
  assert.equal(shouldRestart(0, 0), false);
  assert.equal(shouldRestart(5, 0), false);
});

test("shouldRestart is false for a live stream reporting no duration", () => {
  assert.equal(shouldRestart(9999, 0), false);
});

test("shouldRestart ignores a video shorter than the guard", () => {
  assert.equal(shouldRestart(0.5, 1), false);
});

test("shouldRestart rejects non-finite readings", () => {
  assert.equal(shouldRestart(NaN, 240), false);
  assert.equal(shouldRestart(10, NaN), false);
  assert.equal(shouldRestart(10, Infinity), false);
});

// Pre-roll gating.

test("prerollComplete is false while any player has buffered nothing", () => {
  assert.equal(prerollComplete([0.5, 0.2, 0]), false);
});

test("prerollComplete is true once every player has buffered", () => {
  assert.equal(prerollComplete([0.05, 0.02, 0.3]), true);
});

test("prerollComplete treats a missing reading as not ready", () => {
  assert.equal(prerollComplete([0.5, null, 0.5]), false);
  assert.equal(prerollComplete([0.5, undefined]), false);
});

test("prerollComplete is true for an empty wall rather than hanging", () => {
  assert.equal(prerollComplete([]), true);
});

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

// Pushing past detected black bars: the *content* must cover the cell, not the
// 16:9 frame that contains it.

const PILLARBOXED = { x: 0.34, y: 0, w: 0.32, h: 1 }; // a 9:16 video in a 16:9 frame

test("coverRect zooms past pillarbox bars so content fills the cell", () => {
  const cellW = 320;
  const cellH = 180;
  const r = coverRect(cellW, cellH, PILLARBOXED);
  const contentW = PILLARBOXED.w * r.width;
  const contentH = PILLARBOXED.h * r.height;
  assert.ok(contentW >= cellW - 0.001, `content width ${contentW} < cell ${cellW}`);
  assert.ok(contentH >= cellH - 0.001, `content height ${contentH} < cell ${cellH}`);
});

test("coverRect centres the content region, not the frame", () => {
  const cellW = 320;
  const cellH = 180;
  const r = coverRect(cellW, cellH, PILLARBOXED);
  const contentCentreX = r.left + (PILLARBOXED.x + PILLARBOXED.w / 2) * r.width;
  const contentCentreY = r.top + (PILLARBOXED.y + PILLARBOXED.h / 2) * r.height;
  assert.ok(Math.abs(contentCentreX - cellW / 2) < 0.001, contentCentreX);
  assert.ok(Math.abs(contentCentreY - cellH / 2) < 0.001, contentCentreY);
});

test("coverRect pushes the bars outside the cell entirely", () => {
  const r = coverRect(320, 180, PILLARBOXED);
  // Left edge of the content must be at or left of the cell's left edge,
  // meaning the bar beside it is off-screen.
  const contentLeft = r.left + PILLARBOXED.x * r.width;
  const contentRight = contentLeft + PILLARBOXED.w * r.width;
  assert.ok(contentLeft <= 0.001, `bar visible on the left: ${contentLeft}`);
  assert.ok(contentRight >= 320 - 0.001, `bar visible on the right: ${contentRight}`);
});

test("coverRect zooms past letterbox bars on an ultrawide source", () => {
  const ultrawide = { x: 0, y: 0.13, w: 1, h: 0.74 };
  const r = coverRect(400, 400, ultrawide);
  assert.ok(ultrawide.w * r.width >= 400 - 0.001);
  assert.ok(ultrawide.h * r.height >= 400 - 0.001);
});

test("coverRect with an explicit full frame equals the no-argument form", () => {
  assert.deepEqual(coverRect(640, 360, { x: 0, y: 0, w: 1, h: 1 }), coverRect(640, 360));
});

test("coverRect survives a degenerate content box without NaN", () => {
  const r = coverRect(320, 180, { x: 0, y: 0, w: 0, h: 0 });
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
