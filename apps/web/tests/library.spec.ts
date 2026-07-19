import { test, expect } from "@playwright/test";

const collections = [{ id: "c1", name: "Tone & Gear", source_count: 2 }];
const sources = [
  { id: "s1", type: "pdf", title: "Getting Great Guitar Sounds",
    status: "ready", char_count: 91000, collection_id: "c1",
    pages_total: 77, pages_ready: 77, pages_failed: 0, ocr_active: false },
  { id: "s2", type: "url", title: "Humbucker (Wikipedia)",
    status: "empty", char_count: 0, collection_id: "c1" },
  { id: "s3", type: "text", title: "Loose note", status: "ready",
    char_count: 337, collection_id: null },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/library/collections", (r) => r.fulfill({ json: collections }));
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: sources }));
});

test("groups sources under their collection, with Unfiled for the rest", async ({ page }) => {
  await page.goto("/en/library");
  await expect(page.getByTestId("collection-Tone & Gear")).toContainText("Getting Great Guitar Sounds");
  await expect(page.getByTestId("collection-unfiled")).toContainText("Loose note");
});

test("an empty source is shown as broken with a retry, NOT as healthy", async ({ page }) => {
  // regression: three real sources sat "ready" with 0 chars and rendered green
  await page.goto("/en/library");
  const row = page.getByTestId("source-s2");
  await expect(row).toContainText(/empty/i);
  await expect(row.getByTestId("retry-s2")).toBeVisible();
  await expect(row.getByTestId("status-ok-s2")).toHaveCount(0);
});

test("a stale 'ready' source with 0 chars is ALSO shown as broken, not just an explicit 'empty' status", async ({ page }) => {
  // regression: the real live API still has three pre-existing rows written
  // before the backend's D6 fix landed — they report status "ready" with
  // char_count 0 (the backfill was never run). The honesty requirement is
  // about the CONTENT having nothing readable, not merely the status label,
  // so this must render broken too.
  const staleSources = [
    ...sources,
    { id: "s4", type: "url", title: "Guitar amplifier (Wikipedia)",
      status: "ready", char_count: 0, collection_id: null },
  ];
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: staleSources }));
  await page.goto("/en/library");
  const row = page.getByTestId("source-s4");
  await expect(row).toContainText(/empty/i);
  await expect(row.getByTestId("retry-s4")).toBeVisible();
  await expect(row.getByTestId("status-ok-s4")).toHaveCount(0);
});

test("retry calls the API", async ({ page }) => {
  let called = false;
  await page.route("**/knowledge/sources/s2/retry", (r) => {
    called = true;
    return r.fulfill({ status: 202, json: { job_id: "j1", already_running: false } });
  });
  await page.goto("/en/library");
  await page.getByTestId("retry-s2").click();
  await expect.poll(() => called).toBe(true);
});

test("a ready source links into the reader", async ({ page }) => {
  await page.goto("/en/library");
  await page.getByTestId("source-s1").getByRole("link").first().click();
  await expect(page).toHaveURL(/\/en\/library\/s1/);
});

// --- Stage 7.2: the reload-during-OCR bug ---------------------------------

test("a FRESHLY LOADED tab shows a book that is mid-OCR as being read — not as empty-with-a-retry", async ({ page }) => {
  // THE BUG. Progress lived in the React state of the tab that pressed the
  // button, so a hard reload during the 9-minute OCR fell back to the source's
  // at-rest status — "empty" — and painted the book RED, "nothing was read",
  // with a Retry button that started a SECOND job racing the first. This page
  // has never seen a job id; the server tells it everything.
  const mid = [{
    id: "s9", type: "pdf", title: "Getting Great Guitar Sounds",
    status: "empty", char_count: 0, collection_id: null,
    pages_total: 77, pages_ready: 29, pages_failed: 0, ocr_active: true,
  }];
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: mid }));
  await page.route("**/knowledge/sources/s9/progress", (r) =>
    r.fulfill({ json: {
      source_id: "s9", total: 77, ready: 29, failed: 0, empty: 0, pending: 48,
      current_page: 30, active: true, job_id: "j9",
    } }),
  );

  await page.goto("/en/library");

  await expect(page.getByTestId("ocr-progress-s9")).toContainText("Reading page 30 of 77");
  await expect(page.getByTestId("retry-s9")).toHaveCount(0);
  await expect(page.getByTestId("source-s9")).not.toContainText(/nothing was read/i);
});

