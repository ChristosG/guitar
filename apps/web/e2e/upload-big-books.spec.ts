import { test, expect, type Locator, type Page } from "@playwright/test";
import path from "node:path";

/**
 * UPLOAD THE THREE BIG BOOKS, THROUGH THE BROWSER — and stop there.
 *
 * Sibling of `ingest-a-book.spec.ts` (read that file first; this one follows
 * its conventions exactly), split into its own file because this run's shape
 * is different: three books, uploaded first and READ later, one at a time,
 * over many hours and likely several sittings across the tutor's 5-hour
 * subscription window. Mixing "upload" and "wait 8-20 hours for three books
 * to be read" into one Playwright test would mean a single flaky step near
 * the end throws away every upload that came before it; this file's only job
 * is the fast, cheap, deterministic half — pagination, not vision.
 *
 * WHAT IT PROVES, same as steps 1-2 of `ingest-a-book.spec.ts`:
 *   1. Each PDF uploads through the real Add-source dialog and appears on the
 *      shelf with the right page count.
 *   2. Uploading did NOT start reading it — no `ocr-progress-{id}`, and the
 *      re-read control is there, waiting to be pressed on purpose. Starting
 *      OCR is a separate, deliberate act (`start-ocr.spec.ts`), run once per
 *      book, one book at a time.
 *
 * RUNTIME: a few minutes total — three PDFs' worth of local page rendering,
 * no model calls.
 */

const PASSWORD = process.env.APP_PASSWORD ?? "guitar24";

const BOOKS: { title: string; file: string; pages: number }[] = [
  {
    title: "Modern guitar rigs: the tone fanatics guide to integrating amps and effects (Kahn, Scott)",
    file: "/mnt/nvme2TB/guitar_tutor/books/Modern guitar rigs  the tone fanatics guide to integrating amps and effects (Kahn, Scott).pdf",
    pages: 176,
  },
  {
    title: "Tone Manual: Discovering Your Ultimate Electric Guitar Sound (Dave Hunter)",
    file: "/mnt/nvme2TB/guitar_tutor/books/Tone Manual Discovering Your Ultimate Electric Guitar Sound (Dave Hunter).pdf",
    pages: 184,
  },
  {
    title: "Guitar tone: pursuing the ultimate guitar sound (Gallagher, Mitch)",
    file: "/mnt/nvme2TB/guitar_tutor/books/Guitar tone  pursuing the ultimate guitar sound (Gallagher, Mitch).pdf",
    pages: 388,
  },
];

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

/** Leave a clean slate before uploading — this run must land as exactly one
 * copy of each book, not a duplicate from a previous partial attempt. */
async function removeExistingCopy(page: Page, title: string): Promise<void> {
  const row = bookRow(page, title);
  if ((await row.count()) === 0) return;
  const id = await sourceIdOf(row.first());
  test.info().annotations.push({
    type: "cleanup",
    description: `a previous copy of "${title}" (${id}) was on the shelf; removing it first`,
  });
  await page.getByTestId(`delete-${id}`).click();
  await page.getByTestId("confirm-accept").click();
  await expect(bookRow(page, title)).toHaveCount(0, { timeout: 60_000 });
}

test.describe("uploading the three big books, the way the tutor does", () => {
  // Local page-rendering only (no model calls), but 388 pages of a 360dpi
  // scan is still real work — generous headroom, not a tight bound.
  test.setTimeout(20 * 60 * 1000);

  for (const book of BOOKS) {
    test(`upload "${book.title}" (${book.pages}pp) and confirm OCR did NOT start`, async ({ page }) => {
      const startedAt = Date.now();
      const log = (msg: string) => console.log(`[${book.title}] ${msg}`);

      await signIn(page);
      await removeExistingCopy(page, book.title);

      log("opening the add-source dialog");
      await page.getByTestId("add-source-trigger").click();
      await expect(page.getByTestId("add-source-dialog")).toBeVisible();
      await page.getByTestId("add-source-mode-pdf").click();
      await page.getByTestId("add-source-title").fill(book.title);
      await page.getByTestId("add-source-file").setInputFiles(path.resolve(book.file));

      log(`uploading — the API paginates all ${book.pages} pages in the request`);
      await page.getByTestId("add-source-submit").click();

      await expect(page.getByTestId("add-source-dialog")).toBeHidden({ timeout: 15 * 60 * 1000 });
      const row = bookRow(page, book.title);
      await expect(row, "the book must appear on the shelf after upload").toHaveCount(1, { timeout: 60_000 });
      const id = await sourceIdOf(row);
      log(`on the shelf as ${id} — took ${Math.round((Date.now() - startedAt) / 1000)}s to upload`);

      // Uploading must NOT have started reading it.
      await expect(
        page.getByTestId(`ocr-progress-${id}`),
        "uploading a PDF must NOT start an hours-long Claude run",
      ).toHaveCount(0);
      await expect(
        page.getByTestId(`reocr-${id}`),
        "the tutor must be able to start the read from the UI, without anyone's help",
      ).toBeVisible();

      // The unread-book affordance ("Έναρξη ανάγνωσης") should quote the full
      // page count — the same fact this run will confirm against the DB.
      const unread = page.getByTestId(`status-unread-${id}`);
      await expect(unread).toBeVisible({ timeout: 30_000 });
      await expect(unread).toContainText(String(book.pages));
      log(`confirmed: ${book.pages} unread pages on the shelf, nothing started`);
    });
  }
});
