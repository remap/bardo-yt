import { startWall } from "./wall-engine.js";
import { resolveLayout, cellTransformStyle } from "./layout-fit.js";

// Mirrors ytmatrix/config.py's LayoutConfig defaults (total=8,
// max_per_screen=3, screens={}, offset_x=0, offset_y=0, screen_transforms={})
// -- used only when config.layout is entirely absent, which it is until
// someone edits it on the config page.
const DEFAULT_LAYOUT_CONFIG = {
  total: 8,
  max_per_screen: 3,
  screens: {},
  offset_x: 0,
  offset_y: 0,
  screen_transforms: {},
};

let screensData = null;

// A 404 or malformed JSON here must not leave the page hung on "loading…"
// with an unhandled rejection and nothing visible -- the same failure mode
// wall-engine.js's own resync() guards against for /api/config ("Better to
// say so than to hang on a blank page"). Re-throwing after setting the status
// is deliberate: the caller awaits this before startWall({ computeLayout }),
// so a throw here is what stops startWall from running with no screens to
// build a layout against.
async function loadScreens() {
  try {
    const response = await fetch("/static/layout/screens.json");
    if (!response.ok) throw new Error(`${response.status}`);
    screensData = await response.json();
  } catch (error) {
    document.getElementById("status").textContent =
      "could not load screen geometry — reload to try again";
    throw error;
  }
}

function computeLayout(config) {
  const layoutConfig = config.layout ?? DEFAULT_LAYOUT_CONFIG;
  const resolved = resolveLayout(screensData, layoutConfig);
  // A single transform on the whole container, not a change to any
  // placement's own left/top -- shifts every screen at once, in real
  // rendered pixels, without touching resolveLayout's per-cell math.
  const offsetX = layoutConfig.offset_x ?? 0;
  const offsetY = layoutConfig.offset_y ?? 0;
  return {
    totalCells: resolved.totalCells,
    // Every screen id screens.json defines, regardless of how many cells
    // each currently has -- unlike resolved.placements, which skips a
    // screen entirely once its resolved count is 0 (gotcha: a screen set to
    // "none" would otherwise vanish from /layout-control's per-screen shift
    // UI right when an operator most wants to leave its calibration alone
    // and just stop feeding it video).
    allScreenIds: screensData.screens.map((screen) => screen.id),
    containerStyle: {
      display: "block",
      position: "relative",
      width: "100%",
      height: "100%",
      transform: `translate(${offsetX}px, ${offsetY}px)`,
    },
    cellRect: (index) => {
      const placement = resolved.placements[index];
      // Defensive: Object.assign(el.style, undefined) silently no-ops rather
      // than throwing, so a missing placement would otherwise look like an
      // invisible or corrupted layout with no error at all.
      if (!placement) return { display: "none" };
      const screenTransform = layoutConfig.screen_transforms?.[placement.screenId];
      const screenRect = resolved.screenRects[placement.screenId];
      const { transform, transformOrigin, clipPath } = cellTransformStyle(
        placement,
        screenRect,
        screenTransform,
      );
      return {
        position: "absolute",
        left: `${(placement.left / resolved.canvas.width) * 100}%`,
        top: `${(placement.top / resolved.canvas.height) * 100}%`,
        width: `${(placement.width / resolved.canvas.width) * 100}%`,
        height: `${(placement.height / resolved.canvas.height) * 100}%`,
        transform,
        transformOrigin: transformOrigin ?? "50% 50%",
        clipPath,
      };
    },
    // Which screen this cell belongs to -- a data field, not a style, so it
    // lives alongside cellRect rather than inside its returned style object.
    // /layout-control uses this to label each cell with its screen's letter.
    screenIdForCell: (index) => resolved.placements[index]?.screenId ?? null,
  };
}

await loadScreens();
startWall({ computeLayout, controlChannel: "yt-matrix-layout-control" });
