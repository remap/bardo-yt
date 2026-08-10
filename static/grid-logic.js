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
