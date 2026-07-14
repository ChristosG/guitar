import { test, expect, type Locator, type Page, type Route } from "@playwright/test";

// Stage 7.1 — every destructive action in the cockpit is guarded.
//
// Chris, verbatim: "on delete buttons, i need a confirmation dialog BRO! wtf
// will happen if i click it accidentally?" Before this spec existed, the app
// had ZERO confirm dialogs: one un-guarded click on a curriculum root's Trash
// icon cascaded the whole generated tree (`DELETE /blocks/{id}` →
// `delete-orphan` + `ON DELETE CASCADE`), which is hours of paid generation.
//
// The assertion that matters is NOT "a dialog appeared" — it is that the
// MUTATION NEVER LEFT THE BROWSER until the tutor accepted it. Every test here
// therefore watches the wire (`page.route` records each DELETE/merge) and
// checks three states per site: opened → nothing sent; cancelled → still
// nothing sent; accepted → exactly one request. A dialog that appears while
// the DELETE fires behind it would pass a naive "is the dialog visible" test
// and lose the tutor's book anyway.
//
// Route/origin conventions (anchored patterns, CORS headers, an
// unexpected-request → 500 catch-all) are `cockpit.spec.ts`'s — see its
// docstring for why a bare suffix glob is a trap in this app.
const API_ORIGIN = "http://localhost:8791";

// `Access-Control-Allow-Origin` must echo the app's real origin (and pair
// with `Allow-Credentials`) rather than "*": since the auth slice made every
// call in `lib/api.ts` `credentials: "include"`, a browser REJECTS a
// wildcard-ACAO response outright — the page then renders its "could not
// load" error and every assertion below it fails for a reason that has
// nothing to do with what the test is checking.
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

const NOW = "2026-07-01T00:00:00Z";

interface FixtureBlock {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  est_minutes: number | null;
  order: number;
  language: string;
  plane: string;
  student_id: string | null;
  children: FixtureBlock[];
}

function block(id: string, kind: string, title: string, children: FixtureBlock[] = [], order = 0): FixtureBlock {
  return {
    id,
    kind,
    title,
    body: null,
    est_minutes: kind === "session" ? 30 : null,
    order,
    language: "en",
    plane: "content",
    student_id: null,
    children,
  };
}

const CURRICULUM_ID = "course-1";

/** A realistic two-module curriculum: the root's confirm dialog must be able
 * to say "2 modules, 3 lessons, 5 blocks in all" — the whole point of the
 * copy requirement (a generic "Are you sure?" tells the tutor nothing about
 * what a cascade is about to take). */
function makeCurriculum(): FixtureBlock {
  return block(CURRICULUM_ID, "course", "Tone Shaping Fundamentals", [
    block("module-1", "module", "Gain staging", [
      block("lesson-1a", "lesson", "Clean headroom"),
      block("lesson-1b", "lesson", "Breakup and sag"),
    ]),
    block("module-2", "module", "Pedals", [block("lesson-2a", "lesson", "Overdrive vs distortion")], 1),
  ]);
}

const LESSON_ID = "lesson-x";

function makeLesson(): FixtureBlock {
  return block(LESSON_ID, "lesson", "Pick Gauge and Tone", [
    block("session-1", "session", "Warm-up", [block("item-1a", "item", "Fret placement basics")]),
    block("session-2", "session", "Deep dive", [block("item-2a", "item", "Pick gauge comparison")], 1),
  ]);
}

function removeBlock(node: FixtureBlock, id: string): FixtureBlock {
  return { ...node, children: node.children.filter((c) => c.id !== id).map((c) => removeBlock(c, id)) };
}

const NOTE = {
  id: "note-1",
  title: "Practice reminder",
  body: "Alternate picking, 10 minutes daily.",
  tags: [] as string[],
  student_id: null as string | null,
  promoted_to_knowledge: false,
  created_at: NOW,
  updated_at: NOW,
};

const STUDENT = {
  id: "student-1",
  name: "Maria Ioannou",
  birthdate: null,
  level: "intermediate",
  instrument: "guitar",
  preferred_language: "el",
  status: "active",
  created_at: NOW,
  updated_at: NOW,
};

const ARTIFACT = {
  id: "artifact-1",
  kind: "signal_chain",
  spec: { nodes: [{ label: "Guitar" }, { label: "Amp" }] },
  title: "Blues rig",
  tags: [] as string[],
  source: "ai",
  block_id: null as string | null,
  created_at: NOW,
  updated_at: NOW,
};

const SOURCE = {
  id: "source-1",
  title: "Getting Great Guitar Sounds",
  type: "pdf",
  status: "ready",
  domain: null as string | null,
  language: "en",
  char_count: 91000,
  error: null as string | null,
  created_at: NOW,
  collection_id: "collection-1",
};

const COLLECTION = { id: "collection-1", name: "Books", source_count: 1 };

/** One handler for every API path any guarded page touches. `mutations`
 * records ONLY the state-changing calls (`DELETE`, the merge `POST`) — the
 * GETs each page fires on mount are noise for this spec's question, which is
 * exclusively "did a destructive request reach the wire, and when". */
