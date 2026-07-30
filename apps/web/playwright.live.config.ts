import { defineConfig } from "@playwright/test";

/**
 * THE LIVE SUITE — Playwright against the REAL docker stack, not a mock.
 *
 * `playwright.config.ts` (testDir `./tests`) is the fast, hermetic suite: every
 * spec there stubs the API with `page.route`, boots its own `next dev` on 3100,
 * and finishes in seconds. That suite proves the components RENDER what the API
 * says. It cannot prove the API says anything true, because it never runs one.
 *
 * This config is the other half, and it exists because of a specific
 * instruction from the repo's owner about ingesting a book:
 *
 *   "make sure that you test one of the books via the FE, and not merely from
 *    the backend, so that we know our frontend will work, and we can do it
 *    ourselves from UI and not by telling you (claude) to do it."
 *
 * So: no `page.route`, no fixtures, no `webServer`. It drives the actual `web`
 * container on :8790, which talks to the actual `api` on :8791 (resolved in the
 * browser from `window.location` — see `lib/api.ts`), which spends the tutor's
 * actual Claude subscription through the bridge. If it passes, he can do it too.
 *
 * WHY A SEPARATE FILE rather than a second `project` in the main config:
 * `webServer` is a top-level key and applies to every project in a config, so a
 * live run there would still boot a `next dev` on 3100 that nothing uses. The
 * two suites also want opposite settings — this one runs for the better part of
 * an hour, must never retry (a retry re-reads the book on the subscription), and
 * must never parallelize (one OCR job per source is a server-side invariant).
 *
 * Run it with:  npm run test:e2e          (all live specs)
 *               npm run test:e2e -- e2e/ingest-a-book.spec.ts
 */
export default defineConfig({
  testDir: "./e2e",

  // No `webServer`. The stack is expected to be UP already:
  //   docker compose up -d   (web :8790, api :8791, postgres :5434)
  // Starting it here would mean this config could silently test a `next dev`
  // process instead of the image that actually ships, which is the one thing
  // this suite exists to avoid.
  use: {
    baseURL: process.env.WEB_BASE_URL ?? "http://localhost:8790",
    // A real ingest is worth a trace/screenshot when it goes wrong — 40 minutes
    // is far too long to reproduce a failure by re-running it.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },

  // Each live spec sets its own `test.setTimeout` — reading a 57-page book is
  // ~40 minutes of model time and nothing sensible can be a default here.
  timeout: 60 * 60 * 1000,
  expect: { timeout: 15_000 },

  // One at a time, never retried: see the docstring. Both of these are
  // correctness, not tuning.
  workers: 1,
  retries: 0,
  fullyParallel: false,
  reporter: "line",
});
