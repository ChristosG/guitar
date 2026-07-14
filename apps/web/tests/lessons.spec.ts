import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the Lesson editor (Plan 10 Task 4).
// Same conventions as `cockpit.spec.ts`: routes are anchored to the API's
// own origin (`API_ORIGIN`), NOT a bare `**/lessons`-style suffix glob —
// this app's OWN page URLs (`/en/lessons`, `/en/lessons/{id}`) end in
// exactly the same suffix as the real API paths, so an unanchored glob
// would risk swallowing the page navigation itself instead of only the API
// call (see that file's own docstring on this exact hazard).
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

function item(id: string, title: string, order: number, body = "body"): FixtureBlock {
  return {
    id, kind: "item", title, body, est_minutes: null, order,
    language: "en", plane: "content", student_id: null, children: [],
  };
}

function session(id: string, title: string, order: number, estMinutes: number, children: FixtureBlock[]): FixtureBlock {
  return {
    id, kind: "session", title, body: null, est_minutes: estMinutes, order,
    language: "en", plane: "content", student_id: null, children,
  };
}

function lesson(id: string, title: string, children: FixtureBlock[]): FixtureBlock {
  return {
    id, kind: "lesson", title, body: null, est_minutes: null, order: 0,
    language: "en", plane: "content", student_id: null, children,
  };
}

function findBlock(node: FixtureBlock, id: string): FixtureBlock | null {
  if (node.id === id) return node;
  for (const child of node.children) {
    const found = findBlock(child, id);
    if (found) return found;
  }
  return null;
}

function removeBlock(node: FixtureBlock, id: string): FixtureBlock {
  return { ...node, children: node.children.filter((c) => c.id !== id).map((c) => removeBlock(c, id)) };
}

const LESSON_ID = "lesson-1";
const SOURCE_ID = "source-1";
const SOURCE_TITLE = "Getting Great Guitar Sounds";

function makeTree(): FixtureBlock {
  return lesson(LESSON_ID, "Pick Gauge and Tone", [
    session("session-1", "Warm-up", 0, 20, [
      item("item-1a", "Fret placement basics", 0),
      item("item-1b", "Open chord shapes", 1),
    ]),
    session("session-2", "Deep dive", 1, 30, [
      item("item-2a", "Pick gauge comparison", 0),
    ]),
  ]);
}

/** Mocks `/lessons`, `/lessons/**`, `/blocks/**` and `/knowledge/sources/**`
 * — everything the lesson editor + list pages call. `currentTree` is
 * mutated in place by PATCH/DELETE so a follow-up `GET /lessons/{id}`
 * (`SessionCard`/`ItemRow`'s `refreshTree`, see those files' own docstrings
 * on why rename/delete re-fetch rather than hand-splice the tree) reflects
 * the change — same "closure-held fixture state" convention as
 * `cockpit.spec.ts`'s `mockStudentsApi`.
 */
async function mockLessonsApi(
  page: Page,
  opts: {
    tree?: FixtureBlock;
    listItems?: Array<{ id: string; title: string; created_at: string; provenance: { source_id: string; page_no: number } | null }>;
    splitResponse?: FixtureBlock | { status: number; body: unknown };
    mergeResponse?: FixtureBlock | { status: number; body: unknown };
  } = {},
) {
  let currentTree = opts.tree ?? makeTree();
  const listItems = opts.listItems ?? [
    { id: LESSON_ID, title: currentTree.title, created_at: "2026-07-01T00:00:00Z", provenance: { source_id: SOURCE_ID, page_no: 21 } },
  ];
  const calls = { split: 0, merge: 0, addSession: 0, patch: [] as Array<{ id: string; body: unknown }>, delete: [] as string[] };
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/lessons" && method === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(listItems) });
      return;
    }

    if (pathname === `/lessons/${LESSON_ID}` && method === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(currentTree) });
      return;
    }

    const splitMatch = pathname.match(/^\/lessons\/([^/]+)\/sessions\/([^/]+)\/split$/);
    if (splitMatch && method === "POST") {
      calls.split++;
      const result = opts.splitResponse;
      if (result && "status" in result) {
        await route.fulfill({ status: result.status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(result.body) });
        return;
      }
      currentTree = result ?? currentTree;
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(currentTree) });
      return;
    }

    if (pathname === `/lessons/${LESSON_ID}/sessions/merge` && method === "POST") {
      calls.merge++;
      const result = opts.mergeResponse;
      if (result && "status" in result) {
        await route.fulfill({ status: result.status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(result.body) });
        return;
      }
      currentTree = result ?? currentTree;
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(currentTree) });
      return;
    }

    if (pathname === `/lessons/${LESSON_ID}/sessions` && method === "POST") {
      calls.addSession++;
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(currentTree) });
      return;
    }

    const blockMatch = pathname.match(/^\/blocks\/([^/]+)$/);
    if (blockMatch && method === "PATCH") {
      const id = blockMatch[1];
      const payload = req.postDataJSON();
      calls.patch.push({ id, body: payload });
      const block = findBlock(currentTree, id);
      if (block) Object.assign(block, payload);
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(block ?? {}) });
      return;
    }
    if (blockMatch && method === "DELETE") {
      const id = blockMatch[1];
      calls.delete.push(id);
      currentTree = removeBlock(currentTree, id);
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const sourceMatch = pathname.match(/^\/knowledge\/sources\/([^/]+)$/);
    if (sourceMatch && method === "GET") {
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: sourceMatch[1], title: SOURCE_TITLE, type: "pdf", status: "ready",
          domain: null, language: "en", char_count: 91000, error: null,
          created_at: "2026-01-01T00:00:00Z", collection_id: null, chunks: [],
        }),
      });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({ status: 500, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify({ detail: "unmocked request in test" }) });
  }

  await page.route(`${API_ORIGIN}/lessons`, handler);
  await page.route(`${API_ORIGIN}/lessons/**`, handler);
  await page.route(`${API_ORIGIN}/blocks/**`, handler);
  await page.route(`${API_ORIGIN}/knowledge/sources/**`, handler);

  return { calls, unexpected, getTree: () => currentTree };
}

