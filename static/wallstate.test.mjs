import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_HISTORY,
  clearQuery,
  loadHistory,
  loadQuery,
  pushHistory,
  saveQuery,
  clearWall,
  loadWall,
  saveWall,
} from "./wallstate.js";

// node has no localStorage, and injecting one keeps these tests honest about
// the fact that every read can fail -- Safari in private mode throws on write.
function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
  };
}

test("a fresh browser has no query", () => {
  assert.equal(loadQuery(fakeStorage()), null);
});

test("a saved query is read back", () => {
  const storage = fakeStorage();
  saveQuery("케이팝 커버", storage);
  assert.equal(loadQuery(storage), "케이팝 커버");
});

test("clearing removes the query", () => {
  const storage = fakeStorage();
  saveQuery("q", storage);
  clearQuery(storage);
  assert.equal(loadQuery(storage), null);
});

test("a blank query saves as nothing", () => {
  const storage = fakeStorage();
  saveQuery("   ", storage);
  assert.equal(loadQuery(storage), null);
});

test("a fresh browser has an empty history", () => {
  assert.deepEqual(loadHistory(fakeStorage()), []);
});

test("history accumulates oldest first", () => {
  const storage = fakeStorage();
  pushHistory("a", storage);
  pushHistory("b", storage);
  assert.deepEqual(loadHistory(storage), ["a", "b"]);
});

test("history is capped so it cannot grow forever", () => {
  const storage = fakeStorage();
  for (let i = 0; i < MAX_HISTORY + 50; i += 1) pushHistory(`q${i}`, storage);
  const history = loadHistory(storage);
  assert.equal(history.length, MAX_HISTORY);
  assert.equal(history.at(-1), `q${MAX_HISTORY + 49}`);
});

test("corrupt stored history reads as empty rather than throwing", () => {
  assert.deepEqual(loadHistory(fakeStorage({ "ytmatrix.history": "not json" })), []);
});

test("stored history of the wrong shape reads as empty", () => {
  assert.deepEqual(loadHistory(fakeStorage({ "ytmatrix.history": '{"a":1}' })), []);
});

test("a storage that throws on write does not break the wall", () => {
  const hostile = {
    getItem: () => null,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {},
  };
  assert.doesNotThrow(() => saveQuery("q", hostile));
  assert.doesNotThrow(() => pushHistory("q", hostile));
});

test("a missing storage does not break the wall", () => {
  assert.equal(loadQuery(null), null);
  assert.deepEqual(loadHistory(null), []);
  assert.doesNotThrow(() => saveQuery("q", null));
});

// --- the wall itself -------------------------------------------------------

const MESSAGE = {
  type: "videos",
  query: "golden cover",
  video_ids: ["a", "b", "c"],
  reserves: ["d", "e"],
  titles: { a: "A", b: "B" },
  from_cache: false,
  units_spent_today: 100,
  timings: { motion: 3.3, total: 4.8 },
};

test("a fresh browser has no remembered wall", () => {
  assert.equal(loadWall(fakeStorage()), null);
});

test("a wall round-trips", () => {
  const storage = fakeStorage();
  saveWall(MESSAGE, storage);
  const back = loadWall(storage);
  assert.deepEqual(back.video_ids, ["a", "b", "c"]);
  assert.deepEqual(back.reserves, ["d", "e"]);
  assert.equal(back.query, "golden cover");
  assert.deepEqual(back.titles, { a: "A", b: "B" });
});

test("timings are not restored, because they described a request that is over", () => {
  const storage = fakeStorage();
  saveWall(MESSAGE, storage);
  assert.equal(loadWall(storage).timings, undefined);
});

test("a restored wall says so, so a log can tell it from a fetch", () => {
  const storage = fakeStorage();
  saveWall(MESSAGE, storage);
  assert.equal(loadWall(storage).restored, true);
});

test("clearing removes the wall", () => {
  const storage = fakeStorage();
  saveWall(MESSAGE, storage);
  clearWall(storage);
  assert.equal(loadWall(storage), null);
});

test("something without video_ids is not a wall", () => {
  assert.equal(loadWall(fakeStorage({ "ytmatrix.wall": '{"query":"x"}' })), null);
});

test("a corrupt stored wall reads as absent rather than throwing", () => {
  assert.equal(loadWall(fakeStorage({ "ytmatrix.wall": "not json" })), null);
});

test("saving something that is not a message is refused", () => {
  const storage = fakeStorage();
  saveWall({ query: "no ids here" }, storage);
  assert.equal(loadWall(storage), null);
});

test("a storage that throws on write does not break the wall", () => {
  const hostile = {
    getItem: () => null,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {},
  };
  assert.doesNotThrow(() => saveWall(MESSAGE, hostile));
});

