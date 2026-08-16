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
// one per request would refetch Cloudflare's certs on every call.
let jwks: ReturnType<typeof createRemoteJWKSet> | undefined;
function keySet(teamDomain: string) {
  jwks ??= createRemoteJWKSet(new URL(`${teamDomain}/cdn-cgi/access/certs`));
  return jwks;
}

async function verifiedEmail(
  request: Request,
  env: Env,
): Promise<string | null> {
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, keySet(env.ACCESS_TEAM_DOMAIN), {
      issuer: env.ACCESS_TEAM_DOMAIN,
      audience: env.ACCESS_POLICY_AUD,
    });
    const email = String(payload.email ?? "")
      .trim()
      .toLowerCase();
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
    if (url.pathname === "/healthz") {
      return Response.json({ status: "ok" });
    }

    const email = await verifiedEmail(request, env);
    if (!email) {
      return new Response("Unauthorized", { status: 401 });
    }

    // Overwrite rather than merge. The container writes this straight into the
    // query log, so it must come from the verified token on this line and
    // never from anything the client sent.
    const headers = new Headers(request.headers);
    headers.delete("x-wall-user");
    headers.set("X-Wall-User", email);

    // A constant name, so every user lands on the same instance. WebSocket
    // upgrades forward through this same call -- the Container class proxies
    // them to the container's port without special handling.
    return env.WALL.getByName("wall").fetch(new Request(request, { headers }));
  },
} satisfies ExportedHandler<Env>;

interface Env {
  WALL: DurableObjectNamespace<Wall>;
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_POLICY_AUD: string;
  YOUTUBE_API_KEY: string;
  GEMINI_API_KEY: string;
  R2_ACCOUNT_ID: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  R2_BUCKET: string;
  YTMATRIX_GLOBAL_DAILY_UNITS: string;
}
