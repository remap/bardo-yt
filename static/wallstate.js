// What this browser is watching, and what it has already seen.
//
// This used to be ytmatrix/wallstate.py, one file for the whole installation.
// It lives here now because config is shared but walls are not: everyone edits
// the same config document, and everyone still gets their own query. Moving it
// into localStorage is what makes that true without the server holding a
// single scrap of per-user state.
//
// Every access is defensive. Storage is missing in some embedding contexts,
// throws on write in Safari's private mode, and can contain anything a
// previous version or a curious user left behind -- and none of that is
// allowed to stop the wall from starting.

const QUERY_KEY = "ytmatrix.query";
const HISTORY_KEY = "ytmatrix.history";

// Bounds the history that steers Gemini away from repeats. Without a cap this
// grows forever in a browser that is never cleared.
export const MAX_HISTORY = 200;

function defaultStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    // Accessing localStorage itself throws when storage is blocked.
    return null;
  }
}

function read(storage, key) {
  const target = storage === undefined ? defaultStorage() : storage;
  try {
    return target?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(storage, key, value) {
  const target = storage === undefined ? defaultStorage() : storage;
  try {
    if (value === null) target?.removeItem(key);
    else target?.setItem(key, value);
  } catch {
    // A full or blocked quota costs us the memory, not the wall.
  }
}

export function loadQuery(storage) {
  const value = read(storage, QUERY_KEY);
  return value && value.trim() ? value : null;
}

export function saveQuery(query, storage) {
  const trimmed = typeof query === "string" ? query.trim() : "";
  write(storage, QUERY_KEY, trimmed || null);
}

export function clearQuery(storage) {
  write(storage, QUERY_KEY, null);
}

export function loadHistory(storage) {
  const raw = read(storage, HISTORY_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

export function pushHistory(query, storage) {
  const trimmed = typeof query === "string" ? query.trim() : "";
  if (!trimmed) return;
  const history = [...loadHistory(storage), trimmed].slice(-MAX_HISTORY);
  write(storage, HISTORY_KEY, JSON.stringify(history));
}
