# Deploying to yt.bardo.jburke.io

This is a runbook. Follow it top to bottom with a terminal and the Cloudflare
dashboard open. It assumes nothing about the app beyond what is in this repo.

## What you are deploying

```
browser ──TLS──> Cloudflare edge
                   │  Cloudflare Access checks who you are, at the edge,
                   │  before any of our code runs
                   ▼
                 the edge splits by path, on run_worker_first:
                   │
                   ├── everything else ──> assets layer serves dist/
                   │                       (the HTML and the browser JS —
                   │                        the Worker never runs for these)
                   │
                   └── /api/*, /ws, /healthz ──> Worker (worker/index.ts)
                         ├── verifies the Access JWT itself, a second time
                         ├── stamps X-Wall-User from the verified token
                         └── forwards to ...
                               │
                               ▼
                             one shared container (Dockerfile → ytmatrix.container)
                               └── all persistent state in an R2 bucket
```

The split matters more than it looks. `run_worker_first` in `wrangler.jsonc` is
an allowlist of exactly three routes; `worker/index.ts` never touches the assets
binding and never executes for anything else. So the pages themselves are
protected by Access alone, and the Worker's own 401 is not a second line of
defence for them (see step 3).

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

## Read this before you start: what has and has not been run

The whole stack has now been exercised locally, end to end, under `wrangler
dev` against a real container and an S3-compatible store. "Run locally" is
still not "run on Cloudflare", so this section says which is which.

### Verified by running it

- **The container image builds and serves.** `amd64`, Pillow's JPEG codec
  present (so `motion.py` and `letterbox.py` work), boto3 resolving, no
  `apt-get` packages needed. A missing R2 credential aborts startup with
  `RuntimeError: R2_ACCOUNT_ID is not set`.
- **A real YouTube search, through the container, through the store.** 100
  units spent, 8 videos into a 4x2 grid with 42 reserves, 32 motion scores for
  50 results (`grid + scan_depth`, per gotcha 16), 50 origin lookups, the
  ledger written by compare-and-swap, and titles unescaped correctly. A second
  request came back `from_cache: true` with the ledger unmoved -- the
  never-spend guarantee, observed rather than argued.
- **The WebSocket upgrade through the Worker.** This was the highest-risk
  unverified path in the project. An unauthenticated upgrade is refused with
  401; an authenticated one reaches the container; a config save broadcasts
  `{"type": "config"}` and **no** videos frame follows.
- **JWT verification, with a real RS256 token.** Issuer, audience, algorithm,
  `kid` resolution and the email-claim type check all ran against a token the
  Worker genuinely had to validate, minted by a local stand-in issuer. Nothing
  in the Worker was modified or bypassed to make this work.
- **The identity boundary.** A request carrying a forged `X-Wall-User`
  alongside a valid token was logged under the token's email, not the forged
  one.
- **The public-bundle exposure.** With no Access application in front, `/`,
  `/config` and `/static/player.js` all returned 200 to an unauthenticated
  request while `/api/config` returned 401. This is not a theory; see step 3.

### Still unverified, and only your deploy can settle it

1. **`wrangler deploy` itself.** The image was built by `docker build` and by
   `wrangler dev`, never pushed to Cloudflare's registry.
2. **Real Cloudflare Access.** The token above came from a local issuer
   matching Access's documented shape. Nothing has met a real Access
   application, a real team domain, or a real AUD tag.
3. **Real R2.** The store ran against MinIO over the same S3 API, including
   the conditional write the quota ledger depends on, but not against R2.
4. **The custom domain and its certificate.**
5. **The optional `/healthz` bypass application.** The longest-path-match
   claim in step 4 comes from Cloudflare's documentation and has never been
   built.

### Running the whole stack yourself

Docker and Node are all you need -- no Cloudflare account:

```bash
# 1. An S3-compatible store standing in for R2.
docker run -d --name ytm-minio -p 19000:9000 \
  -e MINIO_ROOT_USER=ytmtest -e MINIO_ROOT_PASSWORD=ytmtestsecret \
  minio/minio:latest server /data

# 2. Create the bucket (any S3 client; boto3 from this repo's venv works).

# 3. Point the container at it. R2_ENDPOINT_URL is the only knob that makes
#    this possible -- unset, as in production, it means real R2.
#    Add to .env:  R2_ACCOUNT_ID=local
#                  R2_ACCESS_KEY_ID=ytmtest
#                  R2_SECRET_ACCESS_KEY=ytmtestsecret
#                  R2_ENDPOINT_URL=http://host.docker.internal:19000

npm run build && npx wrangler dev --port 18787
```

