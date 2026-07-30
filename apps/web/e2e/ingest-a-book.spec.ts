import { test, expect, type Locator, type Page } from "@playwright/test";
import path from "node:path";

/**
 * INGEST A REAL BOOK, THROUGH THE BROWSER, ON THE REAL SUBSCRIPTION.
 *
 * Chris, on why this file exists rather than another backend test:
 *
 *   "make sure that you test one of the books via the FE, and not merely from
 *    the backend, so that we know our frontend will work, and we can do it
 *    ourselves from UI and not by telling you (claude) to do it."
 *
 * The app is HIS to operate. So every step below is a thing a person does with a
 * mouse: log in, press "Add source", pick a file, press the re-read button,
 * confirm the dialog that says what it costs, watch the progress line, open the
 * Reader. There is no `page.route`, no seeded fixture, and deliberately not one
 * `request.post` — if any step here needed a curl to work, that would be a bug
 * in the UI, and the whole point of this test is to find that out. A green
 * backend is not the same claim as "he can do it himself".
 *
 * WHAT IT PROVES, in order:
 *   1. Uploading a PDF does NOT start reading it. Uploading is free (page scans
 *      + routing); READING is ~40s/page of Claude against a 5-hour subscription
 *      cap, and 888 pages across his four books is 8-12 hours. That must never
 *      begin because someone dropped a file on a page. This test asserts the
 *      absence of a progress line right after upload — the regression that
 *      `1ef6b2d fix(library): uploading a PDF must not silently start an 8-hour
 *      Claude run` fixed, which no mocked test can catch because it is a fact
 *      about what the SERVER did.
 *   2. The re-read button is reachable, and says what it costs before it runs.
 *   3. Progress is reported the whole way. The tutor is never left staring at
 *      nothing for 40 minutes.
 *   4. THE POINT: the tab pages were actually READ, and the publisher's text
 *      SURVIVED being read. Powers is a book of guitar exercises — 43 of its 57
 *      pages carry raster tab images over a perfectly good publisher text layer.
 *      Those pages must come back as text_layer+claude: the caption KEPT, a
 *      [FIGURE] description of the tab ADDED under it. Not `claude` alone (that
 *      would mean the model overwrote the publisher and we lied about a
 *      transcription that never happened); not `text_layer` alone (that would
 *      mean the tab images — the actual content of an exercise book — were never
 *      looked at). This test reads that back out of the Reader, in the browser,
 *      the same way he would.
 *
 * RUNTIME: ~40 minutes. 43 unread pages at ~40s each through the vision model. That
 * is not a slow test; it is the actual price of reading the book, and there is
 * no honest way to make this assertion cheaper. Do not shorten the book, and do
 * not fake the wait.
 *
 * PREREQUISITES: `docker compose up -d` (web/api/postgres), and a real Anthropic
 * API key saved in Settings (the reading spends real tokens). Run via
 * `npm run test:e2e` — it needs `playwright.live.config.ts`
 * (baseURL :8790, no webServer, no retries), NOT the default mocked config.
 */

const BOOK_PATH = "/mnt/nvme2TB/guitar_tutor/books/Guitar Exercises Made Simple (Maxwell Powers).pdf";
const BOOK_TITLE = "Guitar Exercises Made Simple (Maxwell Powers)";
const PAGE_COUNT = 57;

/** The password is the whole auth model (one tutor, one password — see
 * `login/page.tsx`). `.env`'s `APP_PASSWORD`; overridable so this can point at
 * a differently-configured stack without an edit. */
const PASSWORD = process.env.APP_PASSWORD ?? "guitar24";

/** Reading 43 pages at ~40s each is ~29 minutes; 50 gives real headroom for a
 * slow page or a bridge retry without being so loose that a genuinely hung run
 * burns an hour before failing. */
const READ_TIMEOUT_MS = 50 * 60 * 1000;

/** `ocr.py`'s figure region — the contract between the model's description and
 * the publisher's words, asserted here in the browser exactly as it is written
 * there: text INSIDE a [FIGURE]…[/FIGURE] region is ours, everything outside one
 * is the page's own words. `_marked()` guarantees BOTH ends on the describe path
 * even when the model forgets them, so the marker's absence in the Reader means
 * the page was never described. */
