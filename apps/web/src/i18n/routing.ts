import { defineRouting } from "next-intl/routing";

export const routing = defineRouting({
  locales: ["en", "el"],
  defaultLocale: "el",
  // GREEK IS THE PRODUCT, AND THE OPERATING SYSTEM DOES NOT GET A VOTE.
  //
  // `defaultLocale: "el"` was never enough on its own. next-intl resolves in
  // this order (read off the INSTALLED v4 `resolveLocale`, not the docs): path
  // prefix → cookie → `Accept-Language` → default, and the middle two only run
  // while `localeDetection` is on, which it is unless you say otherwise. The
  // desktop shell loads the bare origin, so there is no path prefix, and WebKit
  // derives `Accept-Language` from the system `LANG`. On a machine set to
  // `en_US.UTF-8` — the ordinary case for anyone who installed Ubuntu in
  // English — the app opened in ENGLISH with this line sitting right here
  // saying `el` and never being reached. Nothing looked broken. It just shipped
  // the wrong language to the one user who cannot read it.
  //
  // Turning detection off puts `Accept-Language` out of reach. It also puts the
  // COOKIE out of reach, which would throw away an explicit choice, so the
  // bare-origin case is handled in `proxy.ts` instead — where the cookie is read
  // on purpose and the system locale still is not. The cookie keeps being
  // WRITTEN either way: next-intl's `syncCookie` is gated on `localeCookie`,
  // not on `localeDetection`.
  localeDetection: false,
});
