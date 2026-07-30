import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * WHERE THE BROWSER SENDS ITS REQUESTS (`lib/api.ts`'s `resolveApiBase`).
 *
 * The desktop (Tauri) shell picks two FREE ports at launch, so it — and only
 * it — knows where the API child is listening. It tells the page by injecting
 * `window.__GT_API_BASE__` through `WebviewWindowBuilder::initialization_script`,
 * which runs before any page script. `page.addInitScript` is the same mechanism
 * with the same timing, so these tests exercise the real thing rather than a
 * faked `window` in a unit test.
 *
 * What is pinned here:
 *   - injected  -> every API call goes to the injected base, and NOTHING goes
 *     to :8791;
 *   - absent    -> the existing localhost -> :8791 derivation is byte-identical
 *     to what the webapp does today;
 *   - malformed -> an empty or non-string injection is ignored and falls
 *     through to the same :8791 derivation, never `undefined/knowledge/...`;
 *   - RENDERED, not just fetched -> `backup-card.tsx`'s export anchor, the one
 *     API URL in the app that lands in HTML rather than in a `fetch`, follows
 *     the same rules as everything else;
 *   - NOT local -> the injected global is ignored outside a loopback origin.
 *
 * The rendered case used to be documented here as an accepted gap, which it was
 * not. An `href` is baked on the SERVER, where two of the three rules cannot
 * run, and React does not repair a mismatched attribute during hydration — so
 * the server's `http://localhost:8791` guess was what the tutor clicked, on the
 * desktop app AND on the deployed site (where it meant every visitor's own
 * machine). `lib/api.ts` now renders `serverBackupExportUrl()` and swaps in the
 * real one from a mount effect; the test below is what keeps it that way.
 *
 * `/el/settings` is the page under test only because it is the cheapest client
 * page to mock (three endpoints, no polling) — and because it is the page that
 * carries the export anchor.
 *
 * Both origins are mocked wholesale, same convention as `backup.spec.ts`:
 * `X-App-Locale` makes every call a non-simple request, so OPTIONS needs an
 * answer or the real call never fires.
 */

/** What the origin-derivation rule produces for a page served from localhost. */
const DERIVED_ORIGIN = "http://localhost:8791";
/** Stands in for "whatever free port the shell happened to win this launch".
 * Nothing listens here — `page.route` answers before the socket is opened. */
const INJECTED_ORIGIN = "http://localhost:38791";

/** The origin the PAGE is served from, which is also the origin the mocked API
 * has to allow. Not a constant: one test loads the app under a non-loopback
 * hostname, and a credentialed request needs an exact-match ACAO (a `*` is
 * rejected outright), so the mocks echo whichever origin is in play. */
const APP_ORIGIN = "http://localhost:3100";

const corsHeaders = (appOrigin: string) => ({
  "Access-Control-Allow-Origin": appOrigin,
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
});

interface Call {
  method: string;
  pathname: string;
}

/** Route ONE origin, recording every non-preflight call that reaches it. */
async function mockOrigin(page: Page, origin: string, appOrigin = APP_ORIGIN): Promise<Call[]> {
  const calls: Call[] = [];
  const CORS_HEADERS = corsHeaders(appOrigin);

  await page.route(`${origin}/**`, async (route: Route) => {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    calls.push({ method, pathname });

    const json = (b: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(b),
      });

    if (pathname === "/settings") {
      return json({ provider: "anthropic", model: "claude-sonnet-5", configured: true, key_hint: "7f2a" });
    }
    if (pathname === "/blueprint/default") {
      return json({ blueprint: { version: 1, sections: [] }, is_override: false });
    }
    return json([]);
  });

  return calls;
}

/** Both origins, always — a test that only mocked the one it expects could not
 * tell "called the right base" apart from "called the wrong base, which happened
 * to be unroutable". */
async function mockBothOrigins(
  page: Page,
  { derivedOrigin = DERIVED_ORIGIN, appOrigin = APP_ORIGIN } = {},
): Promise<{ derived: Call[]; injected: Call[] }> {
  const injected = await mockOrigin(page, INJECTED_ORIGIN, appOrigin);
  const derived = await mockOrigin(page, derivedOrigin, appOrigin);
  return { derived, injected };
}

/** Exactly what the Tauri shell does, at exactly the same moment. */
async function injectApiBase(page: Page, value: unknown) {
  await page.addInitScript((v) => {
    (window as unknown as Record<string, unknown>).__GT_API_BASE__ = v;
  }, value);
}

// ---------------------------------------------------------------------------

