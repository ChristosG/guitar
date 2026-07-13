import { test, expect } from "@playwright/test";

// Anchored to the API's own origin for the `/jobs/**`/`/lessons/**` mocks
// below (NOT a bare `**/jobs/{id}`-style suffix glob) — same reasoning
// `cockpit.spec.ts`/`lessons.spec.ts` document at their own `API_ORIGIN`:
// this app's OWN post-draft destination page (`/en/lessons/{id}`) ends in
// exactly the same suffix a bare glob would match, which would swallow that
// client-side navigation instead of only the API call.
const API_ORIGIN = "http://localhost:8791";

// Mirrors the real book's shape (Plan 12 Task 4 / G4's whole reason for
// being — 77 real scanned pages in the live DB) so the "not all fetched at
// once" lazy-image test is a real proof, not a toy of 2-3 pages.
const TOTAL_PAGES = 77;

function manifest() {
  return Array.from({ length: TOTAL_PAGES }, (_, i) => ({ page_no: i + 1, status: "ready" }));
}

function pageDetail(pageNo: number) {
  let text = `Page ${pageNo} body text.`;
  if (pageNo === 47) {
    text = "The Tube Screamer is not really a distortion box so much as a mid-range hump.";
  } else if (pageNo === 48) {
    text = "Next page picks up right where the last one left off.";
  } else if (pageNo === 21) {
    text = "Page 21 body text.";
  }
  return {
    id: `p${pageNo}`, page_no: pageNo, status: "ready", total_pages: TOTAL_PAGES,
    image_url: `/media/pages/p${pageNo}.jpg`, text,
  };
}

let imageRequests: string[];

test.beforeEach(async ({ page }) => {
  imageRequests = [];

  // The manifest — exact-suffix match ("pages", nothing after it) so it
  // never swallows the per-page detail route below.
  await page.route(`${API_ORIGIN}/knowledge/sources/s1/pages`, (r) =>
    r.fulfill({ json: manifest() }));

  // One page's own text+scan-url detail.
  await page.route(`${API_ORIGIN}/knowledge/sources/s1/pages/*`, (r) => {
    const pageNo = Number(new URL(r.request().url()).pathname.split("/").pop());
    return r.fulfill({ json: pageDetail(pageNo) });
  });

  // The scan image itself — counted, so the lazy-loading test below can
  // prove not all 77 are requested on first paint.
  await page.route(`${API_ORIGIN}/media/pages/*.jpg`, (r) => {
    imageRequests.push(r.request().url());
    return r.fulfill({ contentType: "image/jpeg", body: Buffer.from("") });
  });
});

test("renders the whole book as a continuous scroll, not a single page with prev/next buttons", async ({ page }) => {
  await page.goto("/en/library/s1?page=1");
  await expect(page.getByTestId("page-marker-1")).toBeVisible();
  await expect(page.getByTestId("page-marker-2")).toBeAttached();
  await expect(page.getByTestId("page-marker-77")).toBeAttached(); // stacked further down the same document
  await expect(page.getByTestId("page-prev")).toHaveCount(0);
  await expect(page.getByTestId("page-next")).toHaveCount(0);
  await expect(page.getByTestId("page-indicator")).toContainText("77"); // total, from the manifest alone
});

test("?page=N deep-link scrolls to that page", async ({ page }) => {
  await page.goto("/en/library/s1?page=21");
  await expect(page.getByTestId("page-text-21")).toContainText("Page 21 body text.");
  await expect(page.getByTestId("page-marker-21")).toBeInViewport();
});

test("scans are lazy-loaded — not all 77 are requested on first paint", async ({ page }) => {
  await page.goto("/en/library/s1?page=1");
  // Give the IntersectionObserver a moment to settle on the initial
  // viewport before asserting — same "let the async settle" reasoning as
  // this suite's job-polling tests, just for a browser API instead of a
  // network poll.
  await expect(page.getByTestId("page-text-1")).toBeVisible();
  await expect.poll(() => imageRequests.length, { timeout: 3000 }).toBeGreaterThan(0);
  await page.waitForTimeout(500);
  expect(imageRequests.length).toBeLessThan(20); // nowhere near all 77
});

/** Selects a whole page's text node — same DOM dance the original
 * single-page test used, just against a page-scoped testid now that every
 * page's text pane shares the same `page-text` prefix. */
async function selectWholePage(page: import("@playwright/test").Page, pageNo: number) {
  await page.evaluate((n) => {
    const el = document.querySelector(`[data-testid="page-text-${n}"]`)!;
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  }, pageNo);
}

/** Selects from the start of one page's text pane to the end of another's —
 * the cross-page-boundary selection G4 exists for. */
async function selectAcrossPages(page: import("@playwright/test").Page, fromPage: number, toPage: number) {
  await page.evaluate(
    ({ fromPage, toPage }) => {
      const from = document.querySelector(`[data-testid="page-text-${fromPage}"]`)!;
      const to = document.querySelector(`[data-testid="page-text-${toPage}"]`)!;
      const range = document.createRange();
      range.setStart(from, 0);
      range.setEnd(to, to.childNodes.length);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      document.dispatchEvent(new Event("selectionchange"));
    },
    { fromPage, toPage },
  );
}

