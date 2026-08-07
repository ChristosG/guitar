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

test.describe("a whole lesson rewritten by revise", () => {
  /** A lesson carrying `prev_segments` — the shape `modify_lesson` leaves
   * behind — with its live segments already regenerated. */
  async function mockRewrittenLesson(page: Page) {
    const restored: string[] = [];

    async function handler(route: Route) {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      const json = (status: number, body: unknown) =>
        route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

      if (pathname === `/blocks/${LESSON_ID}/restore-segments` && req.method() === "POST") {
        restored.push(pathname);
        await json(200, node({ id: LESSON_ID, kind: "lesson", title: "Μάθημα 1", meta: {}, children: [] }));
        return;
      }
      if (pathname === `/curricula/${ROOT_ID}`) {
        await json(200, node({
          id: ROOT_ID, kind: "course", title: "Ήχος Κιθάρας", meta: { brief: null },
          children: [node({
            id: MODULE_ID, kind: "module", title: "Ενότητα 1", body: "Στόχος.", meta: { tier: "library" },
            children: [node({
              id: LESSON_ID, kind: "lesson", title: "Μάθημα 1",
              meta: {
                draft_status: "ready",
                revise_instruction: "κάν' το πιο αναλυτικό",
                prev_segments: [
                  { title: "Ζέσταμα", body: "Ξεκίνα με ανοιχτές χορδές.", section: "warmup" },
                ],
              },
              children: [
                node({ id: SEGMENT_ID, kind: "segment", title: "Ζέσταμα",
                       body: "Ξεκίνα με ανοιχτές χορδές. Άκου προσεκτικά τον ενισχυτή.",
                       meta: { section: "warmup" } }),
              ],
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
      await json(500, { detail: "unexpected" });
    }

    await page.route(`${API_ORIGIN}/curricula/**`, handler);
    await page.route(`${API_ORIGIN}/curricula`, handler);
    await page.route(`${API_ORIGIN}/blocks/**`, handler);
    return { restored };
  }

  test("the lesson gets its own panel, and it can restore", async ({ page }) => {
    const mock = await mockRewrittenLesson(page);

    await page.goto(`/el/curricula/${ROOT_ID}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();
    await page.locator('[data-testid="block-card"][data-kind="module"]').getByTestId("block-card-toggle").click();
    // The chip lives in the card BODY, like every other per-block control, so
    // the lesson has to be open — you look at what changed while looking at the
    // lesson, not from the table of contents.
    await page.locator('[data-testid="block-card"][data-kind="lesson"]').getByTestId("block-card-toggle").click();

    await page.getByTestId("lesson-what-changed-trigger").click();
    await expect(page.getByTestId("what-changed-dialog")).toBeVisible();
    await expect(page.getByTestId("what-changed-instruction")).toContainText("πιο αναλυτικό");
    await expect(page.getByTestId("diff-add").first()).toContainText("ενισχυτή");

    // The restore is confirm-gated, and the copy PROMISES the current version
    // is kept — the server keeps that promise by stashing it.
    await page.getByTestId("what-changed-restore").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await expect(page.getByTestId("confirm-dialog")).toContainText("ΚΡΑΤΙΕΤΑΙ");
    await page.getByTestId("confirm-accept").click();

    await expect.poll(() => mock.restored.length).toBe(1);
  });

  test("a lesson with no snapshot offers nothing", async ({ page }) => {
    await mockTree(page, { section: "warmup" }, AFTER);
    await page.goto(`/el/curricula/${ROOT_ID}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();
    await page.locator('[data-testid="block-card"][data-kind="module"]').getByTestId("block-card-toggle").click();
    await page.locator('[data-testid="block-card"][data-kind="lesson"]').getByTestId("block-card-toggle").click();
    await expect(page.getByTestId("lesson-what-changed-trigger")).toHaveCount(0);
  });
});

test.describe("restructure one module with AI", () => {
  /** The board plus a chat session, so the revise drawer can open. */
  async function mockBoard(page: Page) {
    async function handler(route: Route) {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      const json = (status: number, body: unknown) =>
        route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

      if (pathname === `/curricula/${ROOT_ID}/chat-session`) {
        await json(200, { session_id: "77777777-7777-7777-7777-777777777777" });
        return;
      }
      if (pathname.startsWith("/chat/")) {
        await json(200, { messages: [] });
        return;
      }
      if (pathname === `/curricula/${ROOT_ID}`) {
        await json(200, node({
          id: ROOT_ID, kind: "course", title: "Ήχος Κιθάρας", meta: { brief: null },
          children: [node({
            id: MODULE_ID, kind: "module", title: "Από το πετάλι στον ενισχυτή",
            body: "Στόχος.", meta: { tier: "library" },
            children: [node({ id: LESSON_ID, kind: "lesson", title: "Μάθημα 1",
                              meta: { draft_status: "ready" }, children: [] })],
          })],
        }));
        return;
      }
      if (pathname.endsWith("/progress")) {
        await json(200, { root_id: ROOT_ID, total: 1, queued: 0, drafting: 0, ready: 1, failed: 0, done: true });
        return;
      }
      await json(200, null);
    }
    await page.route(`${API_ORIGIN}/**`, handler);
  }

  test("a module's ⋯ opens the drawer scoped, with the id spelled into the message", async ({ page }) => {
    await mockBoard(page);
    await page.goto(`/el/curricula/${ROOT_ID}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    const moduleCard = page.locator('[data-testid="block-card"][data-kind="module"]');
    await moduleCard.getByTestId("block-card-menu").first().click();
    await page.getByTestId("menu-restructure-ai").click();

    // The chip says which module — a scoped planner that looks unscoped is a trap.
    await expect(page.getByTestId("revise-scope-chip")).toContainText("Από το πετάλι στον ενισχυτή");

    // THE ID IS IN THE COMPOSER, and that is what makes scoping possible at all:
    // this drawer drives a CHAT, so the planner is reached through a tool call,
    // and the model can only pass an id it can actually see.
    const composer = page.getByTestId("chat-input");
    await expect(composer).toHaveValue(new RegExp(MODULE_ID));
    await expect(composer).toHaveValue(/Από το πετάλι/);
  });

  test("the drawer's own button is the whole-course door and clears any scope", async ({ page }) => {
    await mockBoard(page);
    await page.goto(`/el/curricula/${ROOT_ID}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    await page.locator('[data-testid="block-card"][data-kind="module"]').getByTestId("block-card-menu").first().click();
    await page.getByTestId("menu-restructure-ai").click();
    await expect(page.getByTestId("revise-scope-chip")).toBeVisible();

    await page.getByTestId("revise-close").click();
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("revise-scope-chip")).toHaveCount(0);
  });
});