async function mockApi(page: Page) {
  let curriculum = makeCurriculum();
  let lesson = makeLesson();
  const mutations: string[] = [];
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    // The shell probes auth + settings on EVERY page (`GET /auth/me`, `GET
    // /settings` — both added by the auth/settings slices). Answered here so
    // they never land in `unexpected` and never 500 the nav out from under a
    // page this spec is trying to drive.
    if (method === "GET" && pathname === "/auth/me") {
      return json({ authenticated: true, auth_enabled: false });
    }
    if (method === "GET" && pathname === "/settings") {
      return json({ provider: "anthropic", model: "claude-sonnet-5", configured: true, key_hint: "ab12" });
    }

    if (method === "GET") {
      if (pathname === "/notes") return json([NOTE]);
      if (pathname === "/students") return json([STUDENT]);
      if (pathname === "/artifacts") return json([ARTIFACT]);
      if (pathname === "/knowledge/sources") return json([SOURCE]);
      if (pathname === "/library/collections") return json([COLLECTION]);
      if (pathname === "/curricula")
        return json([{ id: CURRICULUM_ID, title: curriculum.title, language: "en", target_profile: null, created_at: NOW }]);
      if (pathname === `/curricula/${CURRICULUM_ID}`) return json(curriculum);
      if (pathname === "/lessons") return json([{ id: LESSON_ID, title: lesson.title, created_at: NOW, provenance: null }]);
      if (pathname === `/lessons/${LESSON_ID}`) return json(lesson);
    }

    if (method === "DELETE") {
      const blockMatch = pathname.match(/^\/blocks\/([^/]+)$/);
      if (blockMatch) {
        mutations.push(`DELETE ${pathname}`);
        curriculum = removeBlock(curriculum, blockMatch[1]);
        lesson = removeBlock(lesson, blockMatch[1]);
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (
        /^\/notes\/[^/]+$/.test(pathname) ||
        /^\/students\/[^/]+$/.test(pathname) ||
        /^\/artifacts\/[^/]+$/.test(pathname) ||
        /^\/knowledge\/sources\/[^/]+$/.test(pathname) ||
        /^\/library\/collections\/[^/]+$/.test(pathname)
      ) {
        mutations.push(`DELETE ${pathname}`);
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
    }

    if (method === "POST" && pathname === `/lessons/${LESSON_ID}/sessions/merge`) {
      mutations.push(`POST ${pathname}`);
      lesson = removeBlock(lesson, "session-2");
      return json(lesson);
    }

    unexpected.push(`${method} ${pathname}`);
    return json({ detail: "unmocked request in test" }, 500);
  }

  await page.route((url) => url.origin === API_ORIGIN, handler);
  return { mutations, unexpected };
}

/** The three states every guarded action must satisfy, asserted against the
 * WIRE and not against the UI's own optimism. */
async function expectGuarded(
  page: Page,
  mock: { mutations: string[] },
  trigger: Locator,
  expectedRequest: string,
  expectedCopy: RegExp,
) {
  const dialog = page.getByTestId("confirm-dialog");

  // 1. Click the destructive control: the dialog opens and NOTHING is sent.
  await trigger.click();
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId("confirm-body")).toContainText(expectedCopy);
  expect(mock.mutations).toEqual([]);

  // 2. Cancel: still nothing sent — the settle-on-close path (Escape, backdrop,
  //    Cancel) must resolve `false`, never leak a request, and never hang the
  //    caller's `await` (a hung promise would leave the row spinning forever).
  await page.getByTestId("confirm-cancel").click();
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(150); // a racing request would have landed by now
  expect(mock.mutations).toEqual([]);

  // 3. Re-open and accept: exactly one request, and it is the right one.
  await trigger.click();
  await expect(dialog).toBeVisible();
  await page.getByTestId("confirm-accept").click();
  await expect.poll(() => mock.mutations).toEqual([expectedRequest]);
}

