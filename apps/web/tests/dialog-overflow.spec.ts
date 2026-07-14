import { test, expect, type Page, type Route } from "@playwright/test";

// Stage 7.1, the two layout bugs Chris reported alongside the missing confirm
// dialogs:
//
//  1. `DialogContent` had NEITHER a max-height NOR an overflow rule, and it was
//     a CSS grid (whose items get `min-width: auto` and refuse to shrink). Long
//     content therefore PAINTED OUTSIDE the rounded card — over the backdrop,
//     off the bottom of the viewport, unreachable. The fix makes the popup
//     `flex flex-col max-h-[85dvh] overflow-hidden` with a scrollable
//     `DialogBody`; this spec proves the card now CONTAINS its content.
//
//  2. The interview's source picker truncated long titles with no way to read
//     them: "some long links are clipped so we need a mouseover tooltip to get
//     the full title." A tutor cannot decide whether to ground a curriculum in
//     a source he can only see the first 30 characters of.
//
// Both are asserted with real geometry (`boundingBox`, `scrollHeight` vs
// `clientHeight`), not screenshots — an overflow bug is a measurable fact.
const API_ORIGIN = "http://localhost:8791";

// `Access-Control-Allow-Origin` must echo the app's real origin (and pair
// with `Allow-Credentials`) rather than "*": since the auth slice made every
// call in `lib/api.ts` `credentials: "include"`, a browser REJECTS a
// wildcard-ACAO response outright — the page then renders its "could not
// load" error and every assertion below it fails for a reason that has
// nothing to do with what the test is checking.
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

/** The realistic worst case, and the one he actually hit: a library of URL
 * sources whose "titles" are long links, more of them than fit on screen. */
const LONG_TITLE =
  "Getting Great Guitar Sounds — https://www.example.com/library/collections/guitar-tone-and-gear/sources/complete-annotated-reference-edition-with-appendices-and-index.pdf";

const SOURCE_OPTIONS = Array.from({ length: 24 }, (_, i) => ({
  value: `source-${i}`,
  label: i === 0 ? LONG_TITLE : `Reference source number ${i} — a perfectly ordinary title`,
  type: i % 2 === 0 ? "pdf" : "url",
  char_count: 12000 + i,
  default_selected: i < 3,
}));

/** Answers `GET /curricula` and `POST /curricula/interview`, and nothing else.
 * The interview's state machine is server-side (see `app.curriculum.interview`)
 * and the dialog renders whatever step the API reports — so a mock that opens
 * DIRECTLY on the "sources" step is legitimate, not a shortcut: that is exactly
 * the payload a real resumed interview can return. */
async function mockInterviewOnSourcesStep(page: Page) {
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    // The shell probes auth + settings on EVERY page (`GET /auth/me`, `GET
    // /settings` — both added by the auth/settings slices). Answered here so
    // they never land in `unexpected` and never 500 the nav out from under a
    // page this spec is trying to drive.
    if (method === "GET" && pathname === "/auth/me") {
      return json({ authenticated: true, auth_enabled: false });
    }
    if (method === "GET" && pathname === "/settings") {
      return json({ provider: "anthropic", model: "claude-sonnet-5", configured: true, key_hint: "ab12" });
    }

    if (pathname === "/curricula" && method === "GET") return json([]);
    if (pathname === "/curricula/interview" && method === "POST") {
      return json(
        {
          interview_id: "interview-1",
          step: "sources",
          question: "Which sources should this curriculum be grounded in?",
          options: SOURCE_OPTIONS,
          findings: null,
          error: null,
        },
        201,
      );
    }

    unexpected.push(`${method} ${pathname}`);
    return json({ detail: "unmocked request in test" }, 500);
  }

  await page.route((url) => url.origin === API_ORIGIN, handler);
  return { unexpected };
}

