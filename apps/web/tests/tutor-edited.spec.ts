import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Task 1.5 — the lesson ROW stops being a badge shelf and becomes one door.
//
// Three things are asserted here, and all three are about what the tutor sees
// on a row he has already finished reading:
//
//  1. ONE AI BUTTON. «AI στο μάθημα» replaces the row's accumulated AI
//     affordances. It is visible on a ready lesson; this spec does not open
//     anything, because in this task the click only pokes a scope context that
//     nothing consumes yet (the panel lands in Task 2.5).
//  2. THE ROW STOPS SHOUTING. A `ready` lesson shows NO status pill — "ready"
//     is the resting state and a pill for it is noise — and the word count
//     leaves the row entirely for a sentence inside the opened lesson, where
//     the number is actually being read against its target.
//  3. A HAND-EDITED SECTION SAYS SO. The API stamps `meta.tutor_edited` with
//     the body it replaced; the segment renders a chip that opens the same
//     «Τι άλλαξε;» diff the AI rewrites use, so "did I change this, or did the
//     AI?" has an answer that outlives the session.
//
// Same route-interception convention as every other cockpit spec next door
// (`curricula-resume.spec.ts`, `revise-async.spec.ts`): anchored to
// `API_ORIGIN` rather than a bare suffix glob, explicit CORS headers plus
// OPTIONS handling because these are cross-origin credentialed calls even
// under interception, and an "unexpected request -> 500" catch-all so a
// routing mistake fails loudly instead of quietly hanging.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

/** A Greek course -> module -> one READY lesson -> one segment the tutor has
 * edited by hand. Greek on purpose: `el` is the product's default locale and
 * the strings this task adds are read there first. */
function tree(rootId: string, moduleId: string, lessonId: string, segmentId: string) {
  return {
    id: rootId, kind: "course", title: "Ήχος", body: null, est_minutes: null,
    order: 0, language: "el", plane: "content", student_id: null, meta: {},
    children: [{
      id: moduleId, kind: "module", title: "Ξύλα", body: null, est_minutes: null,
      order: 0, language: "el", plane: "content", student_id: null, meta: {},
      children: [{
        id: lessonId, kind: "lesson", title: "Μπράτσο", body: "Περίληψη",
        est_minutes: 50, order: 0, language: "el", plane: "content", student_id: null,
        meta: { draft_status: "ready", word_count: 14, target_words: 2750, meets_floor: false },
        children: [{
          id: segmentId, kind: "segment", title: "Θεωρία",
          body: "Ο σφένδαμος είναι πολύ σκληρός.", est_minutes: 12, order: 0,
          language: "el", plane: "content", student_id: null,
          meta: {
            section: "theory",
            tutor_edited: {
              at: "2026-09-11T15:36:59Z",
              prev_body: "Ο σφένδαμος είναι σκληρός.",
              count: 1,
            },
          },
          children: [],
        }],
      }],
    }],
  };
}

function progress(rootId: string) {
  return {
    root_id: rootId, total: 1, queued: 0, drafting: 0, ready: 1, failed: 0,
    done: true, draft_error: null,
  };
}

async function mockCurriculaApi(
  page: Page, rootId: string, moduleId: string, lessonId: string, segmentId: string, sessionId: string,
) {
  const unexpected: string[] = [];

  await page.route(`${API_ORIGIN}/curricula/**`, async (route: Route) => {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (body: unknown) =>
      route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(body),
      });

    if (pathname === `/curricula/${rootId}/chat-session` && method === "GET") return json({ session_id: sessionId });
    if (pathname === `/curricula/${rootId}/progress` && method === "GET") return json(progress(rootId));
    if (pathname === `/curricula/${rootId}` && method === "GET") {
      return json(tree(rootId, moduleId, lessonId, segmentId));
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });

  return { unexpected };
}

test.describe("lesson row — one AI button, live word count, tutor-edited chip (mocked API)", () => {
  test("the lesson row shows one AI button, and a hand-edited section shows its chip and diff", async ({
    page,
  }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonId = randomUUID();
    const segmentId = randomUUID();
    const sessionId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonId, segmentId, sessionId);

    await page.goto(`/el/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // Only the root opens expanded, so the module has to be opened before its
    // lesson row exists at all.
    await page
      .locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle")
      .click();

    const row = page.getByTestId("block-card-header").filter({ hasText: "Μπράτσο" }).first();
    await expect(row.getByTestId("lesson-ai")).toBeVisible();
    // The two badges that used to live here are GONE from the row.
    await expect(row.getByTestId("lesson-word-count")).toHaveCount(0);
    await expect(row.getByTestId("lesson-status")).toHaveCount(0); // ready -> no pill

    await row.getByTestId("block-card-toggle").click();

    // The count moved INTO the lesson, beside its target, where it is a
    // sentence rather than a badge.
    const words = page.getByTestId("lesson-words-line");
    await expect(words).toContainText("14");
    await expect(words).toContainText("2.750"); // Greek grouping separator

    await page.getByTestId("segment-tutor-edited").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("πολύ σκληρός");

    expect(api.unexpected).toEqual([]);
  });
});
