import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of "Create a copy" — the backup the tutor
// takes before letting the AI rewrite a curriculum.
//
// What is worth testing here is not that a POST fires. It is the two things
// that would make the feature quietly useless: the copy not APPEARING on the
// index (the list is what tells him it worked), and the detail page silently
// doing nothing (nothing on that page moves when a duplicate lands, so without
// the notice and the link he cannot tell the click registered).
//
// Same route-interception convention as `curricula-resume.spec.ts` and
// `curricula-revise.spec.ts`: anchored to `API_ORIGIN`, explicit CORS + OPTIONS,
// and an unexpected-request catch-all so a routing mistake fails loudly.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

const ROOT_ID = "11111111-1111-1111-1111-111111111111";
const COPY_ID = "22222222-2222-2222-2222-222222222222";

// A LONG Greek title, because that is the reported case: the tutor's real module
// and course names overflow their column, and the copy's derived name is longer
// still. Greek is the product; an English fixture would not exercise the same
// string at all.
const TITLE = "Από το πετάλι στον ενισχυτή: αλυσίδα σήματος και τελικός ήχος";
// The server derives this, in the COURSE'S language — the client never builds
// it, which is exactly why the fixture returns it rather than the test
// constructing it.
const COPY_TITLE = `${TITLE} (αντίγραφο)`;

function listItem(id: string, title: string, created_at: string) {
  return { id, title, language: "el", target_profile: { level: "beginner" }, created_at };
}

function courseTree(id: string, title: string) {
  return {
    id, kind: "course", title, body: null, est_minutes: null, order: 0,
    language: "el", plane: "content", student_id: null,
    meta: { brief: "Ένα μάθημα για τον ήχο." },
    children: [
      {
        id: randomUUID(), kind: "module", title: "Ενότητα 1", body: "Στόχος.",
        est_minutes: null, order: 0, language: "el", plane: "content",
        student_id: null, meta: { tier: "library" }, children: [],
      },
    ],
  };
}

/** The index starts with ONE curriculum; duplicating adds a second, newest
 * first — which is the real `GET /curricula` ordering (`created_at desc`) and
 * the whole reason the client does not navigate anywhere: the copy arrives at
 * the top on its own. */
