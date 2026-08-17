# Deploying to yt.bardo.jburke.io

This is a runbook. Follow it top to bottom with a terminal and the Cloudflare
dashboard open. It assumes nothing about the app beyond what is in this repo.

## What you are deploying

```
browser ──TLS──> Cloudflare edge
                   │  Cloudflare Access checks who you are, at the edge,
                   │  before any of our code runs
                   ▼
                 Worker (worker/index.ts)
                   ├── verifies the Access JWT itself, a second time
                   ├── serves dist/ (the HTML and the browser JS)
                   └── forwards /api/*, /ws, /healthz to ...
                         │
                         ▼
                       one shared container (Dockerfile → ytmatrix.container)
                         └── all persistent state in an R2 bucket
```

Three facts drive every decision below:

- **One container for everybody.** `env.WALL.getByName("wall")` is a constant,
  so all users land on the same instance. That is only safe because the server
  holds no per-user state.
- **Config is shared, walls are not.** Everyone edits one `config.yaml` in R2.
  What each person is *watching* — their current query and history — lives in
  their own browser's `localStorage`.
- **The container has no durable disk.** Every start is a fresh copy of the
  image, so the cache, the query log, the quota ledger and `config.yaml` all
  live in R2.

## Read this before you start: what has never been run

Nothing in this plan has ever executed against Cloudflare. The code typechecks,
the Python and browser suites pass, and `wrangler` accepts the configuration —
none of which is the same as having worked. Specifically:

1. **The container image has never been built.** Docker was unavailable during
   development. Pillow and boto3 resolving cleanly on `python:3.13-slim` is
   inferred from `uv.lock`, not observed. If the image build fails, that is the
   first surprise to expect.
2. **The WebSocket upgrade through the Worker is the highest-risk path in the
   whole deployment.** `worker/index.ts` forwards with
   `new Request(request, { headers })` to inject the verified identity, and
   returns the container's response unwrapped so a 101's `response.webSocket`
   survives. Inbound headers are immutable in Workers, so this is the only way
   to add one to a forwarded upgrade — the reasoning is sound and the code
   carries a comment saying so, but **it has never executed**. If the wall
   renders but never reacts to a config change, suspect this before anything
   else. Verification check 4 below is what catches it.
3. **JWT verification has never met a live Access application.** The algorithm,
   issuer and audience checks are all pinned, but no real token has been through
   them.

None of this is a reason not to deploy. It is a reason to run the verification
section honestly rather than skim it.

## Prerequisites

- The `jburke.io` zone on Cloudflare (nameservers already pointed at
  Cloudflare). If your domain is different, change the `routes` pattern in
  `wrangler.jsonc` to match.
- A **Workers Paid** plan. Containers and Durable Objects are not on the free
  tier.
- **Docker running locally.** `wrangler deploy` builds the container image and
  pushes it to Cloudflare's registry, so the Docker daemon must be up — it will
  refuse even a `--dry-run` otherwise (see below). Start Docker Desktop, or
  `open -a Docker`, before you get to step 3.
- Node 20+ and a Cloudflare account you can log in to (`npx wrangler login`).
- The Python toolchain is *not* needed to deploy. It builds inside the image.

### From a fresh clone

```bash
npm install
npx wrangler types          # writes worker-configuration.d.ts, which is gitignored
npm run typecheck
```

`npm run typecheck` and never a bare `npx tsc --noEmit`: `tsconfig.json`
references `worker-configuration.d.ts`, which `wrangler types` generates and
`.gitignore` excludes, so the bare command fails with `TS2688` on a fresh clone
before it typechecks a single line. The npm script generates the file first.

You can validate the whole configuration without Docker:

```bash
npx wrangler deploy --dry-run --containers-rollout=none
```

That prints the bindings table and exits. **`--containers-rollout=none` is what
makes it work with the daemon down** — a plain `--dry-run` tries to build the
image and fails with "The Docker CLI is needed to build the configured image
before deploying (even in dry-run mode)". The flag is for preflight only; never
use it on a real deploy, or the Worker ships pointing at a stale image.

The secrets from step 2 do not appear in that bindings table. That is expected:
they are set on the Worker, not in `wrangler.jsonc`.

