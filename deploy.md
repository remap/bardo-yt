# Deploy checklist

Commands and dashboard actions only. The reasoning, the verification steps and
the troubleshooting table are in [`docs/DEPLOY.md`](docs/DEPLOY.md).

**🖥 = browser, not CLI.** Four steps need the dashboard; nothing else does.

---

## 0. Before you start

- Docker Desktop **running** (`wrangler deploy` builds the image).
- Workers **Paid** plan.
- 🖥 **R2 enabled once on the account** — dashboard → **R2** → accept terms.
  Without this, step 2 fails.
- `jburke.io` on Cloudflare. Different domain → edit `routes` in
  `wrangler.jsonc`.

```bash
cd .claude/worktrees/feat-cloudflare-deploy   # until the PR is merged
npm install
npx wrangler login
npx wrangler whoami                            # confirm the right account
```

## 1. 🖥 Create the Access application *first*

Zero Trust → **Access** → **Applications** → **Add an application** →
**Self-hosted**.

| Field | Value |
|---|---|
| Application name | `yt matrix` |
| Public hostname | `yt.bardo.jburke.io`, path **empty** |
| Session duration | 24 hours |

Add a policy: **Allow**, Include → **Emails** → your 5–10 addresses.

Then capture two values:

- **AUD tag** — on the application's overview page.
- **Team domain** — Zero Trust → **Settings** → looks like
  `https://yourteam.cloudflareaccess.com`. **Scheme, no trailing slash.**

> Cloudflare may refuse an application for a hostname with no DNS record yet.
> If it does, skip to step 2 and come back after step 5 — but do it
> **immediately**, because until this exists `https://yt.bardo.jburke.io/`
> serves the wall's HTML and JS to anyone. Only `/api/*`, `/ws` and `/healthz`
> reach the Worker; nothing else is behind its 401.

## 2. R2 bucket

```bash
npx wrangler r2 bucket create yt-matrix
```

## 3. 🖥 R2 API token → S3 credentials

Dashboard → **R2** → **API** → **Manage API Tokens** → **Create API Token**.

- Permission: **Object Read & Write**
- Scope: the `yt-matrix` bucket

Capture, the secret is shown once:

- **Access Key ID**
- **Secret Access Key**
- **Account ID** (R2 overview page, or your dashboard URL)

## 4. Secrets

Five, all prompted. Set `GEMINI_API_KEY` to a placeholder if you have no key —
New query then fails 502 instead of a clean 503, but nothing else breaks.

```bash
npx wrangler secret put YOUTUBE_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put R2_ACCOUNT_ID
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
```

## 5. Wire in the Access values

Edit `wrangler.jsonc` → `vars`, replacing both `REPLACE-ME`s with what you
captured in step 1:

```jsonc
"ACCESS_TEAM_DOMAIN": "https://yourteam.cloudflareaccess.com",
"ACCESS_POLICY_AUD":  "the-aud-tag"
```

Leave `R2_BUCKET` and `YTMATRIX_GLOBAL_DAILY_UNITS` alone.

## 6. Deploy

```bash
npm run deploy        # builds dist/, builds the image, creates the DNS record
```

Certificate issuance takes a minute or two.

## 7. Verify

```bash
curl https://yt.bardo.jburke.io/healthz          # {"status":"ok"} — Worker only
curl -sI https://yt.bardo.jburke.io/ | head -1   # must NOT be 200; Access should
                                                 # redirect or 401/403
curl -sI https://yt.bardo.jburke.io/api/config | head -1   # likewise
```

A `200` on either of the last two means Access is not in front. Fix that before
anything else.

Then in a browser, two profiles signed in as two different users:

1. Open the wall in both — each should show the shared config query.
2. Press **New query** in one. **The other must not change.**
3. Reload the first. Same query, and `units_spent_today` unmoved.
4. Change `grid.cols` on the config page. Both re-lay-out, each keeping its own
   query. **If they re-lay-out but never react, the WebSocket is not getting
   through** — that is the one path a real deploy tests first.

## Afterwards

- **Adding/removing users:** Zero Trust → Access → Applications → *yt matrix* →
  Policies. **No deploy.**
- **Cost** scales with how long *somebody* has a tab open, not with user count —
  one shared container, and playback never touches the server.
- **Rotate `YOUTUBE_API_KEY`** if the one in `.env` was ever pasted anywhere.
