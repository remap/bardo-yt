// Pure grid/reserve bookkeeping. No DOM, no YouTube API -- so it can be
// tested with `node --test` and reasoned about on its own.

// Grid.cells is a Python @property on the pydantic model, which means it is
// NOT part of model_dump() and never reaches the browser. Deriving it here is
// the single source of truth on the JS side; reading `config.grid.cells`
// directly yields undefined and silently builds an empty grid.
export function cellCount(grid) {
  return grid.cols * grid.rows;
}

export function splitSlots(videoIds, cellCount) {
  const slots = [];
  for (let i = 0; i < cellCount; i += 1) {
    slots.push(i < videoIds.length ? videoIds[i] : null);
  }
  return { slots, reserves: videoIds.slice(cellCount) };
}

// `videoEmbeddable=true` is not reliable -- rights blocks, region limits and
// post-indexing takedowns all surface as onError at play time. When that
// happens we swap in a spare and rebuild only that one cell.
export function substituteFailedSlot(state, cellIndex) {
  const slots = [...state.slots];
  const reserves = [...state.reserves];
  const replacement = reserves.shift();
  slots[cellIndex] = replacement ?? null;
  return { slots, reserves, replaced: replacement !== undefined };
}

// A YouTube iframe always renders 16:9 internally and letterboxes itself
// inside any other shape. To fill a cell instead, oversize the iframe to the
// smallest 16:9 box that covers the cell and centre it; the cell clips the
// overflow. This is CSS `object-fit: cover` done by hand, because an iframe is
// not a replaced element and object-fit does nothing to it.
export const VIDEO_ASPECT_W = 16;
export const VIDEO_ASPECT_H = 9;

export const FULL_FRAME = { x: 0, y: 0, w: 1, h: 1 };

// `content` is where the real picture sits inside the player's 16:9 frame,
// normalised 0..1 -- a vertically-shot video arrives pillarboxed, so its
// content is a narrow centre column. Oversize the iframe until that *content*
// covers the cell, then offset so the content's centre lands on the cell's,
// pushing the black bars outside the crop entirely.
//
// With the default full frame this reduces exactly to plain cover-fit.
export function coverRect(cellWidth, cellHeight, content = FULL_FRAME) {
  const w = content.w > 0 ? content.w : 1;
  const h = content.h > 0 ? content.h : 1;

  // Smallest 16:9 iframe whose content region still covers the cell.
  const width = Math.max(cellWidth / w, (VIDEO_ASPECT_W * cellHeight) / (VIDEO_ASPECT_H * h));
  const height = (width * VIDEO_ASPECT_H) / VIDEO_ASPECT_W;

  return {
    width,
    height,
    left: cellWidth / 2 - (content.x + w / 2) * width,
    top: cellHeight / 2 - (content.y + h / 2) * height,
  };
}

// Looping by waiting for the ENDED event is what summons end-screen cards:
// by the time YouTube fires it, the suggestion grid is already drawn over the
// video. So restart just *before* the end instead and never let it finish.
export const LOOP_GUARD_SECONDS = 1.25;

export function shouldRestart(currentTime, duration, guard = LOOP_GUARD_SECONDS) {
  if (!Number.isFinite(currentTime) || !Number.isFinite(duration)) return false;
  // duration is 0 until metadata loads, and a live stream reports 0 forever.
  if (duration <= guard) return false;
  return currentTime >= duration - guard;
}

// Pre-roll: every player must have buffered something before any of them are
// allowed to start, so eight videos begin together rather than trickling in.
export const PREROLL_MIN_FRACTION = 0.01;

export function prerollComplete(fractions, minFraction = PREROLL_MIN_FRACTION) {
  if (fractions.length === 0) return true;
  return fractions.every((fraction) => (fraction ?? 0) >= minFraction);
}

// youtu.be takes the timestamp as whole seconds on `t`; fractional values are
// ignored by the player, so round rather than hand it something it drops.
export function videoUrl(videoId, seconds = null) {
  const base = `https://youtu.be/${videoId}`;
  if (seconds === null || !Number.isFinite(seconds) || seconds < 1) return base;
  return `${base}?t=${Math.floor(seconds)}`;
}

export function formatTimecode(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

const REBUILD_KEYS = ["grid"];
const IN_PLACE_KEYS = ["playback"];

function differs(previous, next, key) {
  return JSON.stringify(previous?.[key]) !== JSON.stringify(next?.[key]);
}

export function classifyConfigChange(previous, next) {
  if (!previous) return "rebuild";
  if (REBUILD_KEYS.some((key) => differs(previous, next, key))) return "rebuild";
  if (IN_PLACE_KEYS.some((key) => differs(previous, next, key))) return "in-place";
  return "none";
}
