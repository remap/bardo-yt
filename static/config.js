import { connectSocket } from "./socket.js";

const field = (id) => document.getElementById(id);
const quotaEl = field("quota");
const resultEl = field("result");

const layoutScreensEl = field("layout-screens");
let screenIds = [];

async function loadScreenIds() {
  try {
    const response = await fetch("/static/layout/screens.json");
    const data = await response.json();
    screenIds = data.screens.map((screen) => screen.id);
    layoutScreensEl.replaceChildren();
    for (const id of screenIds) {
      const label = document.createElement("label");
      label.textContent = `Screen ${id}`;
      const input = document.createElement("input");
      input.id = `layout_screen_${id}`;
      input.type = "text";
      input.placeholder = "auto";
      input.addEventListener("input", refreshQuota);
      label.appendChild(input);
      layoutScreensEl.appendChild(label);
    }
  } catch {
    // Silently degrade: if screens.json is unavailable, screenIds stays empty
    // and the Layout section shows only total/max_per_screen, no per-screen fields.
    screenIds = [];
    layoutScreensEl.replaceChildren();
  }
}

const TEXT_FIELDS = ["query", "order", "video_duration", "safe_search", "relevance_language"];
const NUMBER_FIELDS = ["cols", "rows", "start_offset", "layout_total", "layout_max_per_screen"];
const CHECK_FIELDS = ["loop", "autoplay_on_change"];

function fill(config) {
  loaded = config;
  field("query").value = config.query;
  field("order").value = config.search.order;
  field("video_duration").value = config.search.video_duration;
  field("safe_search").value = config.search.safe_search;
  field("relevance_language").value = config.search.relevance_language ?? "";
  field("cols").value = config.grid.cols;
  field("rows").value = config.grid.rows;
  field("start_offset").value = config.playback.start_offset;
  field("loop").checked = config.playback.loop;
  field("autoplay_on_change").checked = config.playback.autoplay_on_change;
  const layout = config.layout ?? { total: 8, max_per_screen: 3, screens: {} };
  field("layout_total").value = layout.total;
  field("layout_max_per_screen").value = layout.max_per_screen;
  for (const id of screenIds) {
    const el = field(`layout_screen_${id}`);
    if (el) el.value = String(layout.screens?.[id] ?? "auto");
  }
}

// The last config the server sent. Edits are layered onto THIS rather than a
// fresh object, so sections this page has no fields for -- query_generation,
// filtering, quota -- survive a save untouched. Rebuilding the payload from
// scratch is what silently switched query generation off.
let loaded = null;

function collect() {
  const language = field("relevance_language").value.trim();
  return {
    ...(loaded ?? {}),
    query: field("query").value,
    grid: { cols: Number(field("cols").value), rows: Number(field("rows").value) },
    search: {
      ...(loaded?.search ?? {}),
      order: field("order").value,
      video_duration: field("video_duration").value,
      safe_search: field("safe_search").value,
      relevance_language: language === "" ? null : language,
    },
    playback: {
      ...(loaded?.playback ?? {}),
      autoplay_on_change: field("autoplay_on_change").checked,
      start_offset: Number(field("start_offset").value),
      loop: field("loop").checked,
    },
    layout: {
      total: Number(field("layout_total").value),
      max_per_screen: Number(field("layout_max_per_screen").value),
      // Merged onto the previously-loaded screens, not rebuilt from scratch:
      // screenIds comes from the current screens.json snapshot, which may be
      // narrower than what was actually saved (loadScreenIds() degraded to []
      // on a fetch failure, or a screen id was retired from screens.json but
      // still has a saved override). Rebuilding from screenIds alone would
      // silently drop those on the next Save -- the same class of bug the
      // comment above collect() describes for query_generation.
      screens: {
        ...(loaded?.layout?.screens ?? {}),
        ...Object.fromEntries(
          screenIds.map((id) => {
            const raw = field(`layout_screen_${id}`).value.trim();
            return [id, raw === "" || raw === "auto" ? "auto" : raw === "none" ? "none" : Number(raw)];
          }),
        ),
      },
    },
  };
}

// You get 100 searches a day. Never make the operator guess whether a save
// costs one.
async function refreshQuota() {
  try {
    const response = await fetch("/api/cache-status", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(collect()),
    });
    if (!response.ok) {
      quotaEl.textContent = "invalid settings — fix the fields below before saving";
      quotaEl.dataset.cost = "100";
      return;
    }
    const {
      would_hit: hit,
      quota_cost: cost,
      units_spent_today: spent,
      daily_limit_units: limit,
    } = await response.json();
    // The numbers come from the server rather than being written in here. The
    // ceiling that actually stops a save is `quota.daily_limit_units` from the
    // shared config, which is not Google's 10,000 and can be edited on this
    // very page; naming the wrong one is worse than naming none.
    //
    // "once, however many walls are open" is a claim about `resolve_videos`'s
    // single-flight: a search-affecting save makes every connected browser
    // refetch at once, and without that guard the true cost would be this
    // number times the number of open walls (CLAUDE.md gotcha 29).
    quotaEl.textContent = hit
      ? "cached — saving costs 0 quota units"
      : `not cached — saving spends ${cost} units once, however many walls are ` +
        `open (${spent} of ${limit} used today)`;
    quotaEl.dataset.cost = String(cost);
  } catch {
    quotaEl.textContent = "could not reach the server";
    quotaEl.dataset.cost = "100";
  }
}

function describe(detail) {
  if (!Array.isArray(detail)) return String(detail);
  return detail.map((e) => `${(e.loc ?? []).join(".")}: ${e.msg}`).join("\n");
}

field("save").addEventListener("click", async () => {
  resultEl.textContent = "saving…";
  resultEl.dataset.state = "";
  const response = await fetch("/api/config", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(collect()),
  });
  if (response.ok) {
    resultEl.textContent = "saved — the wall is updating";
    resultEl.dataset.state = "ok";
  } else {
    const body = await response.json().catch(() => ({ detail: `error ${response.status}` }));
    resultEl.textContent = describe(body.detail);
    resultEl.dataset.state = "error";
  }
  refreshQuota();
});

for (const id of [...TEXT_FIELDS, ...NUMBER_FIELDS]) {
  field(id).addEventListener("input", refreshQuota);
}
for (const id of CHECK_FIELDS) {
  field(id).addEventListener("change", refreshQuota);
}

async function resync() {
  await loadScreenIds();
  fill(await (await fetch("/api/config")).json());
  refreshQuota();
}

connectSocket({
  onReconnect: resync,
  onMessage: (message) => {
    // Another editor saved; adopt their values rather than silently diverging.
    if (message.type === "config") {
      fill(message.config);
      refreshQuota();
    }
  },
});