test("the outline renders lesson -> sessions -> items in order", async ({ page }) => {
  await mockLessonsApi(page);
  await page.goto(`/en/lessons/${LESSON_ID}`);

  await expect(page.getByTestId("lesson-title")).toContainText("Pick Gauge and Tone");

  const sessionTitles = page.getByTestId("session-title");
  await expect(sessionTitles).toHaveText(["Warm-up", "Deep dive"]);

  const firstSessionItems = page.locator('[data-session-id="session-1"]').getByTestId("item-title");
  await expect(firstSessionItems).toHaveText(["Fret placement basics", "Open chord shapes"]);
});

test("the provenance chip links to the exact page in the Reader", async ({ page }) => {
  await mockLessonsApi(page);
  await page.goto(`/en/lessons/${LESSON_ID}`);

  const chip = page.getByTestId("provenance-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute("href", `/en/library/${SOURCE_ID}?page=21`);
  await expect(chip).toContainText(SOURCE_TITLE);
  await expect(chip).toContainText("21");
});

test("split calls the API and the outline re-renders with the new sessions", async ({ page }) => {
  const splitResult = lesson(LESSON_ID, "Pick Gauge and Tone", [
    session("session-1a", "Warm-up (1/2)", 0, 10, [item("item-1a", "Fret placement basics", 0)]),
    session("session-1b", "Warm-up (2/2)", 1, 10, [item("item-1b", "Open chord shapes", 0)]),
    session("session-2", "Deep dive", 2, 30, [item("item-2a", "Pick gauge comparison", 0)]),
  ]);
  const { calls } = await mockLessonsApi(page, { splitResponse: splitResult });
  await page.goto(`/en/lessons/${LESSON_ID}`);

  await expect(page.getByTestId("session-title")).toHaveCount(2);
  await page.locator('[data-session-id="session-1"]').getByTestId("session-split").click();
  await page.getByTestId("split-minutes-input").fill("10");
  await page.getByTestId("split-submit").click();

  await expect.poll(() => calls.split).toBe(1);
  await expect(page.getByTestId("session-title")).toHaveText(["Warm-up (1/2)", "Warm-up (2/2)", "Deep dive"]);
});

test("rename PATCHes /blocks/{id} and the outline reflects the new title", async ({ page }) => {
  const { calls } = await mockLessonsApi(page);
  await page.goto(`/en/lessons/${LESSON_ID}`);

  await page.locator('[data-session-id="session-1"]').getByTestId("session-title").click();
  const input = page.locator('[data-session-id="session-1"]').getByTestId("session-title-input");
  await input.fill("Renamed session");
  await input.press("Enter");

  await expect.poll(() => calls.patch.find((c) => c.id === "session-1")).toEqual({ id: "session-1", body: { title: "Renamed session" } });
  await expect(page.getByTestId("session-title")).toHaveText(["Renamed session", "Deep dive"]);
});

test("a 422 from an invalid merge is surfaced to the user, not swallowed", async ({ page }) => {
  await mockLessonsApi(page, {
    mergeResponse: { status: 422, body: { detail: "cannot merge non-adjacent sessions: order 0 and order 1 are not contiguous" } },
  });
  await page.goto(`/en/lessons/${LESSON_ID}`);

  await page.locator('[data-session-id="session-1"]').getByTestId("session-merge-down").click();
  // Merge is destructive (the next session ceases to exist and there is no
  // unmerge), so it is confirm-guarded like the deletes — accept, then assert
  // the API's 422 still surfaces.
  await page.getByTestId("confirm-accept").click();

  await expect(page.getByTestId("session-merge-error")).toBeVisible();
  await expect(page.getByTestId("session-merge-error")).toContainText(/not contiguous|cannot merge/i);
  // the outline must NOT have silently collapsed the two sessions
  await expect(page.getByTestId("session-title")).toHaveText(["Warm-up", "Deep dive"]);
});

test("the lessons list shows each lesson's title and where it came from", async ({ page }) => {
  await mockLessonsApi(page);
  await page.goto("/en/lessons");

  await expect(page.getByTestId("lesson-item")).toHaveCount(1);
  await expect(page.getByTestId("lesson-item")).toContainText("Pick Gauge and Tone");
  await expect(page.getByTestId("provenance-chip")).toHaveAttribute("href", `/en/library/${SOURCE_ID}?page=21`);
});
