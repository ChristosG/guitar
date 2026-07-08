import createMiddleware from "next-intl/middleware";
import { NextResponse, type NextRequest } from "next/server";
import { routing } from "./i18n/routing";

// Next.js 16 renamed `middleware.ts`/`middleware()` to `proxy.ts`/`proxy()`
// (the old filename still works but is deprecated — see Next.js 16 upgrade
// guide). This app targets 16.2.x, so we use the current convention.
const intl = createMiddleware(routing);

// The cockpit has no real "home": `/{locale}` redirects to `/{locale}/today`
// (also implemented, per the brief, as a plain `redirect()` in
// `app/[locale]/page.tsx`). Handling it here FIRST is load-bearing, not
// redundant: empirically verified via `curl -I /en` that a Server Component
// `redirect()` does NOT inherit this file's `Cache-Control: no-store` — the
// response that reaches the browser came back `no-cache, must-revalidate`
// instead (Next.js appears to stamp its own Cache-Control on `redirect()`
// responses after middleware headers are merged, overriding this file's
// value for that one header while other middleware-set headers, e.g.
// next-intl's cookie/link headers, do pass through untouched). Since this
// app deploys behind Cloudflare (spec §5.4's whole reason `no-store`
// exists — see below), an edge cache treating `no-cache` more permissively
// than `no-store` is a real production risk, not a theoretical one. A
// redirect issued directly from middleware doesn't go through that
// dynamic-API path, so this file's own `no-store` (set below) is
// guaranteed to be the final word on it. `page.tsx`'s `redirect()` still
// covers any request this matcher doesn't.
function localeRootRedirect(request: NextRequest): NextResponse | null {
  const match = request.nextUrl.pathname.match(/^\/(en|el)\/?$/);
  if (!match) return null;
  const url = request.nextUrl.clone();
  url.pathname = `/${match[1]}/today`;
  return NextResponse.redirect(url);
}

export default function proxy(request: NextRequest) {
  const res = localeRootRedirect(request) ?? intl(request);
  // App-Router Vary:rsc trap (spec §5.4): HTML must never be cached.
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export const config = { matcher: ["/((?!api|_next|.*\\..*).*)"] }; // excludes /_next → assets stay immutable