test("the injected global wins: every call goes to the shell's port, none to :8791", async ({ page }) => {
  const { derived, injected } = await mockBothOrigins(page);
  await injectApiBase(page, INJECTED_ORIGIN);

  await page.goto("/el/settings");
  await expect(page.getByTestId("backup-card")).toBeVisible();

  await expect.poll(() => injected.some((c) => c.method === "GET" && c.pathname === "/settings")).toBe(true);
  // and the port the shell did NOT win never hears from us at all
  expect(derived).toEqual([]);
});

test("no injection: the existing localhost -> :8791 derivation is untouched", async ({ page }) => {
  const { derived, injected } = await mockBothOrigins(page);

  await page.goto("/el/settings");
  await expect(page.getByTestId("backup-card")).toBeVisible();

  await expect.poll(() => derived.some((c) => c.method === "GET" && c.pathname === "/settings")).toBe(true);
  expect(injected).toEqual([]);
});

test("the export ANCHOR points at the shell's port too — a rendered URL is a client fact", async ({
  page,
}) => {
  await mockBothOrigins(page);
  await injectApiBase(page, INJECTED_ORIGIN);

  // React shouts about an unpatched attribute mismatch, which is exactly the
  // failure this test exists for; catching it turns "the tutor's Export button
  // silently kept the server's guess" into a named assertion.
  const hydrationErrors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /didn't match|hydration-mismatch/.test(m.text())) {
      hydrationErrors.push(m.text().slice(0, 200));
    }
  });

  await page.goto("/el/settings");
  const exportLink = page.getByTestId("backup-export");
  await expect(exportLink).toBeVisible();

  // The anchor the SERVER rendered said :8791, because on the server there is
  // no injected global to read. After mount it must say the shell's port.
  await expect(exportLink).toHaveAttribute("href", `${INJECTED_ORIGIN}/backup/export`);
  // still the browser's own download path, not a fetch — a backup is hundreds
  // of MB, and on desktop this is what the shell's `on_download` catches
  expect(await exportLink.evaluate((el) => el.tagName)).toBe("A");
  // and it was a real link the whole time, never an href-less dead element
  expect(hydrationErrors).toEqual([]);
});

test("the SERVER still renders a real href — the anchor is never a dead link pre-hydration", async ({
  request,
}) => {
  // The fix must not become "render no href until an effect runs": that leaves
  // a styled element that is not a link — unfocusable, unclickable — for the
  // whole pre-hydration window. The server renders the one base it can honestly
  // know, and the client corrects it. This is also what keeps hydration quiet.
  const html = await (await request.get("/el/settings")).text();
  expect(html).toContain(`href="${DERIVED_ORIGIN}/backup/export"`);
});

test("on a NON-loopback origin the injected global is ignored — it is a desktop-only rule", async ({
  page,
}) => {
  // Chromium resolves any `*.localhost` name to loopback itself (RFC 6761), so
  // this serves the very same dev server under a hostname that is NOT
  // `localhost`/`127.0.0.1` — the stand-in for guitar.cgrigoriadis.online, with
  // no DNS and no hosts file. Rule 3 then derives `guitar-api.localhost`.
  const APP = "http://guitar.localhost:3100";
  const { derived, injected } = await mockBothOrigins(page, {
    derivedOrigin: "http://guitar-api.localhost",
    appOrigin: APP,
  });
  // The whole threat model in one line: the same bundle ships to the public
  // site, so a script that got a moment of execution there could aim every
  // credentialed request — the login POST included — at a host it chose.
  await injectApiBase(page, INJECTED_ORIGIN);

  await page.goto(`${APP}/el/settings`);
  await expect(page.getByTestId("backup-card")).toBeVisible();

  await expect.poll(() => derived.some((c) => c.method === "GET" && c.pathname === "/settings")).toBe(true);
  expect(injected).toEqual([]);
  // the rendered URL obeys the gate as well, not just the fetches
  await expect(page.getByTestId("backup-export")).toHaveAttribute(
    "href",
    "http://guitar-api.localhost/backup/export",
  );
});

for (const [label, value] of [
  ["an empty string", ""],
  ["whitespace only", "   "],
  ["a non-string", 38791],
] as const) {
  test(`a malformed injection (${label}) is ignored and falls back to :8791`, async ({ page }) => {
    const { derived, injected } = await mockBothOrigins(page);
    await injectApiBase(page, value);

    await page.goto("/el/settings");
    await expect(page.getByTestId("backup-card")).toBeVisible();

    // today's behaviour exactly — no `undefined/settings`, no :38791
    await expect.poll(() => derived.some((c) => c.method === "GET" && c.pathname === "/settings")).toBe(true);
    expect(injected).toEqual([]);
  });
}
