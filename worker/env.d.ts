// The five secrets, declared for the typechecker.
//
// `wrangler types` writes worker-configuration.d.ts from wrangler.jsonc plus
// whatever happens to be in a local, gitignored `.env` -- so the secrets set
// with `wrangler secret put` (Task 10) appear in the generated Env on a
// machine that has a `.env` and vanish on one that does not. That would make
// `npx tsc --noEmit` pass or fail depending on an untracked file.
//
// Declaring them here instead pins the contract: these five are supplied at
// runtime as secrets, never as `vars`, and never checked in. Interface
// merging makes this additive, so it agrees with the generated file rather
// than fighting it.
export {};

declare global {
  namespace Cloudflare {
    interface Env {
      YOUTUBE_API_KEY: string;
      GEMINI_API_KEY: string;
      R2_ACCOUNT_ID: string;
      R2_ACCESS_KEY_ID: string;
      R2_SECRET_ACCESS_KEY: string;
    }
  }
}