## 1. Create the R2 bucket and an S3 API token

```bash
npx wrangler r2 bucket create yt-matrix
```

Then create an R2 API token: **Cloudflare dashboard → R2 → API → Manage API
Tokens → Create API Token**, permission **Object Read & Write**, scoped to the
`yt-matrix` bucket. Save the **Access Key ID** and **Secret Access Key** — the
secret is shown exactly once.

Your **Account ID** is on the R2 overview page and in the dashboard URL.

Two things to get right here:

- **Do not add an `r2_buckets` binding to `wrangler.jsonc`**, and do not let
  `wrangler r2 bucket create --update-config` add one for you. The bucket is
  read and written by the *Python container*, over the S3 API, with the three
  credentials above (`ytmatrix/store.py`). A Worker R2 binding is a Worker-side
  object; the container cannot reach it.
- The bucket name must match the `R2_BUCKET` var in `wrangler.jsonc`, which is
  `yt-matrix`. If you name the bucket something else, change the var too.

The bucket starts empty and fills itself. After a day of use it holds:

| Key | What it is |
|---|---|
| `config.yaml` | the shared config, written by the config page |
| `_budget.json` | the daily quota ledger, the one key with more than one writer |
| `search/…` | content-addressed search results |
| `contentbox/<video_id>.json` | letterbox geometry per video, cached forever |
| `motion/…`, `origin/…` | per-video scores and countries, cached forever |
| `logs/<date>/…` | the query log — one small JSON object per query, named so that listing sorts chronologically |

`config.yaml` is absent until someone saves the config page; until then the
copy baked into the image is the template. Deleting `config.yaml` from the
bucket resets everyone to that template. Deleting `_budget.json` zeroes the
spend counter without giving you any actual quota back (CLAUDE.md gotcha 2).

## 2. Set the secrets

```bash
npx wrangler secret put YOUTUBE_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put R2_ACCOUNT_ID
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
```

Each prompts for the value and stores it encrypted on the Worker. The `Wall`
class forwards all five into the container as environment variables; they never
appear in `wrangler.jsonc` and never reach the browser.

- `YOUTUBE_API_KEY` must be a **plain API key**, not a service account —
  service accounts do not work with the YouTube Data API v3 at all (CLAUDE.md
  gotcha 1).
- `GEMINI_API_KEY` is only needed for the **New query** button. Leave it out and
  everything else works; New query returns 503 with a message saying so.
- The three `R2_*` values are **required**. The container refuses to start
  without them, on purpose — a wall that silently persists nothing is worse
  than one that does not come up.

If `wrangler secret put` complains that no Worker of that name exists, run
`npm run deploy` (step 3) first and then come back and set the secrets, then
deploy again. Between those two points the Worker answers `/healthz` but every
`/api/*` request fails, because the container cannot start without R2
credentials. That is expected, not a fault.

## 3. First deploy

Make sure Docker is running, then:

```bash
npm run deploy
```

That runs `scripts/build-dist.sh` (assembling `dist/` from `static/`), builds
and pushes the container image, and uploads the Worker. Because of the
`custom_domain` route it also creates the `yt.bardo.jburke.io` DNS record and
provisions its certificate. Certificate issuance takes a minute or two; a
handful of TLS errors immediately after the first deploy are that, not you.

Expect the image build to take several minutes the first time.

Verify the Worker is alive — this route is deliberately unauthenticated:

```bash
curl -s https://yt.bardo.jburke.io/healthz
# {"status":"ok"}
```

**`/healthz` says nothing about the container.** The Worker answers it before
routing and never forwards it, shadowing the container's identically-named
endpoint. It proves the Worker deployed. Check 3 in the verification section is
what proves the container is up.

