import { test, expect, type Locator, type Page } from "@playwright/test";

/**
 * PRESS "READ", THROUGH THE BROWSER — and don't wait for the book to finish.
 *
 * Sibling of `ingest-a-book.spec.ts` (its steps 2-4, verbatim in spirit).
 * That file uploads a 57-page book AND blocks for the ~40 minutes it takes
 * to read it, in one test, because for one small book that is honest and
 * cheap. It does not scale to three books of 176/184/388 pages: the
 * tutor's subscription is a 5-HOUR ROLLING CAP, so a book this size WILL get
 * rate-limited mid-run, at which point the server parks it (page reverts to
 * `pending`, the job ends `succeeded`) and a human has to come back and press
 * the button again — see `app/routers/library.py::reocr_source`'s
 * docstring: "come back tomorrow, press it again."
 *
 * So this file does exactly the one deliberate, human act — open the
 * library, find the book, press Read (or Continue reading), read the
 * dialog that says what it costs, accept it — and then confirms the job
 * genuinely started (a progress line appears) before exiting. It is run
 * ONCE PER BOOK to start it, and RE-RUN to resume it after a rate-limit
 * park; actual progress is watched separately by polling the DB
 * (read-only), per the run's own instructions, not by keeping a browser
 * tab (or a Playwright worker) open for hours.
 *
 * Idempotent: if the book is already being read (progress visible) or is
 * already fully `ready`, this is a no-op that says so and exits — safe to
 * re-run blindly as a "resume" action without checking state by hand first.
 *
 * Select the book with BOOK_TITLE (a substring match against the row, same
 * as the shelf search) and its total page count with BOOK_PAGES:
 *
 *   BOOK_TITLE="Modern guitar rigs" BOOK_PAGES=176 npm run test:e2e -- e2e/start-ocr.spec.ts
 */

const PASSWORD = process.env.APP_PASSWORD ?? "guitar24";
const BOOK_TITLE = process.env.BOOK_TITLE;
const BOOK_PAGES = process.env.BOOK_PAGES ? Number(process.env.BOOK_PAGES) : undefined;

async function signIn(page: Page): Promise<void> {
  await page.goto("/el/library");
  if (page.url().includes("/login")) {
    await page.getByTestId("login-password").fill(PASSWORD);
    await page.getByTestId("login-submit").click();
  }
  await expect(page.getByTestId("library-heading")).toBeVisible();
  await expect(page).toHaveURL(/\/el\/library$/);
}

function bookRow(page: Page, title: string): Locator {
  return page.locator('[data-testid^="source-"]').filter({ hasText: title });
}

async function sourceIdOf(row: Locator): Promise<string> {
  const testId = await row.getAttribute("data-testid");
  expect(testId, "the source row must carry a data-testid").toBeTruthy();
  return testId!.replace(/^source-/, "");
}

test.describe("pressing Read on one big book, the way the tutor does", () => {
  test.setTimeout(5 * 60 * 1000);

  test("start (or resume) OCR for BOOK_TITLE", async ({ page }) => {
    test.skip(!BOOK_TITLE, "set BOOK_TITLE (and ideally BOOK_PAGES) to pick the book");
    const log = (msg: string) => console.log(`[${BOOK_TITLE}] ${msg}`);

    await signIn(page);

    const row = bookRow(page, BOOK_TITLE!);
    await expect(row, `"${BOOK_TITLE}" must already be on the shelf (upload it first)`).toHaveCount(1, {
      timeout: 30_000,
    });
    const id = await sourceIdOf(row);
    log(`found on the shelf as ${id}`);

    // Already reading? Nothing to do — this press would be a no-op the server
    // itself ignores (the in-flight guard), so don't even try it.
    const progress = page.getByTestId(`ocr-progress-${id}`);
    if (await progress.isVisible().catch(() => false)) {
      log(`already reading: "${(await progress.textContent())?.trim()}" — nothing to do`);
      return;
    }

    // Already fully read? Also nothing to do.
    const ok = page.getByTestId(`status-ok-${id}`);
    if (await ok.isVisible().catch(() => false)) {
      log(`already ready: "${(await ok.textContent())?.trim()}" — nothing to do`);
      return;
    }

    // Otherwise: press it. Either the small icon button (always present on a
    // non-reading pdf source) or the bigger "start/continue reading" button in
    // the unread/resuming state — both open the same confirm dialog.
    log("pressing the read/continue-reading button");
    await page.getByTestId(`reocr-${id}`).click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    if (BOOK_PAGES) {
      await expect(page.getByTestId("confirm-body")).toContainText(String(BOOK_PAGES));
    }
    await expect(page.getByTestId("confirm-body")).toContainText(/ώρες/);
    const body = (await page.getByTestId("confirm-body").textContent())?.trim();
    log(`confirm dialog: "${body}"`);
    await page.getByTestId("confirm-accept").click();

    // Confirm the job genuinely started before this test exits — a silent
    // no-op here would look identical to "started" from the outside.
    await expect(progress, "pressing Read must start a visible, server-backed job").toBeVisible({
      timeout: 60_000,
    });
    log(`started: "${(await progress.textContent())?.trim()}"`);
  });
});
