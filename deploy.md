# Deploy checklist

Commands and dashboard actions only. The reasoning, the verification steps and
the troubleshooting table are in [`docs/DEPLOY.md`](docs/DEPLOY.md).

**🖥 = browser, not CLI.** Five steps need the dashboard; nothing else does.

---

> ## ⚠️ Run everything from the worktree
>
> ```bash
> cd /Users/jburke/Dropbox/eutamias-dev/bardo/yt/.claude/worktrees/feat-cloudflare-deploy
> ```
>
> `Dockerfile`, `wrangler.jsonc` and `package.json` live on the branch, not on
> `main`. From the main checkout every command below fails in a way that reads
> like a broken setup rather than a wrong directory:
>
> | Command | What you get from the wrong directory |
> |---|---|
> | `docker build .` | `failed to read dockerfile: no such file or directory` |
> | `npx wrangler types` | `No config file detected` |
> | `npm run deploy` | `Could not read package.json` |
>
> Merging the PR removes this footgun entirely.

---

## 0. Before you start

```bash
npm install
npx wrangler login
npx wrangler whoami          # confirm the right account

# Check the plan BEFORE deploying. On the free plan this prints the real
# reason; `npm run deploy` only says "Unauthorized", and not until after it
# has uploaded the assets, the Worker and built the image.
npx wrangler containers list
```

- 🖥 **Workers Paid plan** — Containers and Durable Objects are not on the free
  tier. If `containers list` says *"You do not have access to Cloudflare
  Containers"*, upgrade at
  <https://dash.cloudflare.com/?to=/:account/workers/plans> (~$5/mo).
- 🖥 **R2 enabled once on the account** — dashboard → **R2** → accept terms.
  Otherwise step 2 fails.
- **Docker Desktop running** — `wrangler deploy` builds the image.
- `jburke.io` on Cloudflare. Different domain → edit `routes` in
  `wrangler.jsonc`.

## 1. 🖥 Create the Access application *first*

Zero Trust → **Access** → **Applications** → **Add an application** →
**Self-hosted**.

| Field | Value |
|---|---|
| Application name | `yt matrix` |
| Public hostname | `yt.bardo.jburke.io`, path **empty** |
| Session duration | 24 hours |

Add a policy: **Allow**, Include → **Emails** → your 5–10 addresses.

Capture two values:

- **AUD tag** — the application's overview page.
- **Team domain** — Zero Trust → **Settings**, like
  `https://yourteam.cloudflareaccess.com`. **Scheme, no trailing slash.**

> Cloudflare may refuse an application for a hostname with no DNS record yet.
> If so, come back to this straight after step 3 — but do it **immediately**,
> because until it exists `https://yt.bardo.jburke.io/` serves the wall's HTML
> and JS to anyone. Only `/api/*`, `/ws` and `/healthz` reach the Worker;
> nothing else is behind its 401.

## 2. R2 bucket

```bash
npx wrangler r2 bucket create yt-matrix
```

## 3. 🖥 R2 API token → S3 credentials

Dashboard → **R2** → **API** → **Manage API Tokens** → **Create API Token**.

- Permission: **Object Read & Write**
- Scope: the `yt-matrix` bucket

Capture — the secret is shown once:

- **Access Key ID**
- **Secret Access Key**
- **Account ID** (R2 overview page, or your dashboard URL)

## 4. First deploy

This creates the Worker, so the secrets in step 5 have something to attach to.

```bash
npm run deploy
```

The container will crash on its first request until step 5 — that is expected.
It aborts startup with `RuntimeError: R2_ACCOUNT_ID is not set` rather than
limping along on missing credentials.

## 5. Secrets

Five, each prompted. Use a placeholder for `GEMINI_API_KEY` if you have no key —
New query then fails 502 instead of a clean 503, and nothing else changes.

```bash
npx wrangler secret put YOUTUBE_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put R2_ACCOUNT_ID
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
```

## 6. Wire in the Access values

Edit `wrangler.jsonc` → `vars`, replacing both `REPLACE-ME`s with step 1's
values:

```jsonc
"ACCESS_TEAM_DOMAIN": "https://yourteam.cloudflareaccess.com",
"ACCESS_POLICY_AUD":  "the-aud-tag"
```

Leave `R2_BUCKET` and `YTMATRIX_GLOBAL_DAILY_UNITS` alone.

## 7. Redeploy

Secrets and `vars` both need a deploy to take effect.

```bash
npm run deploy
```

## 8. Verify

```bash
curl https://yt.bardo.jburke.io/healthz          # {"status":"ok"} — Worker only
curl -sI https://yt.bardo.jburke.io/ | head -1   # must NOT be 200
curl -sI https://yt.bardo.jburke.io/api/config | head -1   # likewise
```

A `200` on either of the last two means Access is not in front. Fix before
anything else.

Then in a browser, two profiles signed in as different users:

1. Open the wall in both — each shows the shared config query.
2. Press **New query** in one. **The other must not change.**
3. Reload the first. Same query, `units_spent_today` unmoved.
4. Change `grid.cols` on the config page. Both re-lay-out, each keeping its own
   query. **If they never react, the WebSocket is not getting through** — the
   one path only a real deploy tests.

## Afterwards

- **Adding/removing users:** Zero Trust → Access → Applications → *yt matrix* →
  Policies. **No deploy.**
- **Cost** scales with how long *somebody* has a tab open, not with user count —
  one shared container, and playback never touches the server.
- **Rotate `YOUTUBE_API_KEY`** if the one in `.env` was ever pasted anywhere.
