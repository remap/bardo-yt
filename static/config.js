import { connectSocket } from "./socket.js";

const field = (id) => document.getElementById(id);
const quotaEl = field("quota");
const resultEl = field("result");

const TEXT_FIELDS = ["query", "order", "video_duration", "safe_search", "relevance_language"];
const NUMBER_FIELDS = ["cols", "rows", "start_offset"];
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
    const { would_hit: hit, quota_cost: cost } = await response.json();
    quotaEl.textContent = hit
      ? "cached — saving costs 0 quota units"
      : "not cached — saving spends 100 of 10,000 daily quota units";
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
