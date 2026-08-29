import { startWall } from "./wall-engine.js";
import { resolveLayout } from "./layout-fit.js";

// Mirrors ytmatrix/config.py's LayoutConfig defaults (total=8,
// max_per_screen=3, screens={}) -- used only when config.layout is entirely
// absent, which it is until someone edits it on the config page.
const DEFAULT_LAYOUT_CONFIG = { total: 8, max_per_screen: 3, screens: {} };

let screensData = null;

async function loadScreens() {
  const response = await fetch("/static/layout/screens.json");
  screensData = await response.json();
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