Everything else returns `401 Unauthorized` right now. That is correct — the
Worker is rejecting requests that carry no Access token, and Access is not
configured yet.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://yt.bardo.jburke.io/api/config
# 401
```

## 4. Put Cloudflare Access in front

**Zero Trust dashboard → Access → Applications → Add an application →
Self-hosted.**

- **Application name:** yt matrix
- **Session duration:** 24 hours, or whatever suits — this is how often people
  re-authenticate.
- **Public hostname:** `yt.bardo.jburke.io`, path left empty so the whole site
  is covered.

Add a policy:

- **Policy name:** wall users
- **Action:** Allow
- **Include → Emails** — the 5–10 addresses. **Emails ending in** `@your-domain`
  works too if everyone shares a domain.

Choose login methods under **Settings → Authentication**. One-time PIN needs no
setup and emails a code; Google or GitHub is smoother if everyone already has
one.

Then collect two values:

- The application's **Application Audience (AUD) Tag** — on the application's
  overview, a long hex string.
- Your **team domain**, under **Settings → Custom Pages** or the Zero Trust
  overview. It looks like `https://yourteam.cloudflareaccess.com`.

Note that once the application covers the whole hostname, `/healthz` is behind
Access too and is no longer publicly reachable. That is fine — it existed to
check the first deploy. If you want it reachable for uptime monitoring, add a
second Access policy with action **Bypass** for the path `/healthz`.

## 5. Wire the Access values in and redeploy

Edit `wrangler.jsonc`:

```jsonc
  "vars": {
    "ACCESS_TEAM_DOMAIN": "https://yourteam.cloudflareaccess.com",
    "ACCESS_POLICY_AUD": "the-aud-tag-you-copied",
    "R2_BUCKET": "yt-matrix",
    "YTMATRIX_GLOBAL_DAILY_UNITS": "10000"
  },
```

**`ACCESS_TEAM_DOMAIN` must include the `https://` scheme and must not end in a
slash.** The Worker uses it twice: concatenated into the JWKS URL
(`${teamDomain}/cdn-cgi/access/certs`) and compared against the token's `iss`
claim. A trailing slash leaves the first working and breaks the second, so
every request 401s while the key fetch looks perfectly healthy — a miserable
thing to debug. Omit the scheme and both fail.

```bash
npm run deploy
```

Two more things about this file, for whoever edits it next:

- **`run_worker_first` belongs inside `assets`, not at the top level.** Move it
  out and wrangler prints only `Unexpected fields found in top-level field:
  "run_worker_first"` and deploys anyway — with `/api/*`, `/ws` and `/healthz`
  silently served from static assets, which means 404s from a Worker that looks
  perfectly deployed. (Verified locally: it is a warning, not an error.)
- `html_handling: "auto-trailing-slash"` is doing real routing work. `dist/`
  contains `config.html`, and the pages link to `/config`. Set it to `"none"`
  and that link 404s.

## 6. Verify

Open `https://yt.bardo.jburke.io/` in a fresh browser profile. You should get
the Access login page, then the wall.

Then run these six checks. Each one can fail, and each failure means something
specific. Do not skip 4 and 5 — they are what prove the design.

### 1. Access is actually in front

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://yt.bardo.jburke.io/api/config
```

**Pass:** `302` (Access redirecting to the login page). **Fail:** `200` — the
config of your installation is being served to anyone on the internet. Stop and
check the application's hostname and path in Zero Trust.

A `401` here means Access is not intercepting and the Worker is doing the
rejecting on its own. Safe, but the application is misconfigured.

### 2. The Worker trusts the same Access application

Sign in in the browser. If you land on the wall, JWT verification passed —
`ACCESS_TEAM_DOMAIN` and `ACCESS_POLICY_AUD` agree with the token.

**Fail:** you authenticate successfully and then get a bare `Unauthorized` page.
That is Access saying yes and the Worker saying no, which is exactly the
symptom of a wrong AUD tag or a trailing slash on the team domain. Fix the vars
and redeploy — nothing else produces this combination.

### 3. The container is up

With the browser signed in, open `https://yt.bardo.jburke.io/api/config`.

