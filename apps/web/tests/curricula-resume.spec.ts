import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the "Resume" button on the curriculum
// detail board's `DraftProgressBar` — the state-reactivity bug reported by
// the tutor: a curriculum with a `failed` lesson sits fully parked (nothing
// queued, nothing drafting, so the poll loop has already stopped ticking).
// Clicking Resume fires `POST /curricula/{root}/draft` (202 accepted), and
// the board must reflect the requeue and keep the progress bar moving WITH
// NO RELOAD — the app ships bundled (Tauri) where a manual reload is not a
// fallback a tutor can reach for.
//
// Same route-interception convention every other cockpit spec in this
// directory uses (`curricula-revise.spec.ts`, `cockpit.spec.ts`): anchored to
// `API_ORIGIN` (never a bare suffix glob), explicit CORS headers + OPTIONS
// handling, and an "unexpected request -> 500" catch-all so a routing
// mistake fails loudly.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
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
  meta: Record<string, unknown> | null;
  children: FixtureBlock[];
}

/** A course -> one module -> two lessons: one already `ready`, one `failed` —
 * exactly the state a curriculum with a stalled draft is left in, and the
 * board is fully PARKED on it (nothing `queued`, nothing `drafting`, so
 * `draft-progress-bar.tsx`'s own poll loop has already stopped scheduling
 * itself).
 *
 * `resume()` mimics the real API's contract for `POST /curricula/{root}/draft`
 * (`resumeCurriculumDraft`'s docstring in `lib/api.ts`): the requeue itself
 * (`failed` -> `queued`) is a DB write inside the SAME request that returns
 * 202, so the tree/progress this mock reports right after that call already
 * show `queued` — no artificial network delay is needed to reproduce the
 * bug. The bug was never a timing race between the response and the DB
 * write; it was that nothing on the client ever asked again.
 *
 * `advanceTo*` are called by the TEST ITSELF to mimic the background fan-out
 * actually picking the lesson up (`queued` -> `drafting` -> `ready`) — one
 * step per call, deterministic rather than poll-counted (Next's dev-mode
 * StrictMode double-invokes effects, so a raw poll count is not a clock; see
 * `interview.spec.ts`'s own `finished` flag for the same reason). */
function mockCurriculum(rootId: string, moduleId: string, readyLessonId: string, otherLessonId: string) {
  let phase: "failed" | "queued" | "drafting" | "ready" = "failed";

  function otherLesson(): FixtureBlock {
    const base = {
      id: otherLessonId, kind: "lesson", title: "Amp Settings for Overdrive",
      est_minutes: 25, order: 1, language: "en", plane: "content", student_id: null,
      children: [] as FixtureBlock[],
    };
    if (phase === "failed") {
      return { ...base, body: null, meta: { draft_status: "failed", error: "The model timed out." } };
    }
    if (phase === "ready") {
      return { ...base, body: "Set the gain just below breakup.", meta: { draft_status: "ready", word_count: 1800, meets_floor: true } };
    }
    return { ...base, body: null, meta: { draft_status: phase } }; // queued | drafting
  }

  function tree(): FixtureBlock {
    return {
      id: rootId, kind: "course", title: "Tone Shaping Fundamentals", body: null, est_minutes: null,
      order: 0, language: "en", plane: "content", student_id: null,
      meta: { brief: "A usable live tone.", source_ids: null },
      children: [
        {
          id: moduleId, kind: "module", title: "Signal Chain Basics", body: "Understand the guitar signal path.",
          est_minutes: null, order: 0, language: "en", plane: "content", student_id: null,
          meta: { objective: "Understand the guitar signal path.", tier: "library" },
          children: [
            {
              id: readyLessonId, kind: "lesson", title: "Pickups and Tone", body: "Pickup types and tone shaping.",
              est_minutes: 30, order: 0, language: "en", plane: "content", student_id: null,
              meta: { draft_status: "ready", word_count: 2100, meets_floor: true }, children: [],
            },
            otherLesson(),
          ],
        },
      ],
    };
  }

  function progress() {
    const queued = phase === "queued" ? 1 : 0;
    const drafting = phase === "drafting" ? 1 : 0;
    const failed = phase === "failed" ? 1 : 0;
    const ready = 1 + (phase === "ready" ? 1 : 0);
    return { root_id: rootId, total: 2, queued, drafting, ready, failed, done: queued === 0 && drafting === 0 };
  }

  return {
    tree,
    progress,
    resume: () => { phase = "queued"; },
    advanceToDrafting: () => { phase = "drafting"; },
    advanceToReady: () => { phase = "ready"; },
  };
}