test("a partially-read book is AMBER with a retry for just the failed pages — never a green checkmark", async ({ page }) => {
  const partial = [{
    id: "s8", type: "pdf", title: "Getting Great Guitar Sounds",
    status: "partial", char_count: 88000, collection_id: null,
    pages_total: 77, pages_ready: 71, pages_failed: 6, pages_pending: 0, ocr_active: false,
  }];
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: partial }));
  await page.goto("/en/library");

  const row = page.getByTestId("source-s8");
  await expect(row.getByTestId("status-partial-s8")).toContainText("71 of 77 pages read");
  await expect(row.getByTestId("status-partial-s8")).toContainText("6 failed");
  await expect(row.getByTestId("status-ok-s8")).toHaveCount(0);
  await expect(row.getByTestId("retry-s8")).toBeVisible();
  // A partial book is still a book: the Reader must open it.
  await expect(row.getByRole("link").first()).toHaveAttribute("href", /\/library\/s8/);
});

// --- Footgun fix: "partial" now also means "never read", and that must NOT
// route through the cheap, no-confirmation retry button above (b886bd5 made
// status honest about unread pages; source-row.tsx had not yet been taught
// the difference between "a few pages failed" and "nobody pressed read yet"). ---

test("a freshly uploaded, never-read book invites reading — NOT a no-confirmation retry that would silently start an 8-hour run", async ({ page }) => {
  const neverRead = [{
    id: "s10", type: "pdf", title: "Gallagher — 388 pages",
    status: "partial", char_count: 0, collection_id: null,
    pages_total: 388, pages_ready: 0, pages_failed: 0, pages_pending: 388, ocr_active: false,
  }];
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: neverRead }));
  await page.goto("/en/library");

  const row = page.getByTestId("source-s10");
  // The old, cheap, no-confirmation retry button must NOT be offered for this shape.
  await expect(row.getByTestId("retry-s10")).toHaveCount(0);
  await expect(row.getByTestId("status-partial-s10")).toHaveCount(0);
  // It must read as an invitation, not an error: no "failed" framing.
  await expect(row.getByTestId("status-unread-s10")).toBeVisible();
  await expect(row.getByTestId("status-unread-s10")).not.toContainText(/failed/i);

  // Pressing the CTA must go through the SAME confirming dialog as "reocr" —
  // stating the page count and the cost — not fire the request directly.
  let retryHit = false;
  let reocrHit = false;
  await page.route("**/knowledge/sources/s10/retry", (r) => { retryHit = true; return r.fulfill({ status: 202, json: { job_id: "j1" } }); });
  await page.route("**/knowledge/sources/s10/reocr", (r) => { reocrHit = true; return r.fulfill({ status: 202, json: { job_id: "j1" } }); });

  await row.getByTestId("start-reading-s10").click();
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await expect(page.getByTestId("confirm-body")).toContainText("388");
  expect(retryHit, "must not have fired the request before confirmation").toBe(false);

  await page.getByTestId("confirm-accept").click();
  await expect.poll(() => reocrHit).toBe(true);
  expect(retryHit, "a never-read book must start via reocr, not retry").toBe(false);
});

test("an interrupted run (some read, some still pending) is a RESUME, not 'failed pages' — also confirms first", async ({ page }) => {
  const interrupted = [{
    id: "s11", type: "pdf", title: "Hunter — 184 pages",
    status: "partial", char_count: 40000, collection_id: null,
    pages_total: 184, pages_ready: 30, pages_failed: 0, pages_pending: 154, ocr_active: false,
  }];
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: interrupted }));
  await page.goto("/en/library");

  const row = page.getByTestId("source-s11");
  await expect(row.getByTestId("retry-s11")).toHaveCount(0);
  await expect(row.getByTestId("status-unread-s11")).toContainText("30 of 184 pages read so far");
  await expect(row.getByTestId("status-unread-s11")).not.toContainText(/failed/i);

  let reocrHit = false;
  await page.route("**/knowledge/sources/s11/reocr", (r) => { reocrHit = true; return r.fulfill({ status: 202, json: { job_id: "j1" } }); });

  await row.getByTestId("start-reading-s11").click();
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await page.getByTestId("confirm-accept").click();
  await expect.poll(() => reocrHit).toBe(true);
});

// --- Stage 8: ask your library --------------------------------------------

test("searching the library returns cited hits that link into the reader AT THE PAGE", async ({ page }) => {
  await page.route("**/knowledge/search", (r) =>
    r.fulfill({ json: { hits: [{
      chunk_id: "ch1", source_id: "s1", source_title: "Getting Great Guitar Sounds",
      text: "The Tube Screamer is the classic mid-humped overdrive; its 808 circuit ...",
      section_path: null, page: 43, page_id: "p43", score: 0.032,
      vector_score: 0.88, lexical_score: 4.1,
    }] } }),
  );

  await page.goto("/en/library");
  await page.getByTestId("library-search-input").fill("Tube Screamer");
  await page.getByTestId("library-search-submit").click();

  const hit = page.getByTestId("search-hit-ch1");
  await expect(hit).toContainText("Getting Great Guitar Sounds");
  await expect(hit).toContainText("p. 43");
  await expect(hit).toContainText(/Tube Screamer/);

  await hit.click();
  await expect(page).toHaveURL(/\/en\/library\/s1\?page=43/);
});

