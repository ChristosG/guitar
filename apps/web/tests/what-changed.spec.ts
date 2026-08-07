import { test, expect, type Page, type Route } from "@playwright/test";

// The «Τι άλλαξε;» panel, in a browser, against a mocked tree.
//
// `prose-diff.spec.ts` already proves the ENGINE in Node. What can only be
// proved here is the wiring: that the chip appears exactly when there is a
// stashed previous body, that the two texts reaching the engine are the right
// two, and that a regenerated block gets the honest "this was rewritten"
// treatment instead of a screen of red and green.
//
// Same route-interception convention as `curricula-duplicate.spec.ts`.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

const ROOT_ID = "33333333-3333-3333-3333-333333333333";
const MODULE_ID = "44444444-4444-4444-4444-444444444444";
const LESSON_ID = "55555555-5555-5555-5555-555555555555";
const SEGMENT_ID = "66666666-6666-6666-6666-666666666666";

// An EDITED segment: the model kept the paragraph and appended a sentence.
const BEFORE = "Ο ενισχυτής χρωματίζει τον ήχο της κιθάρας.";
const AFTER =
  "Ο ενισχυτής χρωματίζει τον ήχο της κιθάρας. Δοκίμασε χαμηλό gain για καθαρό τόνο.";

function node(over: Record<string, unknown>) {
  return {
    id: "x", kind: "segment", title: "τ", body: null, est_minutes: null, order: 0,
    language: "el", plane: "content", student_id: null, meta: null, children: [],
    ...over,
  };
}

async function mockTree(page: Page, segmentMeta: Record<string, unknown> | null, after: string) {
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const { pathname } = new URL(req.url());
    if (req.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (status: number, body: unknown) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (pathname === `/curricula/${ROOT_ID}`) {
      await json(200, node({
        id: ROOT_ID, kind: "course", title: "Ήχος Κιθάρας", meta: { brief: null },
        children: [node({
          id: MODULE_ID, kind: "module", title: "Ενότητα 1", body: "Στόχος.",
          meta: { tier: "library" },
          children: [node({
            id: LESSON_ID, kind: "lesson", title: "Μάθημα 1",
            meta: { draft_status: "ready", word_count: 300 },
            children: [node({ id: SEGMENT_ID, kind: "segment", title: "Ζέσταμα", body: after, meta: segmentMeta })],
          })],
        })],
      }));
      return;
    }
    if (pathname.endsWith("/progress")) {
      await json(200, { root_id: ROOT_ID, total: 1, queued: 0, drafting: 0, ready: 1, failed: 0, done: true });
      return;
    }
    if (pathname === "/curricula/interview/open") {
      await json(200, null);
      return;
    }
    unexpected.push(`${req.method()} ${pathname}`);
    await json(500, { detail: "unexpected" });
  }

  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/curricula`, handler);
  await page.route(`${API_ORIGIN}/blocks/**`, handler);
  return { unexpected };
}

/** Open the board and expand down to the segment card. */
async function openSegment(page: Page) {
  await page.goto(`/el/curricula/${ROOT_ID}`);
  await expect(page.getByTestId("tree-board")).toBeVisible();
  await page.locator('[data-testid="block-card"][data-kind="module"]').getByTestId("block-card-toggle").click();
  await page.locator('[data-testid="block-card"][data-kind="lesson"]').getByTestId("block-card-toggle").click();
  await expect(page.locator('[data-testid="block-card"][data-kind="segment"]')).toBeVisible();
}

test.describe("what changed", () => {
  test("no stashed previous body means no chip at all", async ({ page }) => {
    await mockTree(page, { section: "warmup" }, AFTER);
    await openSegment(page);
    await expect(page.getByTestId("what-changed-trigger")).toHaveCount(0);
    await expect(page.getByTestId("extend-undo")).toHaveCount(0);
  });

  test("an edited paragraph shows the added words and nothing else", async ({ page }) => {
    await mockTree(
      page,
      { section: "warmup", prev_body: BEFORE, refined: true, refine_instruction: "πρόσθεσε μια συμβουλή" },
      AFTER,
    );
    await openSegment(page);

    await page.getByTestId("what-changed-trigger").click();
    await expect(page.getByTestId("what-changed-dialog")).toBeVisible();

    // What he asked for, first.
    await expect(page.getByTestId("what-changed-instruction")).toContainText("πρόσθεσε μια συμβουλή");

    // The appended sentence is marked as an addition...
    await expect(page.getByTestId("what-changed-diff")).toBeVisible();
    await expect(page.getByTestId("diff-add").first()).toContainText("gain");
    // ...and this is NOT reported as a wholesale rewrite.
    await expect(page.getByTestId("what-changed-rewritten")).toHaveCount(0);
  });

  test("a regenerated paragraph says so instead of showing confetti", async ({ page }) => {
    // No shared phrasing at all — the case Chris was worried about.
    await mockTree(
      page,
      { section: "warmup", prev_body: "Ο ενισχυτής χρωματίζει τον ήχο με τον προενισχυτή του." },
      "Οι χορδές καθορίζουν το ύφος: πάχος, υλικό, ηλικία και περιέλιξη.",
    );
    await openSegment(page);

    await page.getByTestId("what-changed-trigger").click();
    await expect(page.getByTestId("what-changed-rewritten")).toBeVisible();
    // The whole point: no red/green word soup in this mode.
    await expect(page.getByTestId("what-changed-diff")).toHaveCount(0);
  });

  test("the chip and Undo appear together — two answers to one question", async ({ page }) => {
    await mockTree(page, { section: "warmup", prev_body: BEFORE, refined: true }, AFTER);
    await openSegment(page);
    await expect(page.getByTestId("what-changed-trigger")).toBeVisible();
    await expect(page.getByTestId("extend-undo")).toBeVisible();
  });
});
