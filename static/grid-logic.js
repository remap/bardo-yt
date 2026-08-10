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

/**
 * Draw a fresh set of slots at random from the whole pool.
 *
 * The server's order is deliberate -- relevance, spread across countries,
 * stills pushed to the back -- so this is an explicit act, not the default,
 * and it is not persisted: a reload restores the ranked order. One search
 * returns 50 videos for eight cells, so there is a lot of unseen material
 * behind the wall and reshuffling it costs no quota at all.
 *
 * `random` is injected so the shuffle can be tested rather than eyeballed.
 */
export function shuffleSlots(pool, cellCount, random = Math.random) {
  // Deduplicated: the same video in two cells reads as a bug whatever put it
  // there. Ranked order rarely surfaces a repeat because duplicates sit far
  // apart in relevance; drawing at random from the whole pool does not have
  // that protection.
  const remaining = [...new Set(pool.filter(Boolean))];
  // Fisher-Yates over a copy: every ordering equally likely, no bias from
  // sort-with-random-comparator.
  for (let i = remaining.length - 1; i > 0; i -= 1) {
    const j = Math.floor(random() * (i + 1));
    [remaining[i], remaining[j]] = [remaining[j], remaining[i]];
  }
  return splitSlots(remaining, cellCount);
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
// Zoom is expressed relative to cover: 1 is "fills the cell, cropped", which
// is the resting state. Above 1 crops in further; below 1 pulls back toward
// seeing the whole frame.
export const ZOOM_MAX = 6;
// Cover (1) is where a cell STARTS, not the limit. Zooming out below it is
// deliberate -- it pulls back until the whole 16:9 frame is visible inside the
// cell. That fit point is the floor: past it you are only shrinking the video
// inside a growing field of black, which is nobody's idea of zoomed out.
export const ZOOM_STEP = 1.12;

export function coverRect(cellWidth, cellHeight, content = FULL_FRAME, zoom = 1) {
  const w = content.w > 0 ? content.w : 1;
  const h = content.h > 0 ? content.h : 1;
  const scale = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;

  // Smallest 16:9 iframe whose content region still covers the cell...
  const width =
    Math.max(cellWidth / w, (VIDEO_ASPECT_W * cellHeight) / (VIDEO_ASPECT_H * h)) * scale;
  const height = (width * VIDEO_ASPECT_H) / VIDEO_ASPECT_W;

  // ...centred on the content, so zooming pushes evenly outward rather than
  // drifting toward a corner.
  return {
    width,
    height,
    left: cellWidth / 2 - (content.x + w / 2) * width,
    top: cellHeight / 2 - (content.y + h / 2) * height,
  };
}

/**
 * The zoom at which the whole picture fits inside the cell -- the floor.
 *
 * At cover, one axis matches the cell exactly and the other overflows, so
 * this is always <= 1. For a cell that is already 16:9 it is exactly 1:
 * cover already shows everything and there is nothing to pull back to.
 */
export function minZoom(cellWidth, cellHeight, content = FULL_FRAME) {
  if (!(cellWidth > 0) || !(cellHeight > 0)) return 1;
  const cover = coverRect(cellWidth, cellHeight, content, 1);
  if (!(cover.width > 0) || !(cover.height > 0)) return 1;
  return Math.min(1, cellWidth / cover.width, cellHeight / cover.height);
}

export function nextZoom(current, deltaY, floor = 1, ceiling = ZOOM_MAX) {
  const from = Number.isFinite(current) && current > 0 ? current : 1;
  if (!Number.isFinite(deltaY) || deltaY === 0) return from;
  // Wheel up (negative deltaY) zooms in, matching every map and image viewer.
  const stepped = deltaY < 0 ? from * ZOOM_STEP : from / ZOOM_STEP;
  return Math.min(ceiling, Math.max(floor, stepped));
}

// A cell's view: how far zoomed in, and how far the picture has been pushed
// off centre to keep the pointer anchored. Zooming toward the cursor is what
// makes offsets exist at all -- centred zoom never needs them.
export const IDENTITY_VIEW = { zoom: 1, offsetX: 0, offsetY: 0 };

/**
 * Where the iframe actually goes, given a view.
 *
 * While the picture is larger than the cell the offset is clamped so no gap
 * can open at an edge. Once it is smaller -- zoomed out past cover, which is
 * intended -- it is centred instead, because no position covers and drifting
 * off to one side would just look broken.
 */
export function rectFor(cellWidth, cellHeight, content = FULL_FRAME, view = IDENTITY_VIEW) {
  const base = coverRect(cellWidth, cellHeight, content, view.zoom);
  const w = content.w > 0 ? content.w : 1;
  const h = content.h > 0 ? content.h : 1;

  const axis = (start, span, cellSpan, offset) => {
    const shown = span; // size of the content region along this axis
    if (shown <= cellSpan) return -start + (cellSpan - shown) / 2; // centre it
    const moved = start + offset;
    const clamped = Math.min(0, Math.max(cellSpan - shown, moved));
    return clamped - start;
  };

  const contentLeft = base.left + content.x * base.width;
  const contentTop = base.top + content.y * base.height;
  const dx = axis(contentLeft, w * base.width, cellWidth, view.offsetX);
  const dy = axis(contentTop, h * base.height, cellHeight, view.offsetY);

  return {
    width: base.width,
    height: base.height,
    left: base.left + dx,
    top: base.top + dy,
  };
}

/**
 * Drag the picture by a pixel delta.
 *
 * Deliberately unclamped: rectFor does the clamping, so the stored offset can
 * run past the edge and come back when the zoom changes. Clamping here would
 * make a drag that overshoots stick at the limit and then refuse to return.
 */
export function panBy(view, dx, dy) {
  const base = { ...IDENTITY_VIEW, ...(view ?? {}) };
  return {
    ...base,
    offsetX: base.offsetX + (Number.isFinite(dx) ? dx : 0),
    offsetY: base.offsetY + (Number.isFinite(dy) ? dy : 0),
  };
}

/**
 * Zoom one step toward or away from a point, keeping that point stationary.
 *
 * `pointerX`/`pointerY` are relative to the cell. The pixel under the cursor
 * before the step is the pixel under it after -- which is what makes wheel
 * zoom feel like inspecting rather than like the picture sliding around.
 */
export function zoomAt(
  view,
  cellWidth,
  cellHeight,
  content = FULL_FRAME,
  deltaY = 0,
  pointerX = 0,
  pointerY = 0,
  ceiling = ZOOM_MAX,
) {
  const floor = minZoom(cellWidth, cellHeight, content);
  const current = Number.isFinite(view?.zoom) && view.zoom > 0 ? view.zoom : 1;
  const zoom = nextZoom(current, deltaY, floor, ceiling);
  if (zoom === current) return { ...IDENTITY_VIEW, ...view, zoom };

  const before = rectFor(cellWidth, cellHeight, content, view ?? IDENTITY_VIEW);
  const ratio = zoom / current;
  // Solve for the placement that leaves the pointer over the same pixel.
  const wantLeft = pointerX - (pointerX - before.left) * ratio;
  const wantTop = pointerY - (pointerY - before.top) * ratio;

  const base = coverRect(cellWidth, cellHeight, content, zoom);
  return { zoom, offsetX: wantLeft - base.left, offsetY: wantTop - base.top };
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
