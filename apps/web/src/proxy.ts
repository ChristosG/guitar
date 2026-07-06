import createMiddleware from "next-intl/middleware";
import type { NextRequest } from "next/server";
import { routing } from "./i18n/routing";

// Next.js 16 renamed `middleware.ts`/`middleware()` to `proxy.ts`/`proxy()`
// (the old filename still works but is deprecated — see Next.js 16 upgrade
// guide). This app targets 16.2.x, so we use the current convention.
const intl = createMiddleware(routing);

export default function proxy(request: NextRequest) {
  const res = intl(request);
  // App-Router Vary:rsc trap (spec §5.4): HTML must never be cached.
  res.headers.set("Cache-Control", "no-store");
  return res;
}

export const config = { matcher: ["/((?!api|_next|.*\\..*).*)"] }; // excludes /_next → assets stay immutable
