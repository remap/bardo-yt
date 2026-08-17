import { Container } from "@cloudflare/containers";
import { env } from "cloudflare:workers";
import { createRemoteJWKSet, jwtVerify } from "jose";

/**
 * One shared container for the whole installation.
 *
 * It can be shared because the server holds no per-user state: config is
 * global and each browser remembers its own query in localStorage. That makes
 * this a single mostly-idle process rather than one per user -- playback never
 * touches it, so ten walls cost about what one does.
 *
 * If per-user server state ever comes back, this is the line that changes:
 * `getByName(email)` instead of a constant gives every user their own
 * instance. That is a product decision, not a refactor.
 */
export class Wall extends Container {
  defaultPort = 8080;
  // Long enough that a wall left playing does not cold-start every time the
  // operator reaches for the config page. Playback itself never touches this
  // process -- video streams from YouTube straight to the browser -- so a
  // sleeping instance does not interrupt a running wall.
  sleepAfter = "20m";
  envVars = {
    YOUTUBE_API_KEY: env.YOUTUBE_API_KEY,
    GEMINI_API_KEY: env.GEMINI_API_KEY,
    R2_ACCOUNT_ID: env.R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID: env.R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY: env.R2_SECRET_ACCESS_KEY,
    R2_BUCKET: env.R2_BUCKET,
    YTMATRIX_GLOBAL_DAILY_UNITS: env.YTMATRIX_GLOBAL_DAILY_UNITS,
  };
}

// createRemoteJWKSet caches and refreshes the key set itself; building a new
// one per request would refetch Cloudflare's certs on every call. Keyed by
// domain rather than held in a single slot, so the cache cannot outlive a
// change to ACCESS_TEAM_DOMAIN and go on verifying against the old team's
// certs. One deployment only ever has one domain -- this is about the
// function telling the truth about its own argument.
const jwksByDomain = new Map<string, ReturnType<typeof createRemoteJWKSet>>();
function keySet(teamDomain: string) {
  let set = jwksByDomain.get(teamDomain);
  if (!set) {
    set = createRemoteJWKSet(new URL(`${teamDomain}/cdn-cgi/access/certs`));
    jwksByDomain.set(teamDomain, set);
  }
  return set;
}

async function verifiedEmail(
  request: Request,
  env: Env,
): Promise<string | null> {
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, keySet(env.ACCESS_TEAM_DOMAIN), {
      // Access signs with RS256. Naming it means a token cannot argue for a
      // weaker algorithm in its own header -- jose would reject the swap
      // anyway, having resolved an asymmetric key by kid, but an auth
      // boundary should not depend on a second line of defence.
      algorithms: ["RS256"],
      issuer: env.ACCESS_TEAM_DOMAIN,
      audience: env.ACCESS_POLICY_AUD,
    });
    // Type-checked, not coerced. `String(payload.email)` would turn a
    // non-string claim into a perfectly good-looking identity; a service
    // token carries `common_name` and no email at all, and must fail here.
    if (typeof payload.email !== "string") return null;
    const email = payload.email.trim().toLowerCase();
    return email || null;
  } catch {
    return null;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Unauthenticated on purpose: this is the Worker's own liveness, and it
    // must answer before Access is configured or the first deploy cannot be
    // checked at all.
    //
    // It shadows the container's identically-named endpoint and never reaches
    // it, so a green /healthz proves the Worker is up and says NOTHING about
    // the container. To check that, hit an authenticated /api/config.
    if (url.pathname === "/healthz") {
      return Response.json({ status: "ok" });
    }

    const email = await verifiedEmail(request, env);
    if (!email) {
      return new Response("Unauthorized", { status: 401 });
    }

    // An upgrade is forwarded exactly as it arrived -- no reconstruction, no
    // injected identity. The socket is a server->client broadcast channel and
    // carries no identity at all: `websocket_endpoint` never reads
    // `X-Wall-User`, and the only place the container uses the header is the
    // query log, which is written on HTTP requests. So the one path whose
    // request reconstruction nothing has ever executed is also the one path
    // that has no use for it. Handing the stub the original Request is what
    // every Cloudflare WebSocket example does, and it is the shape
    // `@cloudflare/containers` expects: its `containerFetch` checks
    // `res.webSocket`, builds a WebSocketPair and pumps both directions.
    //
    // Authentication still happens first, above -- an upgrade is not exempt.
    //
    // The PATH test is load-bearing and must not be dropped as redundant. The
    // header rebuild below is the only thing that strips a client-supplied
    // `x-wall-user`, so any request that skips it can name itself in the query
    // log. On the header alone, `GET /api/videos` with `Upgrade: websocket`
    // and no `Connection: upgrade` opts out: uvicorn treats that as ordinary
    // HTTP -- its `_get_upgrade` returns None unless `upgrade` is among the
    // Connection tokens -- so the request would reach `querylog.append` with
    // an identity nobody verified. Only `/ws` may skip sanitisation, and a
    // forged header is harmless there because `websocket_endpoint` never reads
    // it, which is the premise this whole branch rests on.
    if (url.pathname === "/ws" && request.headers.get("upgrade")?.toLowerCase() === "websocket") {
      return env.WALL.getByName("wall").fetch(request);
    }

    // Overwrite rather than merge. The container writes this straight into the
    // query log, so it must come from the verified token on this line and
    // never from anything the client sent.
    const headers = new Headers(request.headers);
    headers.delete("x-wall-user");
    headers.set("X-Wall-User", email);

    // A constant name, so every user lands on the same instance.
    //
    // Rebuilt rather than mutated: inbound headers are immutable in Workers,
    // so `new Request(request, { headers })` is the only way to add one to a
    // forwarded request. /ws never reaches this line (see the branch above).
    //
    // Either way the stub's Response is returned UNWRAPPED, and must stay
    // that way: a 101 carries its half of the socket on `response.webSocket`,
    // which `new Response(res.body, res)` silently drops.
    return env.WALL.getByName("wall").fetch(new Request(request, { headers }));
  },
} satisfies ExportedHandler<Env>;