`/`, `/config` and `/healthz` answer immediately. `/api/*` and `/ws` return 401
until you present a token, which is the point -- to exercise those you need an
Access token or a local stand-in issuer serving a JWKS at
`<team-domain>/cdn-cgi/access/certs`, with `--var ACCESS_TEAM_DOMAIN:...
--var ACCESS_POLICY_AUD:...` pointed at it.

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
- **R2 enabled on the account.** It needs a one-time opt-in with a payment
  method — dashboard → **R2** → accept the terms. On an account that has never
  onboarded R2, step 1's `wrangler r2 bucket create` simply fails.
- The Python toolchain is *not* needed to deploy. It builds inside the image.

### The image is amd64, whatever machine you build on

Cloudflare Containers run `linux/amd64` only, so the `Dockerfile` pins
`FROM --platform=linux/amd64` rather than inheriting the build host's
architecture. Nothing to do — a plain `docker build .` produces the right image
on an Apple Silicon Mac — but do not remove the pin.

Without it, a build on an ARM machine fails in a way that points nowhere near
the cause: the arm64 wheel for `google-genai` (pulled in by `ytmatrix/gemini.py`)
dies on import with **SIGILL**, so the container exits 132 with no traceback and
no message. The process just vanishes during startup. Everything else in the
image imports cleanly, which makes it look like a problem with this app.

Verified on an Apple Silicon Mac: the pinned build produces `amd64`, starts, and
answers `/healthz` with `{"status":"ok"}`.

### From a fresh clone

```bash
npm install
npx wrangler login          # then confirm you are on the right account:
npx wrangler whoami
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
- `GEMINI_API_KEY` is only needed for the **New query** button. **Set all five
  anyway.** If you have no Gemini key, put a placeholder such as `unset` in it
  rather than skipping the command: `worker/index.ts` hands all five to the
  container unconditionally, and what the platform does with a secret that was
  never set — drop it, or refuse the start config — has never been observed
  here. A placeholder removes the question.

  The cost of the placeholder is a worse error message on the one button that
  needs it: New query fails with a 502 from Gemini instead of the clean 503
  ("GEMINI_API_KEY is not set") you would get from a genuinely absent key.
  Nothing else in the app touches it.
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

Every API path returns `401 Unauthorized` right now. That is correct — the
Worker is rejecting requests that carry no Access token, and Access is not
configured yet.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://yt.bardo.jburke.io/api/config
# 401
```

**The static files are a different matter, and this is the one thing in this
runbook worth hurrying over.** `run_worker_first` in `wrangler.jsonc` lists
`/api/*`, `/ws` and `/healthz` — only those paths invoke the Worker. Everything
else is served straight from `dist/`, which means that between this step and the
next, `https://yt.bardo.jburke.io/` serves the wall's HTML and JavaScript to
anyone who asks:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://yt.bardo.jburke.io/
# 200 -- and it will stay 200 for anyone on the internet until step 4
```

That leaks no data and no credentials — the page is inert without the API, which
is still 401 — but it is public. Access is what protects those files, and it
only starts protecting them once the application in step 4 exists. Do step 4
now, not tomorrow.

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

- This application's **Application Audience (AUD) Tag** — on the application's
  overview, a long hex string. If you end up with more than one application (see
  below), this is the one the Worker validates against: the **yt matrix**
  application covering the whole hostname, never any other.
- Your **team domain**, under **Settings → Custom Pages** or the Zero Trust
  overview. It looks like `https://yourteam.cloudflareaccess.com`.

### Optional: keeping `/healthz` public

Once the application covers the whole hostname, `/healthz` is behind Access too
and is no longer publicly reachable. That is fine — it existed to check the
first deploy.

If you want it reachable for uptime monitoring, note that **an Access policy has
no path**. Policies select *who* (emails, IP ranges, everyone); the path belongs
to the **application**. So this takes a *second* self-hosted application:

- **Public hostname:** `yt.bardo.jburke.io`, **path** `healthz`
- One policy, **Action: Bypass**, **Include → Everyone**

Access is documented to resolve by longest-path match, so the more specific
application should win for that one route while the main application still
covers everything else. That is read from Cloudflare's documentation and has
never been built here — if `/healthz` still prompts for a login after you add
the bypass application, the ordering is not doing what this paragraph says and
the answer is Cloudflare's, not this repository's. Leave the
`ACCESS_POLICY_AUD` var pointing at the **main** application's AUD tag — the
bypass application has its own, and it is not the one the Worker checks. (This
route is unauthenticated in the Worker anyway, so no token ever reaches the
verification path from it.)

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

Leave the other two vars alone unless you have a reason.
`YTMATRIX_GLOBAL_DAILY_UNITS` is Google's project-wide ceiling — 10,000 units a
day, a hundred searches — and it lives here rather than in `config.yaml`
precisely because `config.yaml` is shared and editable by every user, so this is
the one limit none of them can raise. `R2_BUCKET` must match the bucket you
created in step 1.

