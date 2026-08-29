import { startWall } from "./wall-engine.js";
import { resolveLayout } from "./layout-fit.js";

// Mirrors ytmatrix/config.py's LayoutConfig defaults (total=8,
// max_per_screen=3, screens={}) -- used only when config.layout is entirely
// absent, which it is until someone edits it on the config page.
const DEFAULT_LAYOUT_CONFIG = { total: 8, max_per_screen: 3, screens: {} };

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
  return {
    totalCells: resolved.totalCells,
    containerStyle: {
      display: "block",
      position: "relative",
      width: "100%",
      height: "100%",
    },
    cellRect: (index) => {
      const placement = resolved.placements[index];
      // Defensive: Object.assign(el.style, undefined) silently no-ops rather
      // than throwing, so a missing placement would otherwise look like an
      // invisible or corrupted layout with no error at all.
      if (!placement) return { display: "none" };
      return {
        position: "absolute",
        left: `${(placement.left / resolved.canvas.width) * 100}%`,
        top: `${(placement.top / resolved.canvas.height) * 100}%`,
        width: `${(placement.width / resolved.canvas.width) * 100}%`,
        height: `${(placement.height / resolved.canvas.height) * 100}%`,
      };
    },
  };
}

await loadScreens();
startWall({ computeLayout });