test("a search with no hits says so, honestly", async ({ page }) => {
  await page.route("**/knowledge/search", (r) => r.fulfill({ json: { hits: [] } }));
  await page.goto("/en/library");
  await page.getByTestId("library-search-input").fill("theremin");
  await page.getByTestId("library-search-submit").click();
  await expect(page.getByTestId("library-search-empty")).toContainText("theremin");
});

// --- C7: concept-canon compile status on the source row -------------------

test("a compiled book shows its concept count and links into the canon", async ({ page }) => {
  await page.route("**/knowledge/sources", (r) =>
    r.fulfill({
      json: [
        {
          id: "s1", type: "pdf", title: "Modern Guitar Rigs (Kahn)",
          status: "ready", char_count: 120000, collection_id: null,
          pages_total: 200, pages_ready: 200, pages_failed: 0, ocr_active: false,
          compile: { status: "ready", concept_count: 34, compiled_at: "2026-07-17T00:00:00Z", model: "claude-sonnet-5", error: null },
        },
      ],
    }),
  );
  await page.goto("/en/library");
  const line = page.getByTestId("compile-ready-s1");
  await expect(line).toContainText("34 concepts in the canon");
  await expect(line).toHaveAttribute("href", "/en/canon");
});

test("a compiled book offers a Recompile button that force-recompiles via the API", async ({ page }) => {
  await page.route("**/knowledge/sources", (r) =>
    r.fulfill({
      json: [
        {
          id: "s1", type: "pdf", title: "Modern Guitar Rigs (Kahn)",
          status: "ready", char_count: 120000, collection_id: null,
          pages_total: 200, pages_ready: 200, pages_failed: 0, ocr_active: false,
          compile: { status: "ready", concept_count: 34, compiled_at: "2026-07-17T00:00:00Z", model: "claude-sonnet-5", error: null },
        },
      ],
    }),
  );
  let forced = false;
  await page.route("**/knowledge/sources/s1/compile**", (r) => {
    if (r.request().url().includes("force=true")) forced = true;
    return r.fulfill({ status: 202, json: { job_id: "j1", already_running: false } });
  });
  await page.goto("/en/library");
  await page.getByTestId("recompile-s1").click();
  // The confirm dialog spells out the real spend AND the replacement before it fires.
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await page.getByTestId("confirm-accept").click();
  await expect.poll(() => forced).toBe(true);
});

test("a book being read into the canon shows an honest 'reading' status", async ({ page }) => {
  await page.route("**/knowledge/sources", (r) =>
    r.fulfill({
      json: [
        {
          id: "s1", type: "pdf", title: "Tone Manual (Hunter)",
          status: "ready", char_count: 300000, collection_id: null,
          pages_total: 388, pages_ready: 388, pages_failed: 0, ocr_active: false,
          compile: { status: "running", concept_count: null, compiled_at: null, model: "claude-sonnet-5", error: null },
        },
      ],
    }),
  );
  await page.goto("/en/library");
  await expect(page.getByTestId("compile-running-s1")).toBeVisible();
});

test("an uncompiled but readable book offers a Compile button that calls the API", async ({ page }) => {
  await page.route("**/knowledge/sources", (r) =>
    r.fulfill({
      json: [
        {
          id: "s1", type: "pdf", title: "A readable, uncompiled book",
          status: "ready", char_count: 50000, collection_id: null,
          pages_total: 60, pages_ready: 60, pages_failed: 0, ocr_active: false,
          compile: null,
        },
      ],
    }),
  );
  let called = false;
  await page.route("**/knowledge/sources/s1/compile", (r) => {
    called = true;
    return r.fulfill({ status: 202, json: { job_id: "j1", already_running: false } });
  });
  await page.goto("/en/library");
  await expect(page.getByTestId("compile-none-s1")).toBeVisible();
  await page.getByTestId("compile-s1").click();
  // The confirm dialog states the cost before spending anything.
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await page.getByTestId("confirm-accept").click();
  await expect.poll(() => called).toBe(true);
});

test("an API that does not report compile status shows no compile line (no false 'not compiled')", async ({ page }) => {
  await page.route("**/knowledge/sources", (r) =>
    r.fulfill({
      json: [
        {
          id: "s1", type: "pdf", title: "Book from an older API",
          status: "ready", char_count: 50000, collection_id: null,
          pages_total: 60, pages_ready: 60, pages_failed: 0, ocr_active: false,
          // no `compile` key at all — the API predates C7
        },
      ],
    }),
  );
  await page.goto("/en/library");
  await expect(page.getByTestId("status-ok-s1")).toBeVisible();
  await expect(page.getByTestId("compile-none-s1")).toHaveCount(0);
  await expect(page.getByTestId("compile-ready-s1")).toHaveCount(0);
});
