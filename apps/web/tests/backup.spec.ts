import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * BACKUP / RESTORE in Settings (workstream A3, `backup-card.tsx` +
 * `app/routers/backup.py`). What this spec pins:
 *
 *  - the card renders on /el/settings, with the EXPORT action as a plain
 *    ANCHOR whose href is the API's `/backup/export` (the browser/webview
 *    download path, not a fetch — a backup is hundreds of MB of scans);
 *  - the RESTORE flow is confirm-gated ("replaces ALL data" copy), fires the
 *    multipart POST only on accept, and RELOADS the app on success (every
 *    screen is stale by definition after a restore);
 *  - a server failure `code` renders as exactly one Greek sentence from
 *    `backup.errors.*` — never the raw code, never a status number.
 *
 * The API origin is mocked wholesale, same convention as
 * `blueprint-settings.spec.ts`: `X-App-Locale` makes every call a non-simple
 * request, so OPTIONS needs an answer or the real call never fires.
 */

const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

interface Call {
  method: string;
  pathname: string;
}

interface Opts {
  /** Force the restore POST's response — how a test sees a server rejection. */
  restore?: { status: number; json: unknown };
}

async function mockApi(page: Page, opts: Opts = {}): Promise<Call[]> {
  const calls: Call[] = [];

  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    calls.push({ method, pathname });

    const json = (b: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(b),
      });

    if (pathname === "/settings") {
      return json({
        provider: "anthropic",
        model: "claude-sonnet-5",
        configured: true,
        key_hint: "7f2a",
      });
    }
    if (pathname === "/prompts") return json([]);
    if (pathname === "/blueprint/default") {
      return json({ blueprint: { version: 1, sections: [] }, is_override: false });
    }
    if (pathname === "/backup/restore" && method === "POST") {
      if (opts.restore) return json(opts.restore.json, opts.restore.status);
      return json({ ok: true, restored_manifest: { pg_major: 16 } });
    }
    return json([]);
  });

  return calls;
}

/** Feed the hidden file input a small fake archive — `setInputFiles` works on
 * a `display: none` input, and the card's onChange runs the confirm gate. */
async function pickBackupFile(page: Page) {
  await page.getByTestId("backup-file-input").setInputFiles({
    name: "guitar-backup-2026-07-30.tar.gz",
    mimeType: "application/gzip",
    buffer: Buffer.from("fake tar.gz bytes"),
  });
}

// ---------------------------------------------------------------------------

test("the card renders in Settings and Export is an anchor at the API's export URL", async ({ page }) => {
  await mockApi(page);
  await page.goto("/el/settings");

  const card = page.getByTestId("backup-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Αντίγραφα ασφαλείας");

  const exportLink = card.getByTestId("backup-export");
  await expect(exportLink).toBeVisible();
  await expect(exportLink).toHaveAttribute("href", `${API_ORIGIN}/backup/export`);
  // an <a>, not a button-with-fetch — the browser download path is the point
  expect(await exportLink.evaluate((el) => el.tagName)).toBe("A");
});

test("restore: picking a file opens the destructive confirm, and accept fires the POST and reloads", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/el/settings");
  await expect(page.getByTestId("backup-card")).toBeVisible();

  // a marker that only survives until the page reloads
  await page.evaluate(() => {
    (window as unknown as { __not_reloaded?: number }).__not_reloaded = 1;
  });

  await pickBackupFile(page);

  const dialog = page.getByTestId("confirm-dialog");
  await expect(dialog).toBeVisible();
  // the copy says it replaces EVERYTHING, and names the chosen file
  await expect(page.getByTestId("confirm-body")).toContainText("ΟΛΑ");
  await expect(page.getByTestId("confirm-body")).toContainText("guitar-backup-2026-07-30.tar.gz");
  expect(calls.some((c) => c.pathname === "/backup/restore")).toBe(false); // not yet

  // Arm the navigation watch BEFORE the click. Polling `page.evaluate` across
  // the reload is what made this test flaky: an evaluate that lands mid-reload
  // dies with "Execution context was destroyed" instead of returning a value.
  // Waiting for the navigation asserts the same thing — that a reload happened
  // — without ever reaching into a context that is being torn down.
  const reloaded = page.waitForEvent("framenavigated", (f) => f === page.mainFrame());

  await page.getByTestId("confirm-accept").click();

  await expect
    .poll(() => calls.filter((c) => c.method === "POST" && c.pathname === "/backup/restore").length)
    .toBe(1);

  // the success path reloads the whole app
  await reloaded;
  await expect(page.getByTestId("backup-card")).toBeVisible();
  // and the marker did not survive it — one evaluate, on a settled context
  expect(
    await page.evaluate(() => (window as unknown as { __not_reloaded?: number }).__not_reloaded),
  ).toBeUndefined();
});

test("restore, cancelled at the confirm, sends nothing at all", async ({ page }) => {
  const calls = await mockApi(page);
  await page.goto("/el/settings");
  await expect(page.getByTestId("backup-card")).toBeVisible();

  await pickBackupFile(page);
  await expect(page.getByTestId("confirm-dialog")).toBeVisible();
  await page.getByTestId("confirm-cancel").click();

  await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
  expect(calls.some((c) => c.pathname === "/backup/restore")).toBe(false);
});

test("a failed restore renders its code's Greek sentence — never the raw code or status", async ({ page }) => {
  await mockApi(page, {
    restore: { status: 409, json: { detail: { code: "jobs_running", message: "2 jobs running" } } },
  });
  await page.goto("/el/settings");
  await expect(page.getByTestId("backup-card")).toBeVisible();

  await pickBackupFile(page);
  await page.getByTestId("confirm-accept").click();

  const error = page.getByTestId("backup-error");
  await expect(error).toBeVisible();
  // the exact Greek sentence for jobs_running (backup.errors.jobs_running, el.json)
  await expect(error).toContainText("Η εφαρμογή γράφει κάτι αυτή τη στιγμή");
  await expect(error).not.toContainText("jobs_running");
  await expect(error).not.toContainText("409");

  // no reload happened — the restore button is idle again, ready for a retry
  await expect(page.getByTestId("backup-restore")).toBeEnabled();
});

test("an unknown future code falls back to the restore_failed sentence", async ({ page }) => {
  await mockApi(page, {
    restore: { status: 500, json: { detail: { code: "some_new_code", message: "?" } } },
  });
  await page.goto("/el/settings");
  await expect(page.getByTestId("backup-card")).toBeVisible();

  await pickBackupFile(page);
  await page.getByTestId("confirm-accept").click();

  const error = page.getByTestId("backup-error");
  await expect(error).toBeVisible();
  await expect(error).toContainText("Η επαναφορά δεν ολοκληρώθηκε");
  await expect(error).not.toContainText("some_new_code");
});

test("every backup.* string exists in BOTH Greek and English", () => {
  const read = (locale: string) =>
    JSON.parse(readFileSync(join(__dirname, `../src/messages/${locale}.json`), "utf8"));

  const flatten = (obj: Record<string, unknown>, prefix = ""): string[] =>
    Object.entries(obj).flatMap(([k, v]) =>
      v && typeof v === "object"
        ? flatten(v as Record<string, unknown>, `${prefix}${k}.`)
        : [`${prefix}${k}`],
    );

  const el = flatten(read("el").backup ?? {}).sort();
  const en = flatten(read("en").backup ?? {}).sort();
  expect(el.length).toBeGreaterThan(0);
  expect(el).toEqual(en);
});