async function mockCurriculaApi(page: Page, fixture: ReturnType<typeof mockCurriculum>) {
  const calls = { get: 0, progress: 0, resume: 0 };
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const resumeMatch = pathname.match(/^\/curricula\/([^/]+)\/draft$/);
    if (resumeMatch && method === "POST") {
      calls.resume++;
      // The requeue happens INSIDE this handler, before the response — same
      // contract as the real API (see this file's docstring above).
      fixture.resume();
      await route.fulfill({
        status: 202, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ job_id: randomUUID(), status: "pending" }),
      });
      return;
    }

    const progressMatch = pathname.match(/^\/curricula\/([^/]+)\/progress$/);
    if (progressMatch && method === "GET") {
      calls.progress++;
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(fixture.progress()),
      });
      return;
    }

    const getMatch = pathname.match(/^\/curricula\/([^/]+)$/);
    if (getMatch && method === "GET") {
      calls.get++;
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(fixture.tree()),
      });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  return { calls, unexpected };
}

test.describe("draft progress bar — resume (mocked API)", () => {
  test("resume flips a failed lesson to queued/drafting/ready and the bar advances, with NO reload", async ({
    page,
  }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const readyLessonId = randomUUID();
    const otherLessonId = randomUUID();

    const fixture = mockCurriculum(rootId, moduleId, readyLessonId, otherLessonId);
    const curricula = await mockCurriculaApi(page, fixture);

    await page.goto(`/en/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // Fully parked: one lesson failed, nothing queued or drafting.
    await expect(page.getByTestId("draft-progress")).toHaveAttribute("data-done", "true");
    await expect(page.getByTestId("draft-progress-failed")).toBeVisible();
    await expect(page.getByTestId("draft-resume")).toBeVisible();

    await page
      .locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle")
      .click();
    const otherLesson = page
      .locator('[data-testid="block-card"][data-kind="lesson"]')
      .filter({ hasText: "Amp Settings for Overdrive" });
    await expect(otherLesson.getByTestId("lesson-status")).toHaveAttribute("data-status", "failed");

    await page.getByTestId("draft-resume").click();
    expect(curricula.calls.resume).toBe(1);

    // NO RELOAD anywhere in this test — the board must reflect the requeue,
    // and the bar must keep polling, entirely on its own.
    await expect(otherLesson.getByTestId("lesson-status")).toHaveAttribute("data-status", "queued", {
      timeout: 5_000,
    });
    await expect(page.getByTestId("draft-progress")).toHaveAttribute("data-done", "false");
    await expect(page.getByTestId("draft-progress-failed")).toHaveCount(0);

    // The background fan-out picks it up — the poll loop must still be
    // ticking (not parked) to ever notice this.
    fixture.advanceToDrafting();
    await expect(otherLesson.getByTestId("lesson-status")).toHaveAttribute("data-status", "drafting", {
      timeout: 5_000,
    });
    // NO TOGGLE TOUCHED, AND THE LINE IS THERE ANYWAY. A lesson being written
    // is news, and news does not hide behind «Λεπτομέρειες» — nor does the
    // Resume button that sits beside it.
    await expect(page.getByTestId("draft-progress-states")).toContainText("1 writing");

    // ...and it finishes, without a reload.
    fixture.advanceToReady();
    // SETTLED, SO THE WHOLE CARD GOES QUIET. "2 of 2" is a status report while
    // something is still being written and plain arithmetic once nothing is —
    // and with the tallies behind «Λεπτομέρειες» the card had nothing left in
    // it but a 100% bar and an empty row. Nothing failed and there is nothing
    // to resume, so it removes itself.
    await expect(page.getByTestId("draft-progress")).toHaveCount(0, { timeout: 5_000 });
    await expect(page.getByTestId("draft-progress-count")).toHaveCount(0);
    await expect(page.getByTestId("draft-progress-states")).toHaveCount(0);
    // «Λεπτομέρειες» brings the whole card back, `data-done` and all — the poll
    // never stopped, the card was only hidden.
    await page.getByTestId("board-details-toggle").click();
    await expect(page.getByTestId("draft-progress")).toHaveAttribute("data-done", "true");
    await expect(page.getByTestId("draft-progress-count")).toContainText("2 of 2");
    await expect(page.getByTestId("draft-progress-states")).toBeVisible();
    // The pill DISAPPEARS on `ready` (Task 1.5: ready is the resting state, and
    // a badge on every settled row is noise) — its absence is the assertion,
    // and the enabled AI button is the positive half of the same claim.
    await expect(otherLesson.getByTestId("lesson-status")).toHaveCount(0);
    await expect(otherLesson.getByTestId("lesson-ai")).toBeEnabled();

    expect(curricula.unexpected).toEqual([]);
  });
});