const FIGURE_MARKER = "[FIGURE]";
const FIGURE_END = "[/FIGURE]";

/** Wall-clock, for the progress log below (and the report). A 40-minute test
 * that prints nothing for 40 minutes is indistinguishable from a hung one. */
/** The markers are literal brackets — regex metacharacters. Escaped rather than
 * hand-written as `\[FIGURE\]` so the constants above stay the single source of
 * the strings this test is checking for. */
function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function elapsed(startedAt: number): string {
  const secs = Math.round((Date.now() - startedAt) / 1000);
  return `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
}

/** Sign in the way he does. The API is the real gate; `proxy.ts` bounces a
 * cookie-less browser to /{locale}/login?next=… and the form sends it back. */
async function signIn(page: Page): Promise<void> {
  await page.goto("/el/library");
  // With AUTH_ENABLED=1 this redirect always happens on a cold context; if the
  // stack is running with auth off we are already on the Library and there is
  // no door to open.
  if (page.url().includes("/login")) {
    await page.getByTestId("login-password").fill(PASSWORD);
    await page.getByTestId("login-submit").click();
  }
  await expect(page.getByTestId("library-heading")).toBeVisible();
  await expect(page).toHaveURL(/\/el\/library$/);
}

/** The row for our book, found the way a person finds it — by its title on the
 * shelf, not by an id we got from an API call we were not supposed to make. */
function bookRow(page: Page): Locator {
  return page.locator('[data-testid^="source-"]').filter({ hasText: BOOK_TITLE });
}

/** The source id, read back off the row the UI rendered. Needed only to address
 * that row's OWN testids (`reocr-{id}`, `ocr-progress-{id}`, …), which are id-
 * scoped by design so two books being read at once can't be confused for each
 * other. */
async function sourceIdOf(row: Locator): Promise<string> {
  const testId = await row.getAttribute("data-testid");
  expect(testId, "the source row must carry a data-testid").toBeTruthy();
  return testId!.replace(/^source-/, "");
}

/** Leave the shelf as we found it, THROUGH THE UI. Makes the test re-runnable
 * without a psql prompt — and re-running it is the normal case, because the
 * interesting failures here take 40 minutes to reach. Scoped to an exact title
 * match on purpose: this deletes a real book off a real library, and a fuzzy
 * match here would eventually delete the wrong one. */
async function removeExistingCopy(page: Page): Promise<void> {
  const row = bookRow(page);
  if ((await row.count()) === 0) return;

  const id = await sourceIdOf(row.first());
  test.info().annotations.push({
    type: "cleanup",
    description: `a previous copy of "${BOOK_TITLE}" (${id}) was on the shelf; removing it first`,
  });
  await page.getByTestId(`delete-${id}`).click();
  await page.getByTestId("confirm-accept").click();
  await expect(bookRow(page)).toHaveCount(0, { timeout: 60_000 });
}

test.describe("ingesting a real book, the way the tutor does", () => {
  test.setTimeout(READ_TIMEOUT_MS + 10 * 60 * 1000);

  test("Powers' 57 pages: upload, press read, watch it, and get the tab pages back", async ({ page }) => {
    const startedAt = Date.now();
    const log = (msg: string) => console.log(`[${elapsed(startedAt)}] ${msg}`);

    await signIn(page);
    await removeExistingCopy(page);

    // --- 1. Add the book -----------------------------------------------------
    // Exactly the four gestures the dialog asks for: open, choose PDF, name it,
    // pick the file. `setInputFiles` IS the file picker — it is the only part of
    // a browser Playwright cannot click, and it drives the same <input type=file>
    // the tutor's own file dialog would fill.
    log("opening the add-source dialog");
    await page.getByTestId("add-source-trigger").click();
    await expect(page.getByTestId("add-source-dialog")).toBeVisible();
    await page.getByTestId("add-source-mode-pdf").click();
    await page.getByTestId("add-source-title").fill(BOOK_TITLE);
    await page.getByTestId("add-source-file").setInputFiles(path.resolve(BOOK_PATH));

    log("uploading — the API paginates all 57 pages in the request, so this is not instant");
    await page.getByTestId("add-source-submit").click();

    // The upload response only lands once `ingest_source` has rendered and routed
    // every page (`routers/knowledge.py`: "call `ingest_source` in the same
    // request/session before responding"). That is free and fast-ish, but it is
    // 57 page scans — not a 5s wait.
    await expect(page.getByTestId("add-source-dialog")).toBeHidden({ timeout: 5 * 60 * 1000 });
    const row = bookRow(page);
    await expect(row, "the book must appear on the shelf after upload").toHaveCount(1, { timeout: 60_000 });
    const id = await sourceIdOf(row);
    log(`the book is on the shelf as ${id}`);

    // --- 2. Uploading MUST NOT have started reading it -----------------------
    // THE ASSERTION THIS WHOLE FILE IS THE ONLY PLACE TO MAKE. `ocr_active` is a
    // server fact (an in-flight GenerationJob), so a mocked test can only assert
    // that the row renders a stub; it can never assert that no 8-hour job began.
    // Here, nothing has been mocked — if the upload path ever re-acquires an
    // unprompted `startOcr` call, this line goes red.
    await expect(
      page.getByTestId(`ocr-progress-${id}`),
      "uploading a PDF must NOT start an 8-hour Claude run — reading is a decision, on a button",
    ).toHaveCount(0);
    // …and the way it must be started is visible and pressable, right there.
    await expect(
      page.getByTestId(`reocr-${id}`),
      "the tutor must be able to start the read from the UI, without anyone's help",
    ).toBeVisible();
    log("confirmed: the upload did not start reading; the read button is there");

    // --- 3. Press read, and read the price -----------------------------------
    await page.getByTestId(`reocr-${id}`).click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    // The dialog must say what it costs BEFORE it costs it. Asserted on the
    // number of pages and the word "ώρες" (hours) because "several minutes" is
    // what this copy used to say, and it was off by two orders of magnitude.
    await expect(page.getByTestId("confirm-body")).toContainText(String(PAGE_COUNT));
    await expect(page.getByTestId("confirm-body")).toContainText(/ώρες/);
    log("the confirm dialog quotes the page count and says it takes hours — accepting");
    await page.getByTestId("confirm-accept").click();

    // --- 4. Watch it. For forty minutes. -------------------------------------
    const progress = page.getByTestId(`ocr-progress-${id}`);
    await expect(progress, "the read must report progress in the UI, not silently churn").toBeVisible({
      timeout: 60_000,
    });
    await expect(progress).toContainText(new RegExp(`Ανάγνωση σελίδας \\d+ από ${PAGE_COUNT}`));
    log(`progress is live: "${(await progress.textContent())?.trim()}"`);

    // A heartbeat, so a 40-minute run is legible while it happens rather than
    // only in its verdict. This is also the reload-safety property under test:
    // progress is a SERVER fact (`GET .../progress`), which is why this poll can
    // read it from a tab that did not press the button.
    const seen = new Set<string>();
    const heartbeat = setInterval(() => {
      progress
        .textContent()
        .then((text) => {
          const line = text?.trim();
          if (line && !seen.has(line)) {
            seen.add(line);
            log(line);
          }
        })
        .catch(() => {
          /* the row is gone/re-rendering — the waits below are the authority */
        });
    }, 30_000);

    try {
      // The job is over when the server stops saying it is active — the row's
      // progress line detaches on the tick after `active: false`. Waiting on the
      // line to GO (rather than on a status to arrive) is what makes this robust
      // to the terminal state being green or amber; which one it actually is gets
      // asserted immediately below, on purpose, rather than tolerated here.
      await expect(progress, `reading ${PAGE_COUNT} pages should finish inside ${READ_TIMEOUT_MS / 60000} minutes`)
        .toHaveCount(0, { timeout: READ_TIMEOUT_MS });
    } finally {
      clearInterval(heartbeat);
    }
    log("the read has finished");

    // --- 5. It must be READY. Not partial, not empty. ------------------------
    // `status-ok-{id}` is rendered for `ready` ONLY: `source-row.tsx` gives
    // `partial` its own amber testid and a retry, and treats ready-with-0-chars
    // as broken. So this one locator is the whole "the book is genuinely
    // readable" claim, and it cannot be satisfied by a green-looking lie.
    const ok = page.getByTestId(`status-ok-${id}`);
    await expect(ok, "every page must have been read — a partial book fails this test").toBeVisible({
      timeout: 60_000,
    });
    await expect(ok).toContainText(`${PAGE_COUNT} σελίδες`);
    log(`final row status: "${(await ok.textContent())?.trim()}"`);

    // --- 6. Open it and read what came back ----------------------------------
    await row.getByRole("link").first().click();
    await expect(page).toHaveURL(new RegExp(`/el/library/${id}`));
    await expect(page.getByTestId("reader-scroll")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("page-indicator")).toContainText(`από ${PAGE_COUNT}`);

    // WAIT FOR THE WHOLE BOOK TO LAND BEFORE COUNTING ANYTHING. The Reader
    // fetches all 57 pages' text up front, six at a time, and renders a
    // `page-skeleton-{n}` wherever a page's detail has not arrived yet. Counting
    // [FIGURE] pages while those are still streaming in reads a number that
    // depends on network timing — the count would be whatever had loaded at that
    // instant, and the assertion below would pass or fail by luck. No skeletons
    // left == every page's text is in the DOM == the count is a fact.
    await expect(
      page.locator('[data-testid^="page-skeleton-"]'),
      "every page's text should load — a stuck skeleton means the Reader never got that page",
    ).toHaveCount(0, { timeout: 3 * 60 * 1000 });

    // A tab page, found by the marker the pipeline promises to write.
    const described = page.locator('[data-testid^="page-text-"]').filter({ hasText: FIGURE_MARKER });
    await expect(
      described.first(),
      "Powers is 43 pages of tab images — at least one page must carry a [FIGURE] description",
    ).toBeVisible();

    const describedCount = await described.count();
    log(`${describedCount} of ${PAGE_COUNT} pages carry a ${FIGURE_MARKER} block`);
    // 43 pages carry raster tab images. Asserting the exact number would make this
    // test a hostage to one page's picture being judged not worth describing (an
    // empty description is a legitimate answer — `merge()` keeps the publisher
    // text and the page stays ready). A clear majority is the honest claim: the
    // book's exercises were looked at, not skipped.
    expect(describedCount, "the tab pages must have been described, not skipped").toBeGreaterThanOrEqual(30);

    // --- 7. THE MERGE: kept AND added, on the same page -----------------------
    const text = (await described.first().textContent()) ?? "";
    // The contract as `book_text()` applies it, re-implemented here on purpose:
    // this asserts what a READER downstream can recover from the DOM, so it must
    // not borrow the producer's own parser to do it.
    const region = new RegExp(
      `${escapeRe(FIGURE_MARKER)}([\\s\\S]*?)${escapeRe(FIGURE_END)}`,
    );
    const match = text.match(region);
    // A BOUNDED description, not merely a begun one. An opener with no closer is
    // the whole of CRITICAL 1: a reader cannot tell where our description of the
    // tab stops and Powers' own words resume, so it quotes ours as his.
    expect(
      match,
      "the description must be CLOSED — an unterminated [FIGURE] is a description that gets cited as the author's words",
    ).not.toBeNull();
    const figure = match?.[1] ?? "";
    const publisher = text.replace(region, "\n").trim();

    // The publisher's caption is still there. If this is empty, the model
    // OVERWROTE the book — the exact failure `_publisher_text`/`merge` exist to
    // prevent, and the one that would be invisible from a page count alone.
    expect(
      publisher.length,
      "the publisher's own text must SURVIVE the read — a description must be added, never substituted",
    ).toBeGreaterThan(0);
    // …and the model genuinely described the tab, rather than shrugging.
    expect(
      figure.trim().length,
      "the [FIGURE] block must actually describe the tab",
    ).toBeGreaterThan(40);
    // …and nothing of OURS is sitting in what a reader would quote as his.
    expect(
      publisher.includes(figure.trim().slice(0, 40)),
      "our description of the tab must not survive into the page's own words",
    ).toBe(false);

    const pageTestId = await described.first().getAttribute("data-testid");
    log(`--- ${pageTestId} as the Reader renders it -------------------------`);
    log(text.trim());
    log("-----------------------------------------------------------------");

    // Attached rather than only logged: this is the artifact a human actually
    // reviews to decide whether the read was any good — a line count and a
    // green tick cannot answer "did it understand the tab".
    await test.info().attach("tab-page-as-rendered.txt", {
      body: `${pageTestId}\n\n${text.trim()}\n`,
      contentType: "text/plain",
    });
  });
});