test.describe("destructive actions are confirm-guarded (mocked API)", () => {
  test("note delete", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/notes");
    const row = page.getByTestId("note-item").filter({ hasText: NOTE.title });
    await expect(row).toBeVisible();

    await expectGuarded(page, mock, row.getByTestId("note-delete"), `DELETE /notes/${NOTE.id}`, /permanently deleted/i);
    expect(mock.unexpected).toEqual([]);
  });

  test("student delete names the progress/assignments/logs it cascades", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/students");
    const card = page.getByTestId("student-item").filter({ hasText: STUDENT.name });
    await expect(card).toBeVisible();

    await expectGuarded(
      page,
      mock,
      card.getByTestId("student-delete"),
      `DELETE /students/${STUDENT.id}`,
      /progress.*assignments.*lesson logs/i,
    );
    expect(mock.unexpected).toEqual([]);
  });

  test("artifact delete", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/artifacts");
    const item = page.getByTestId("artifact-item").filter({ hasText: ARTIFACT.title });
    await expect(item).toBeVisible();

    await expectGuarded(
      page,
      mock,
      item.getByTestId("artifact-delete"),
      `DELETE /artifacts/${ARTIFACT.id}`,
      /permanently deleted/i,
    );
    expect(mock.unexpected).toEqual([]);
  });

  test("library source delete quotes the indexed text that goes with it", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/library");
    await expect(page.getByTestId(`source-${SOURCE.id}`)).toBeVisible();

    await expectGuarded(
      page,
      mock,
      page.getByTestId(`delete-${SOURCE.id}`),
      `DELETE /knowledge/sources/${SOURCE.id}`,
      /91,000 characters of indexed text/i,
    );
    expect(mock.unexpected).toEqual([]);
  });

  test("collection delete promises the sources survive (SET NULL, not cascade)", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/library");
    const trigger = page.getByTestId(`delete-collection-${COLLECTION.id}`);
    await expect(trigger).toBeVisible();

    await expectGuarded(
      page,
      mock,
      trigger,
      `DELETE /library/collections/${COLLECTION.id}`,
      /becomes Unfiled — it is NOT deleted/i,
    );
    expect(mock.unexpected).toEqual([]);
  });

  test("curriculum ROOT delete counts the modules and lessons it cascades", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/curricula");
    await page.getByTestId("template-item").click();
    await expect(page.getByTestId("tree-board")).toBeVisible();

    const root = page.locator('[data-testid="block-card"][data-kind="course"]').first();
    await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);

    await root.getByTestId("block-card-delete").first().click();
    await expect(page.getByTestId("confirm-title")).toContainText("Delete the entire curriculum");
    // The numbers ARE the requirement: the tutor must see what a cascade costs.
    await expect(page.getByTestId("confirm-body")).toContainText("2 modules");
    await expect(page.getByTestId("confirm-body")).toContainText("3 lessons");
    await expect(page.getByTestId("confirm-body")).toContainText("5 blocks in all");
    expect(mock.mutations).toEqual([]);

    await page.getByTestId("confirm-cancel").click();
    await page.waitForTimeout(150);
    expect(mock.mutations).toEqual([]);
    await expect(page.getByTestId("tree-board")).toBeVisible(); // nothing was destroyed

    await root.getByTestId("block-card-delete").first().click();
    await page.getByTestId("confirm-accept").click();
    await expect.poll(() => mock.mutations).toEqual([`DELETE /blocks/${CURRICULUM_ID}`]);
    expect(mock.unexpected).toEqual([]);
  });

  test("curriculum child (module) delete", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/curricula");
    await page.getByTestId("template-item").click();

    const moduleCard = page.locator('[data-testid="block-card"][data-kind="module"]').first();
    await expect(moduleCard).toBeVisible();

    await expectGuarded(
      page,
      mock,
      moduleCard.getByTestId("block-card-delete").first(),
      "DELETE /blocks/module-1",
      /Gain staging/,
    );
    expect(mock.unexpected).toEqual([]);
  });

  test("lesson session delete", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto(`/en/lessons/${LESSON_ID}`);
    const session = page.locator('[data-session-id="session-1"]');
    await expect(session).toBeVisible();

    await expectGuarded(page, mock, session.getByTestId("session-delete"), "DELETE /blocks/session-1", /its 1 item/i);
    expect(mock.unexpected).toEqual([]);
  });

  test("lesson item delete", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto(`/en/lessons/${LESSON_ID}`);
    const item = page.locator('[data-item-id="item-1a"]');
    await expect(item).toBeVisible();

    await expectGuarded(page, mock, item.getByTestId("item-delete"), "DELETE /blocks/item-1a", /permanently deleted/i);
    expect(mock.unexpected).toEqual([]);
  });

  test("session merge-down is guarded too — it destroys the next session", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto(`/en/lessons/${LESSON_ID}`);
    const session = page.locator('[data-session-id="session-1"]');
    await expect(session).toBeVisible();

    await expectGuarded(
      page,
      mock,
      session.getByTestId("session-merge-down"),
      `POST /lessons/${LESSON_ID}/sessions/merge`,
      /stops existing/i,
    );
    expect(mock.unexpected).toEqual([]);
  });

  test("Escape closes the dialog without sending anything", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/en/notes");
    await page.getByTestId("note-delete").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
    await page.waitForTimeout(150);
    expect(mock.mutations).toEqual([]);

    // And the row is not left stuck in a "deleting" spinner — i.e. the promise
    // really settled rather than hanging on the un-accepted dialog.
    await expect(page.getByTestId("note-delete")).toBeEnabled();
  });

  test("the dialog is Greek in the Greek locale (the default one)", async ({ page }) => {
    const mock = await mockApi(page);
    await page.goto("/el/notes");
    await page.getByTestId("note-delete").click();

    await expect(page.getByTestId("confirm-title")).toContainText("Διαγραφή");
    await expect(page.getByTestId("confirm-body")).toContainText("δεν αναιρείται");
    await expect(page.getByTestId("confirm-cancel")).toHaveText("Άκυρο");
    expect(mock.mutations).toEqual([]);
    expect(mock.unexpected).toEqual([]);
  });
});
