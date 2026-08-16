import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_HISTORY,
  clearQuery,
  loadHistory,
  loadQuery,
  pushHistory,
  saveQuery,
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