async function mockApi(page: Page) {
  const calls = { list: 0, duplicate: 0, tree: 0 };
  const unexpected: string[] = [];
  let duplicated = false;

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const json = (status: number, body: unknown) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (pathname === "/curricula" && method === "GET") {
      calls.list++;
      const rows = duplicated
        ? [listItem(COPY_ID, COPY_TITLE, "2026-08-07T02:00:00Z"), listItem(ROOT_ID, TITLE, "2026-08-01T10:00:00Z")]
        : [listItem(ROOT_ID, TITLE, "2026-08-01T10:00:00Z")];
      await json(200, rows);
      return;
    }

    const dup = pathname.match(/^\/curricula\/([^/]+)\/duplicate$/);
    if (dup && method === "POST") {
      calls.duplicate++;
      // The copy exists by the time this responds — one transaction, same as
      // the real endpoint. So a refetch immediately after already sees it.
      duplicated = true;
      await json(201, listItem(COPY_ID, COPY_TITLE, "2026-08-07T02:00:00Z"));
      return;
    }

    const tree = pathname.match(/^\/curricula\/([^/]+)$/);
    if (tree && method === "GET") {
      calls.tree++;
      const id = tree[1];
      await json(200, courseTree(id, id === COPY_ID ? COPY_TITLE : TITLE));
      return;
    }

    if (pathname.endsWith("/progress") && method === "GET") {
      await json(200, { root_id: ROOT_ID, total: 0, queued: 0, drafting: 0, ready: 0, failed: 0, done: true });
      return;
    }
    if (pathname === "/curricula/interview/open" && method === "GET") {
      await json(200, null);
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await json(500, { detail: `unexpected: ${method} ${pathname}` });
  }

  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/curricula`, handler);
  return { calls, unexpected };
}

test.describe("duplicate a curriculum", () => {
  test("the copy appears on the index without taking him anywhere", async ({ page }) => {
    const mock = await mockApi(page);

    await page.goto("/el/curricula");
    await expect(page.getByTestId("template-item")).toHaveCount(1);

    await page.getByTestId("curriculum-actions-trigger").click();
    await page.getByTestId("curriculum-duplicate").click();

    // Two cards, the copy FIRST — `created_at desc` from the server, not a
    // client-side sort.
    await expect(page.getByTestId("template-item")).toHaveCount(2);
    await expect(page.getByTestId("template-title").first()).toHaveText(COPY_TITLE);
    await expect(page.getByTestId("template-title").nth(1)).toHaveText(TITLE);

    // STILL ON THE LIST. Being teleported into a copy made for safekeeping is
    // the wrong default, and this is the assertion that pins it.
    await expect(page).toHaveURL(/\/el\/curricula$/);

    expect(mock.calls.duplicate).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });

  test("a truncating title can still be read in full", async ({ page }) => {
    // The reported bug: «Από το πετάλι στον ενισχυτή: αλυσίδα σήματος και
    // τελικ…» with no way to see the rest. `title=` is the whole fix, and it is
    // the ATTRIBUTE that is asserted — a native tooltip has no DOM to look at.
    await mockApi(page);
    await page.goto("/el/curricula");
    await expect(page.getByTestId("template-title").first()).toHaveAttribute("title", TITLE);

    await page.getByTestId("template-title").first().click();
    await expect(page.getByTestId("curriculum-detail-title")).toHaveAttribute("title", TITLE);
    await expect(page.getByTestId("block-card-title").first()).toHaveAttribute("title", TITLE);
  });

  test("the detail page says a copy was made and offers a way into it", async ({ page }) => {
    const mock = await mockApi(page);

    await page.goto(`/el/curricula/${ROOT_ID}`);
    await expect(page.getByTestId("curriculum-detail-title")).toHaveText(TITLE);

    await page.getByTestId("curriculum-actions-trigger").click();
    await page.getByTestId("curriculum-duplicate").click();

    // Nothing else on this page moves when a duplicate lands, so these two ARE
    // the feedback. The notice carries the server's derived name.
    await expect(page.getByTestId("curriculum-duplicate-notice")).toContainText(COPY_TITLE);
    const link = page.getByTestId("curriculum-duplicate-link").getByRole("link");
    await expect(link).toBeVisible();

    // He stays on the original until he chooses otherwise.
    await expect(page).toHaveURL(new RegExp(`/el/curricula/${ROOT_ID}$`));

    await link.click();
    await expect(page).toHaveURL(new RegExp(`/el/curricula/${COPY_ID}$`));
    await expect(page.getByTestId("curriculum-detail-title")).toHaveText(COPY_TITLE);

    expect(mock.calls.duplicate).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });

  test("a failed duplicate says so instead of failing silently", async ({ page }) => {
    await page.route(`${API_ORIGIN}/curricula**`, async (route) => {
      const { pathname } = new URL(route.request().url());
      if (route.request().method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname.endsWith("/duplicate")) {
        await route.fulfill({
          status: 500, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ detail: "boom" }),
        });
        return;
      }
      if (pathname === "/curricula") {
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify([listItem(ROOT_ID, TITLE, "2026-08-01T10:00:00Z")]),
        });
        return;
      }
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS, body: "null",
      });
    });

    await page.goto("/el/curricula");
    await page.getByTestId("curriculum-actions-trigger").click();
    await page.getByTestId("curriculum-duplicate").click();

    await expect(page.getByTestId("curriculum-actions-error")).toBeVisible();
    // ...and no phantom card, which is what a hopeful optimistic update would
    // have left behind.
    await expect(page.getByTestId("template-item")).toHaveCount(1);
    await expect(page.getByTestId("curriculum-duplicate-notice")).toHaveCount(0);
  });
});