**Pass:** a JSON document with `grid`, `search`, `playback` and so on.
**Fail:** a 5xx. The Worker is fine and the container is not. In order of
likelihood: a missing or wrong `R2_*` secret (the container raises at startup
and never binds a port), then the image failing to build. `npx wrangler tail`
in another terminal while you reload shows the Worker's side; `npx wrangler
containers list` and `npx wrangler containers instances <id>` show whether an
instance is running at all.

Remember this is the check `/healthz` cannot do.

### 4. The WebSocket survives the Worker

**This is the unverified path. Test it deliberately.**

In one browser profile, open the wall in one tab and `/config` in another.
Change `grid.cols` — from 4 to 3, say — and save.

**Pass:** the wall tab re-lays out to the new grid **within a second, without
being reloaded**.

**Fail:** the wall only changes when you reload it. The config was saved (R2 has
it) but the push never arrived, which means the upgrade did not survive the
Worker's forward. Confirm it in devtools: Network → WS filter → `/ws` should
show status **101 Switching Protocols** and a live message list. A 200, a 426,
or a connection that closes and retries forever is the failure.

If it fails, the suspect is the single `return env.WALL.getByName("wall").fetch(
new Request(request, { headers }))` line in `worker/index.ts` — specifically
whether `new Request(request, …)` preserves the upgrade. The fallback that
keeps the wall usable meanwhile is polling; see "Levers" below.

### 5. Two users: config is shared, walls are not

This is the check that proves the central design decision, so do it carefully.
You need **two browser profiles signed in as two different people** — two
windows of the same profile share `localStorage` and will not show you
anything.

Call them A and B. Both should be showing a wall, each with its own query in
the status line.

1. **Walls are separate.** Press **New query** in A.
   **Pass:** A's videos change; B does not move, now or in thirty seconds, and
   still shows its own query after a reload.
   **Fail:** B's wall changes too. That means per-user state has leaked back
   into the server, or the videos message is being broadcast rather than
   returned to its caller. It is a real defect, not a cosmetic one: with one
   shared container, every user would be dragged along by whoever clicked last.

2. **Config is shared.** In B, change `grid.cols` on the config page and save.
   **Pass:** both walls re-lay out to the new grid, and **each keeps its own
   query** — check the status line in each.
   **Fail (a):** neither changes without a reload → check 4 above, same cause.
   **Fail (b):** A's videos are replaced by B's query → the config broadcast is
   carrying a video set with it. Also a real defect; the server does not know
   what query any browser is watching and must not pretend to.

   One deliberate exception: editing the **query field itself** on the config
   page *does* move everyone's wall. That is a config change to the shared
   query, and it is meant to beat each browser's stored one.

3. **A reload is free.** Note the `… units today` figure in each wall's status
   line, then reload both.
   **Pass:** each comes back to the query it was showing, the status line reads
   **`cached`** rather than `fresh search`, and the units figure has not moved.
   **Fail:** the units figure climbs by 100 on a reload. A reload is spending
   quota, which at 100 units of 10,000 per search is a hundred reloads from an
   outage. Spending is supposed to be the New query button's job and nothing
   else's.

If you want the number without reloading a wall, run this in the devtools
console of a signed-in tab — it reads the ledger and cannot cause a search:

```js
await (await fetch("/api/cache-status", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: "{}",
})).json();
// { would_hit: true, quota_cost: 0, units_spent_today: 200, daily_limit_units: 5000 }
```

Do not poke `/api/videos` by hand to read it. With no `query` parameter it
resolves the *config* query, and if that is not in the cache it will run a real
search and spend 100 units.

### 6. The log knows who did what

In the R2 dashboard, open the newest object under `logs/<today>/`. It is one
JSON record for one query, and it carries a `"user"` field with the email of
whoever ran it — the verified one from the token, set by the Worker, never
anything the browser sent.

**Fail:** there is no `"user"` field at all (it is omitted when the email is
empty, not written blank). The `X-Wall-User` header is not arriving, so identity
is being dropped between the Worker and the container.

## Troubleshooting

| Symptom | Cause to check first |
|---|---|
| `TS2688: Cannot find type definition file for './worker-configuration.d.ts'` | You ran `npx tsc --noEmit`. Run `npm run typecheck`, or `npx wrangler types` first. |
| `The Docker CLI is needed…` on `--dry-run` | Expected with the daemon down. Add `--containers-rollout=none` for a config-only check; start Docker for a real deploy. |
| `Action required — Install @types/node` from `wrangler types` | Advisory, not an error. The Worker uses no Node built-ins; `npm run typecheck` exits 0 with this printed. |
| Authenticated, then a bare `Unauthorized` | `ACCESS_POLICY_AUD` is wrong, or `ACCESS_TEAM_DOMAIN` has a trailing slash or no scheme. |
| Every path 401s and no login page appears | The Access application is not covering this hostname. |
| `/healthz` green, `/api/*` 5xx | The container is not starting. Missing `R2_*` secret is the usual reason. `npx wrangler tail`. |
| Wall renders, never reacts to a config change | The WebSocket upgrade. Verification check 4. |
| `/config` 404s | `html_handling` in `wrangler.jsonc`, or `dist/` was not rebuilt. `npm run build`. |
| New query returns 503 | `GEMINI_API_KEY` is not set. |
| Wall shows "quota spent — showing cached results" | The 10,000-unit daily allowance is gone, or `quota.daily_limit_units` in the config is lower. It resets on the Pacific date change. |
| Everyone's wall is suddenly the same | Someone edited the query field on the config page. That is the one config edit that overrides each browser's own query. |

## Adding and removing users

**Zero Trust → Access → Applications → yt matrix → Policies → edit the email
list.** That is the whole operation. **No deploy, no code change, no restart** —
the policy takes effect at the edge on the next request, and the Worker learns
about it only in the sense that a removed user's token stops verifying.

There is nothing to clean up in R2 either: a user's wall lived only in their own
browser. Their entries stay in the query log under `logs/`, which is what the
log is for.

## Costs and what drives them

Containers bill for the time an instance is **awake**. `sleepAfter` is `20m`,
and there is exactly one instance for everybody.

So **cost does not scale with the number of users** — it scales with how long
*somebody* has a tab open. Ten walls cost about what one does. Playback never
touches the server at all: video streams from YouTube to the browser directly,
and the container only handles config edits, searches and cache reads.

What keeps the instance awake is therefore the open WebSocket, not request
volume. A wall left running all day keeps one basic instance alive all day.

R2 and Worker costs are noise by comparison: a few thousand small objects, and
requests measured in hundreds per day. YouTube quota is the resource that
actually runs out, and it is free.

### Levers, with tradeoffs rather than recommendations

- **Shorten `sleepAfter`** in `worker/index.ts`. A sleeping container does not
  interrupt a playing wall — nothing about playback needs the server — so the
  only visible cost is a few seconds of cold start on the next config edit or
  new query. Lower it to `5m` and an idle-but-open tab stops billing sooner.
- **Drop the socket when the tab is hidden.** Same effect, driven by the client
  instead of the clock. Costs you live remote control on a backgrounded tab.
- **Replace the WebSocket with polling.** This would let the container sleep
  through playback entirely and would cut the dominant cost more than anything
  else here. It also removes live remote control, which is the feature the
  socket exists for: a config change would land on other screens within the poll
  interval instead of immediately. If check 4 above fails and the upgrade turns
  out not to survive the Worker, this is also the fallback that keeps the app
  working. Named as a lever, not recommended.
- **`instance_type`** is `basic` in `wrangler.jsonc`. The workload is I/O-bound
  — HTTP to YouTube, HTTP to R2 — with one burst of Pillow work per query for
  the motion and letterbox scoring. If queries feel slow to resolve, measure
  before upgrading; the likely culprit is sequential R2 round trips, not CPU.

## Local development is unchanged

None of the above touches local work. `./run.sh` still runs the whole app
against a `FileStore` on `https://localhost:8444/` with **no Cloudflare
account, no Access, and no R2** — it builds `dist/`, generates a self-signed
certificate, and serves the same FastAPI app the container runs.

Use `localhost`, never `127.0.0.1`: YouTube refuses to embed into a page served
from an IP address, and every player fails with error 150 in a way that looks
exactly like genuinely non-embeddable videos (CLAUDE.md gotcha 11).

To exercise the Worker and container together locally:

```bash
npm run dev     # requires Docker; wrangler builds and runs the container
```

Access does not sit in front of `wrangler dev`, so requests arrive with no
`cf-access-jwt-assertion` header. The Worker will 401 them — comment out the
auth check for a local session if you need to drive the Worker path, and do not
commit that. The identity a real request carries is only used to stamp the
query log, so its absence changes nothing else.

**`npm run dev` has never been run either** (Docker again). `./run.sh` is the
development path that is actually exercised, and it is the one the test suite
covers.