Neither Access value is a secret — they are identifiers, not credentials, which
is why they live in `wrangler.jsonc` as `vars` rather than going through
`wrangler secret put`. Commit them; that is what makes the next deploy
reproducible.

```bash
npm run deploy
```

Every later change is the same command. `npx wrangler deployments list` shows
the last ten, and `npx wrangler rollback [version-id]` goes back to one of them
— useful for the Worker, though note that a rollback restores the Worker's code
and configuration, not the R2 bucket's contents.

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

### 1. Access is actually in front — of both halves

Check a Worker path and a static path, because they are protected by different
things. Use a terminal with no Access session, not your signed-in browser:

```bash
for p in /api/config / ; do
  curl -s -o /dev/null -w "$p -> %{http_code} %{redirect_url}\n" \
    "https://yt.bardo.jburke.io$p"
done
```

**Pass:** neither line is a `200`. A `302` whose redirect URL points at
`https://yourteam.cloudflareaccess.com/…` is the usual answer — Access
intercepting at the edge, before any of our code — but Access does not always
redirect a non-browser client. A `401` or `403` served with Access's own headers
(re-run with `-D -` if you want to see them) is the same thing said differently,
and is equally a pass. **The failure is `200`**, in either line.

**Fail — `/` returns `200`:** the page is public. The Access application is not
covering the whole hostname; check that its path field is empty. This is the
failure that matters, because `/` never reaches the Worker and has no second
line of defence.

**Fail — `/api/config` returns `200`:** stop immediately. Your configuration is
readable by anyone.

**`/api/config` returns `401` while `/` redirects:** Access is not matching the
API path but the Worker is rejecting the request on its own. Not a leak, but the
application is misconfigured; fix it rather than relying on the Worker.

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

If it fails, the suspect is the upgrade branch in `worker/index.ts` — the
`return env.WALL.getByName("wall").fetch(request)` that runs before the header
rebuild. The request goes to the stub exactly as it arrived and its response
comes back unwrapped, which is the shape every Cloudflare WebSocket example
uses, so a failure here is about the Worker→container hop itself rather than
about anything this code does to the request. The fallback that keeps the wall
usable meanwhile is polling; see "Levers" below.

### 5. Two users: config is shared, walls are not

This is the check that proves the central design decision, so do it carefully.
You need **two browser profiles** — two windows of the same profile share
`localStorage` and will not show you anything.

Two different people is the realistic test. If you are alone, two profiles
signed in as *the same* email still tell you everything about walls and config,
because separation is per browser profile, not per account (that is also why one
person's laptop and TV are two separate walls). You just are not exercising
Access with two identities.

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

   `grid.cols` is the right field to test with, because it is one of the
   changes that does *not* touch the search.

   **Any edit that changes the search unifies the walls, and that is expected.**
   Not only the query field: `order`, `video_duration`, `safe_search` and
   `relevance_language` do it too. A browser's stored query is served only from
   the shared cache *under the current search parameters*, so once those
   change, nobody's stored query can be served without spending 100 units per
   wall — every wall falls back to the shared config query and forgets its own.
   Pressing **New query** afterwards gives each browser its own wall back.

   The cost of that is one search for the installation, not one per wall: the
   server collapses concurrent identical resolutions into a single call. Check
   it if you like — the `… units today` figure should climb by 100 once,
   however many walls are open.

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
| `/` loads with no login prompt | Same cause, worse consequence: static assets never reach the Worker, so Access is the only thing protecting them. Verification check 1. |
| `/healthz` green, `/api/*` 5xx | The container is not starting. Missing `R2_*` secret is the usual reason. `npx wrangler tail`. |
| Wall renders, never reacts to a config change | The WebSocket upgrade. Verification check 4. |
| `/config` 404s | `html_handling` in `wrangler.jsonc`, or `dist/` was not rebuilt. `npm run build`. |
| New query returns 503 | `GEMINI_API_KEY` is not set. |
| Wall shows "quota spent — showing cached results" | Google itself said no: a 403 `quotaExceeded` from the Data API. The project's real 10,000 units are gone, whatever this app's ledger thinks. Resets on the Pacific date change. |
| Wall shows "daily budget spent — showing cached results" | Our own ledger stopped us first, before any call: `quota.daily_limit_units` in the config (default 5000) or `YTMATRIX_GLOBAL_DAILY_UNITS`. Raise the config value, or set it to `0` to disable that ceiling — `YTMATRIX_GLOBAL_DAILY_UNITS` still applies underneath, and is meant to. |
| Everyone's wall is suddenly the same | Someone saved a config change that affects the search — the query field, or any of `order`, `video_duration`, `safe_search`, `relevance_language`. No stored query is servable under the new parameters, so every wall falls back to the shared query. Press **New query** to get a personal wall back. |

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