async function openSourcesStep(page: Page) {
  await page.goto("/en/curricula");
  await page.getByTestId("curricula-generate-button").click();
  await page.getByTestId("interview-title").fill("Tone Shaping Fundamentals");
  await page.getByTestId("interview-start-submit").click();
  await expect(page.getByTestId("interview-sources-empty")).toHaveCount(0);
  await expect(page.getByTestId("interview-source-row-source-0")).toBeVisible();
}

test("a long dialog stays inside its own card instead of painting over the page", async ({ page }) => {
  const mock = await mockInterviewOnSourcesStep(page);
  await page.setViewportSize({ width: 900, height: 600 }); // deliberately short
  await openSourcesStep(page);

  const popup = page.locator('[data-slot="dialog-content"]');
  const box = await popup.boundingBox();
  expect(box).not.toBeNull();

  const viewport = page.viewportSize()!;
  // The card is fully on screen — top and bottom both inside the viewport.
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height + 1);
  // ...and no taller than the 85dvh cap.
  expect(box!.height).toBeLessThanOrEqual(viewport.height * 0.85 + 1);

  // The popup itself does not scroll (overflow-hidden) — the BODY does. That
  // split is the fix: if the popup were the scroller, the sticky footer would
  // scroll away with the content.
  const popupOverflows = await popup.evaluate((el) => el.scrollHeight - el.clientHeight > 1);
  expect(popupOverflows).toBe(false);

  // ...the BODY is. `min-height: 0` is the load-bearing half of that: a flex
  // item defaults to `min-height: auto` and refuses to shrink below its
  // content, so `overflow-y: auto` alone would never engage and the column
  // would simply grow past the cap again. Asserted on computed style rather
  // than on a measured overflow, because whether THIS step happens to overflow
  // depends on the step (the source list has its own inner `max-h-72`) — the
  // container's ability to contain it must hold either way.
  const body = page.getByTestId("interview-body");
  const bodyStyle = await body.evaluate((el) => {
    const s = getComputedStyle(el);
    return { overflowY: s.overflowY, minHeight: s.minHeight };
  });
  expect(bodyStyle.overflowY).toBe("auto");
  expect(bodyStyle.minHeight).toBe("0px");

  // Every option is reachable by scrolling INSIDE the card — the last source
  // in a 24-source library is not stranded off the bottom of the screen.
  const last = page.getByTestId(`interview-source-row-source-${SOURCE_OPTIONS.length - 1}`);
  await last.scrollIntoViewIfNeeded();
  const lastBox = await last.boundingBox();
  expect(lastBox!.y + lastBox!.height).toBeLessThanOrEqual(viewport.height + 1);

  expect(mock.unexpected).toEqual([]);
});

test("a clipped source title reveals itself in full on hover", async ({ page }) => {
  const mock = await mockInterviewOnSourcesStep(page);
  await page.setViewportSize({ width: 900, height: 600 });
  await openSourcesStep(page);

  const label = page.getByTestId("interview-source-label-source-0");
  await expect(label).toBeVisible();

  // It really is clipped (line-clamp-2), i.e. the tooltip is not decoration:
  // without it this text is simply unreadable.
  const clipped = await label.evaluate((el) => el.scrollHeight - el.clientHeight > 1);
  expect(clipped).toBe(true);

  // The row must not have pushed the card wider than the dialog either — that
  // was the `<fieldset>`'s missing `min-w-0` (a flex item won't shrink below
  // its content without it).
  const rowBox = (await page.getByTestId("interview-source-row-source-0").boundingBox())!;
  const popupBox = (await page.locator('[data-slot="dialog-content"]').boundingBox())!;
  expect(rowBox.x + rowBox.width).toBeLessThanOrEqual(popupBox.x + popupBox.width + 1);

  await label.hover();
  const tooltip = page.locator('[data-slot="tooltip-content"]');
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toHaveText(LONG_TITLE);

  expect(mock.unexpected).toEqual([]);
});
