import { test, expect } from "@playwright/test";

// Anchored to the API's own origin for the `/jobs/**`/`/lessons/**` mocks
// below (NOT a bare `**/jobs/{id}`-style suffix glob) — same reasoning
// `cockpit.spec.ts`/`lessons.spec.ts` document at their own `API_ORIGIN`:
// this app's OWN post-draft destination page (`/en/lessons/{id}`) ends in
// exactly the same suffix a bare glob would match, which would swallow that
// client-side navigation instead of only the API call.
const API_ORIGIN = "http://localhost:8791";

const page47 = {
  id: "p47", page_no: 47, status: "ready", total_pages: 77,
  image_url: "/media/pages/p47.jpg",
  text: "The Tube Screamer is not really a distortion box so much as a mid-range hump.",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/knowledge/sources/s1/pages/47", (r) => r.fulfill({ json: page47 }));
  await page.route("**/knowledge/sources/s1/pages/48", (r) =>
    r.fulfill({ json: { ...page47, id: "p48", page_no: 48, text: "Next page." } }));
  await page.route("**/media/pages/*.jpg", (r) =>
    r.fulfill({ contentType: "image/jpeg", body: Buffer.from("") }));
});

test("shows the scan beside its selectable text", async ({ page }) => {
  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("page-scan")).toBeVisible();
  await expect(page.getByTestId("page-text")).toContainText("Tube Screamer");
  await expect(page.getByTestId("page-indicator")).toContainText("47");
  await expect(page.getByTestId("page-indicator")).toContainText("77");
});

test("paging forward loads the next page", async ({ page }) => {
  await page.goto("/en/library/s1?page=47");
  await page.getByTestId("page-next").click();
  await expect(page.getByTestId("page-text")).toContainText("Next page.");
});

/** Selects the whole `page-text` node — same DOM dance both tests below
 * share, factored out once they both needed it. */
async function selectPageText(page: import("@playwright/test").Page) {
  await page.evaluate(() => {
    const el = document.querySelector('[data-testid="page-text"]')!;
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
}

test("selecting text reveals 'author a lesson', drafts it (202 + poll), and lands in the editor", async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  let jobPolls = 0;

  // POST /lessons/from-selection: async since Plan 10 Task 1 — 202 + job_id,
  // NOT the old stub's 201 + echo.
  await page.route(`${API_ORIGIN}/lessons/from-selection`, (r) => {
    body = r.request().postDataJSON();
    return r.fulfill({ status: 202, json: { job_id: "job1", status: "pending" } });
  });

  // GET /jobs/job1: "running" on the first poll, "succeeded" from the
  // second — proves the Reader actually polls rather than trusting the 202
  // body alone (same shape `chat.spec.ts`'s `mockJobsApi` uses).
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

  // The drafted lesson's editor page, once the Reader navigates there.
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
  await expect(page.getByTestId("page-text")).toContainText("Tube Screamer");
  await selectPageText(page);

  await page.getByTestId("author-lesson").click();
  await expect(page.getByTestId("author-drafting")).toBeVisible(); // honest "this takes a while" state, not a frozen button
  await expect.poll(() => body?.page_no).toBe(47);       // provenance travels
  await expect.poll(() => body?.source_id).toBe("s1");

  await expect(page).toHaveURL("/en/lessons/lesson-xyz");
  await expect(page.getByTestId("lesson-title")).toContainText("Pick Gauge and Tone");
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
  await expect(page.getByTestId("page-text")).toContainText("Tube Screamer");
  await selectPageText(page);

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
  await expect(page.getByTestId("page-text")).not.toHaveAttribute("contenteditable", "true");
});