test("selecting text reveals 'author a lesson', drafts it (202 + poll), and lands in the editor", async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  let jobPolls = 0;

  await page.route(`${API_ORIGIN}/lessons/from-selection`, (r) => {
    body = r.request().postDataJSON();
    return r.fulfill({ status: 202, json: { job_id: "job1", status: "pending" } });
  });

  await page.route(`${API_ORIGIN}/jobs/job1`, (r) => {
    jobPolls++;
    const base = {
      id: "job1", kind: "lesson", error: null, error_kind: null,
      created_at: "2026-07-01T00:00:00Z", updated_at: "2026-07-01T00:00:00Z",
    };
    if (jobPolls < 2) {
      return r.fulfill({ json: { ...base, status: "running", result_root_id: null } });
    }
    return r.fulfill({ json: { ...base, status: "succeeded", result_root_id: "lesson-xyz" } });
  });

  await page.route(`${API_ORIGIN}/lessons`, (r) => r.fulfill({ json: [] }));
  await page.route(`${API_ORIGIN}/lessons/lesson-xyz`, (r) =>
    r.fulfill({
      json: {
        id: "lesson-xyz", kind: "lesson", title: "Pick Gauge and Tone", body: null,
        est_minutes: null, order: 0, language: "en", plane: "content", student_id: null,
        children: [],
      },
    }));

  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("page-text-47")).toContainText("Tube Screamer");
  await selectWholePage(page, 47);

  await page.getByTestId("author-lesson").click();
  await expect(page.getByTestId("author-drafting")).toBeVisible(); // honest "this takes a while" state, not a frozen button
  await expect.poll(() => body?.page_from).toBe(47); // single-page selection -> from === to
  await expect.poll(() => body?.page_to).toBe(47);
  await expect.poll(() => body?.source_id).toBe("s1");

  await expect(page).toHaveURL("/en/lessons/lesson-xyz");
  await expect(page.getByTestId("lesson-title")).toContainText("Pick Gauge and Tone");
});

test("a selection spanning two pages posts BOTH page_from and page_to", async ({ page }) => {
  let body: Record<string, unknown> | null = null;

  await page.route(`${API_ORIGIN}/lessons/from-selection`, (r) => {
    body = r.request().postDataJSON();
    return r.fulfill({ status: 202, json: { job_id: "job1", status: "pending" } });
  });
  await page.route(`${API_ORIGIN}/jobs/job1`, (r) =>
    r.fulfill({
      json: {
        id: "job1", kind: "lesson", status: "succeeded", result_root_id: "lesson-xyz",
        error: null, error_kind: null,
        created_at: "2026-07-01T00:00:00Z", updated_at: "2026-07-01T00:00:00Z",
      },
    }));
  await page.route(`${API_ORIGIN}/lessons`, (r) => r.fulfill({ json: [] }));
  await page.route(`${API_ORIGIN}/lessons/lesson-xyz`, (r) =>
    r.fulfill({
      json: {
        id: "lesson-xyz", kind: "lesson", title: "Grounded in Both Pages", body: null,
        est_minutes: null, order: 0, language: "en", plane: "content", student_id: null,
        children: [],
      },
    }));

  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("page-text-47")).toContainText("Tube Screamer");
  await expect(page.getByTestId("page-text-48")).toContainText("Next page"); // fetched up front alongside 47, per this file's own docstring
  await selectAcrossPages(page, 47, 48);

  await expect(page.getByTestId("selection-range")).toContainText("47");
  await expect(page.getByTestId("selection-range")).toContainText("48");

  await page.getByTestId("author-lesson").click();
  await expect.poll(() => body?.page_from).toBe(47);
  await expect.poll(() => body?.page_to).toBe(48);
  await expect(page).toHaveURL("/en/lessons/lesson-xyz");
});

test("a failed draft surfaces the error and offers retry", async ({ page }) => {
  let attempts = 0;
  await page.route(`${API_ORIGIN}/lessons/from-selection`, (r) => {
    attempts++;
    return r.fulfill({ status: 202, json: { job_id: `job${attempts}`, status: "pending" } });
  });
  await page.route(`${API_ORIGIN}/jobs/**`, (r) =>
    r.fulfill({
      json: {
        id: "job1", kind: "lesson", status: "failed", result_root_id: null,
        error: "Lesson drafting failed (model returned invalid/truncated output). Try again.",
        error_kind: "upstream", created_at: "2026-07-01T00:00:00Z", updated_at: "2026-07-01T00:00:00Z",
      },
    }));

  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("page-text-47")).toContainText("Tube Screamer");
  await selectWholePage(page, 47);

  await page.getByTestId("author-lesson").click();
  await expect(page.getByTestId("author-error")).toContainText("Try again");
  await expect(page.getByTestId("author-retry")).toBeVisible();

  await page.getByTestId("author-retry").click();
  await expect.poll(() => attempts).toBe(2); // retry re-submits the SAME still-selected passage
});

test("there is NO OCR editing affordance", async ({ page }) => {
  // spec D5 — the tutor must never be handed the machine's chores
  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("fix-ocr")).toHaveCount(0);
  await expect(page.getByTestId("page-text-47")).not.toHaveAttribute("contenteditable", "true");
});
