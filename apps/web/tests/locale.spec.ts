import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route, type Request } from "@playwright/test";

// The UI locale finally leaves the browser (Plan 13, Stage 5.1). Until now
// `useLocale()` was read by eight components and every one of them used it only
// to build an href — nothing in `lib/api.ts` carried it, so the API never knew
// what language the tutor was looking at and the MODEL picked the output
// language.
//
// What this file pins is the invariant, not one call site: EVERY request the app
// makes to the API carries `X-App-Locale`, and it matches the locale in the URL.
// A per-endpoint assertion would pass forever while the next raw `fetch` added
// to `lib/api.ts` quietly skips the header — which is exactly what happened to
// `credentials: "include"` and to `streamChatMessage`. So the assertion is a
// sweep over `page.on("request")`, and it includes the SSE POST specifically,
// because that call bypasses `request()` — the header chokepoint — entirely.
const API_ORIGIN = "http://localhost:8791";
const LOCALE_HEADER = "x-app-locale"; // `request.headers()` lowercases

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  // `X-App-Locale` is a CUSTOM header: it makes even a plain GET a non-simple
  // request, so the browser preflights and this list is what decides whether the
  // real call is ever sent. The API's own allowlist is asserted in
  // `apps/api/tests/test_locale.py::test_cors_allows_the_locale_header`.
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

function sseBody(events: Array<{ event: string; data: unknown }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
}

/** Everything at the API origin, answered from memory — this test does not care
 * what the endpoints return, only what the browser SENT to reach them, so an
 * unrecognised path is an empty list rather than a failure. */
async function mockApi(page: Page) {
  const sessionId = randomUUID();

  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const json = (body: unknown) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(body),
      });

    if (pathname === "/chat" && method === "POST") return json({ session_id: sessionId });
    if (pathname.endsWith("/messages/stream") && method === "POST") {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: CORS_HEADERS,
        body: sseBody([
          { event: "delta", data: { text: "Το power chord είναι…" } },
          { event: "done", data: { citations: null } },
        ]),
      });
      return;
    }
    if (pathname.match(/^\/chat\/[^/]+\/pending$/)) return json(null);
    // Suggestion chips (chat overhaul, Piece B): fired non-blocking after the
    // streamed answer above renders. `{suggestions: []}`, NOT the catch-all's
    // bare `[]` below — `getChatSuggestions` reads `.suggestions` off the
    // body, and a bare array would hand the panel `undefined` instead of an
    // empty list.
    if (pathname.match(/^\/chat\/[^/]+\/suggestions$/) && method === "POST") return json({ suggestions: [] });
    if (pathname.match(/^\/chat\/[^/]+$/) && method === "GET") return json([]);
    await json([]);
  });

  return { sessionId };
}

/** Records every request the page fires at the API origin. Preflights are not
 * reported by `page.on("request")` and are not the point anyway — what matters
 * is the header on the real call. */
function recordApiRequests(page: Page): Request[] {
  const seen: Request[] = [];
  page.on("request", (req) => {
    if (req.url().startsWith(API_ORIGIN)) seen.push(req);
  });
  return seen;
}

test.describe("X-App-Locale on every API call", () => {
  test("a Greek session sends el on every request, INCLUDING the SSE POST", async ({ page }) => {
    await mockApi(page);
    const seen = recordApiRequests(page);

    await page.goto("/el/chat");
    await expect(page).toHaveURL(/\/el\/chat\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    await page.getByTestId("chat-input").fill("Τι είναι το power chord;");
    await page.getByTestId("chat-send").click();
    // The streamed answer landing is what proves the SSE POST actually went out
    // (and survived the preflight the new custom header forces).
    await expect(page.getByText("Το power chord είναι…")).toBeVisible();

    expect(seen.length).toBeGreaterThan(0);
    const missing = seen
      .filter((r) => r.headers()[LOCALE_HEADER] !== "el")
      .map((r) => `${r.method()} ${new URL(r.url()).pathname} -> ${r.headers()[LOCALE_HEADER] ?? "(absent)"}`);
    expect(missing, "every API request must carry X-App-Locale: el").toEqual([]);

    const stream = seen.find((r) => r.url().endsWith("/messages/stream"));
    expect(stream, "the SSE POST bypasses request() — it must set the header itself").toBeTruthy();
    expect(stream!.headers()[LOCALE_HEADER]).toBe("el");
  });

  test("the English side of the app sends en", async ({ page }) => {
    await mockApi(page);
    const seen = recordApiRequests(page);

    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    expect(seen.length).toBeGreaterThan(0);
    expect(seen.every((r) => r.headers()[LOCALE_HEADER] === "en")).toBe(true);
  });
});
