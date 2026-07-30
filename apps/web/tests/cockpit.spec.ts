import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the cockpit shell + Curricula pages:
// every `/curricula*`/`/blocks/*` call is
// intercepted via `page.route` and answered from an in-memory fixture —
// same convention as `library.spec.ts` (CORS headers + explicit OPTIONS
// handling, because these are cross-origin calls to NEXT_PUBLIC_API_BASE
// even under Playwright's route interception; an "unexpected request ->
// 500" catch-all so a routing mistake fails loudly instead of leaking to a
// real backend).
//
// Route patterns are anchored to the API's own origin (matching
// `lib/api.ts`'s `NEXT_PUBLIC_API_BASE` fallback), not a bare suffix glob:
// a suffix glob also matches this app's OWN same-origin
// page URLs (`http://localhost:3100/en/curricula` ends in "/curricula" just
// as much as the real `http://localhost:8791/curricula` does), which risks
// swallowing the page navigation itself instead of only the API call.
// `library.spec.ts` avoids this by coincidence (every `/knowledge/*`/
// `/library/*` API path it mocks has a segment after that prefix, which the
// bare `/en/library` page URL doesn't) — anchoring to the origin here
// removes the ambiguity outright rather than relying on that coincidence.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  // Must echo the app's real origin + `Allow-Credentials`, not "*": every call
  // in `lib/api.ts` is `credentials: "include"` (the auth slice), and a browser
  // rejects a wildcard-ACAO response to a credentialed request outright — the
  // page then renders its "could not load" state and the assertions below fail
  // for a reason that has nothing to do with what they check.
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
  children: FixtureBlock[];
}

/** A small course -> module -> 2 lessons tree, per the brief's "mock
 * returns a small course->module->lesson tree". */
function makeGeneratedTree(title: string, language: string): FixtureBlock {
  return {
    id: randomUUID(),
    kind: "course",
    title,
    body: null,
    est_minutes: null,
    order: 0,
    language,
    plane: "content",
    student_id: null,
    children: [
      {
        id: randomUUID(),
        kind: "module",
        title: "Signal Chain Basics",
        body: "Understand the guitar signal path.",
        est_minutes: null,
        order: 0,
        language,
        plane: "content",
        student_id: null,
        children: [
          {
            id: randomUUID(),
            kind: "lesson",
            title: "Pickups and Tone",
            body: "Explore pickup types and tone shaping.",
            est_minutes: 30,
            order: 0,
            language,
            plane: "content",
            student_id: null,
            children: [],
          },
          {
            id: randomUUID(),
            kind: "lesson",
            title: "Cables and Signal Integrity",
            body: "How cable quality affects tone.",
            est_minutes: 20,
            order: 1,
            language,
            plane: "content",
            student_id: null,
            children: [],
          },
        ],
      },
    ],
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

interface CurriculumListItemFixture {
  id: string;
  title: string;
  language: string;
  target_profile: Record<string, unknown> | null;
  created_at: string;
}

/** What the API says when the confirm step cannot materialize the tree. The
 * failure surface moved with the flow: there is no job to fail inside the dialog
 * any more, so the thing that can go wrong is the answer itself. */
const CONFIRM_FAILURE_MESSAGE = "The curriculum could not be created. Try again.";

/** The model's outline, as the "outline" step's `findings` — the thing the tutor
 * edits (and, here, accepts as-is). */
const OUTLINE = {
  title: "Tone Shaping Fundamentals",
  modules: [
    {
      title: "Signal Chain Basics",
      objective: "Understand the guitar signal path.",
      tier: "library",
      coverage_note: "Covered by your book, pp. 12-30.",
      lessons: [
        { title: "Pickups and Tone", objective: "Pickup types.", est_minutes: 60 },
        { title: "Cables and Signal Integrity", objective: "Cable quality.", est_minutes: 60 },
      ],
    },
  ],
};

/** Mocks `/curricula`, the guided interview v2 (`/curricula/interview...` — six
 * steps: who -> duration -> scope -> sources -> OUTLINE -> confirm),
 * `/curricula/{id}` + its progress poll, and `PATCH /blocks/{id}`.
 *
 * THERE IS NO JOB POLL HERE ANY MORE, and that is the point of Stage 6. The old
 * flow approved a curriculum and then held the tutor on a spinner inside the dialog
 * for the several minutes the whole thing took to generate. Now the "confirm"
 * answer MATERIALIZES the tree (course -> modules -> lessons, every lesson
 * `queued`) and returns a 202 carrying `root_id` as well as `job_id` — so the
 * dialog closes, the board opens on a real curriculum immediately, and the lessons
 * fill in underneath him while he reads module 1.
 *
 * `confirmFails: true` makes that final answer a 500 — the dialog must stay open,
 * say so, and leave the board and the template list untouched. */
async function mockCurriculaApi(
  page: Page,
  {
    templates = [] as CurriculumListItemFixture[],
    confirmFails = false,
  } = {},
) {
  const interviewId = randomUUID();
  let interviewStep = "who";
  let courseTitle = "";
  const courseLanguage = "en";
  let currentTree: FixtureBlock | null = null;
  const calls = {
    list: 0, interviewStart: 0, getInterview: 0, answer: 0, progress: 0, get: 0, patch: 0,
    curriculumPatch: 0,
  };
  const answerBodies: unknown[] = [];
  const lastBody: { patch?: unknown; curriculumPatch?: unknown } = {};
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    if (pathname === "/curricula/interview/open" && method === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: "null" });
      return;
    }
    if (pathname === "/curricula" && method === "GET") {
      calls.list++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(templates),
      });
      return;
    }
    if (pathname === "/curricula/interview" && method === "POST") {
      calls.interviewStart++;
      const payload = req.postDataJSON() as { title: string; domain?: string | null };
      courseTitle = payload.title;
      interviewStep = "who";
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          interview_id: interviewId,
          step: "who",
          question: "Who is this curriculum for?",
          // The API always offers "no particular student" as a first-class option,
          // and hands over the levels it will accept — the student is OPTIONAL.
          options: [{ value: "none", label: "No particular student", kind: "none" }],
          findings: { levels: ["all_levels", "beginner", "intermediate", "advanced"] },
          error: null,
        }),
      });
      return;
    }
    // `answerInterview`'s own error path re-syncs with a bare `GET .../interview/
    // {id}` (`interview-dialog.tsx`'s "best-effort re-sync" after a failed
    // answer) — mocked here so `confirmFails` exercises that path instead of
    // tripping the unexpected-request catch-all below.
    const getInterviewMatch = pathname.match(/^\/curricula\/interview\/([^/]+)$/);
    if (getInterviewMatch && method === "GET") {
      calls.getInterview++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          interview_id: interviewId,
          step: interviewStep,
          question: "",
          options: null,
          findings: null,
          error: null,
        }),
      });
      return;
    }
    const answerMatch = pathname.match(/^\/curricula\/interview\/([^/]+)\/answer$/);
    if (answerMatch && method === "POST") {
      calls.answer++;
      const payload = req.postDataJSON() as { answer: Record<string, unknown> };
      answerBodies.push(payload.answer);

      // The interview's SIX steps (Stage 6.8): who -> duration -> scope -> sources
      // -> outline -> confirm. "preview" is gone; the outline step replaced it, and
      // it is the one the tutor actually edits.
      const state = (over: Record<string, unknown>) => ({
        interview_id: interviewId, options: null, findings: null, error: null, ...over,
      });
      const send = (body: unknown, status = 200) =>
        route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

      if (interviewStep === "who") {
        interviewStep = "duration";
        await send(state({ step: "duration", question: "How long does this run?" }));
        return;
      }
      if (interviewStep === "duration") {
        interviewStep = "scope";
        await send(state({
          step: "scope",
          question: "What is this course FOR?",
          options: [{ value: "general_knowledge", label: "Fill the gaps from general knowledge." }],
        }));
        return;
      }
      if (interviewStep === "scope") {
        interviewStep = "sources";
        await send(state({ step: "sources", question: "Which sources?", options: [] }));
        return;
      }
      if (interviewStep === "sources") {
        interviewStep = "outline";
        await send(state({ step: "outline", question: "Here is the course.", findings: OUTLINE }));
        return;
      }
      if (interviewStep === "outline") {
        interviewStep = "confirm";
        await send(state({ step: "confirm", question: "Ready?", findings: payload.answer.outline }));
        return;
      }
      // confirm. The tree is MATERIALIZED here, so the 202 carries `root_id` and
      // the board opens on a real curriculum immediately (Stage 6.6/6.9).
      if (confirmFails) {
        await send({ detail: CONFIRM_FAILURE_MESSAGE }, 500);
        return;
      }
      currentTree = makeGeneratedTree(courseTitle, courseLanguage);
      templates.unshift({
        id: currentTree.id, title: currentTree.title, language: currentTree.language,
        target_profile: null, created_at: new Date().toISOString(),
      });
      await send({ job_id: randomUUID(), root_id: currentTree.id, status: "pending" }, 202);
      return;
    }

    const progressMatch = pathname.match(/^\/curricula\/([^/]+)\/progress$/);
    if (progressMatch && method === "GET") {
      calls.progress++;
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          root_id: progressMatch[1], total: 2, queued: 0, drafting: 0,
          ready: 2, failed: 0, done: true,
        }),
      });
      return;
    }

    const getMatch = pathname.match(/^\/curricula\/([^/]+)$/);
    if (getMatch && method === "GET") {
      calls.get++;
      const found = currentTree && findBlock(currentTree, getMatch[1]);
      await route.fulfill({
        status: found ? 200 : 404,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(found ?? { detail: "not found" }),
      });
      return;
    }
    // `PATCH /curricula/{id}` — the dedicated curriculum-root rename route
    // `CurriculumActionsMenu` (the detail page header's ⋯ menu) calls, as
    // opposed to the generic `PATCH /blocks/{id}` below. The board's own root
    // card has NO inline rename any more (review fix, block-card.tsx) — the
    // header menu is the one rename door for a course root — so this is the
    // route the "title edits PATCH" half of this test now exercises.
    if (getMatch && method === "PATCH") {
      calls.curriculumPatch++;
      const payload = req.postDataJSON();
      lastBody.curriculumPatch = payload;
      if (currentTree && currentTree.id === getMatch[1] && typeof payload.title === "string") {
        currentTree.title = payload.title;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          id: getMatch[1],
          title: payload.title,
          language: currentTree?.language ?? "en",
          target_profile: null,
          created_at: new Date().toISOString(),
        }),
      });
      return;
    }
    const patchMatch = pathname.match(/^\/blocks\/([^/]+)$/);
    if (patchMatch && method === "PATCH") {
      calls.patch++;
      const payload = req.postDataJSON();
      lastBody.patch = payload;
      const found = currentTree && findBlock(currentTree, patchMatch[1]);
      if (found && typeof payload.title === "string") found.title = payload.title;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(
          found ?? {
            id: patchMatch[1],
            kind: "course",
            title: payload.title,
            body: null,
            est_minutes: null,
            order: 0,
            language: "en",
            plane: "content",
            student_id: null,
            children: [],
          },
        ),
      });
      return;
    }
    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(`${API_ORIGIN}/curricula`, handler);
  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/blocks/**`, handler);

  return { calls, answerBodies, lastBody, unexpected };
}

test.describe("cockpit shell", () => {
  test("the persistent nav shows all 6 sections and links to /curricula, /library", async ({ page }) => {
    await page.goto("/en/curricula");

    await expect(page.getByTestId("app-title")).toHaveText(/Guitar Tutor Copilot/);

    // Today/Students/Notes are GONE from the desktop build — the nav is
    // curricula-first now, and their routes no longer exist.
    for (const section of ["curricula", "library", "canon", "lessons", "artifacts", "chat"]) {
      await expect(page.getByTestId(`nav-${section}`)).toBeVisible();
    }
    await expect(page.getByTestId("nav-today")).toHaveCount(0);
    await expect(page.getByTestId("nav-students")).toHaveCount(0);
    await expect(page.getByTestId("nav-notes")).toHaveCount(0);
    await expect(page.getByTestId("nav-curricula")).toHaveAttribute("href", "/en/curricula");
    await expect(page.getByTestId("nav-library")).toHaveAttribute("href", "/en/library");

    // Root locale redirects into the shell too — Curricula is the landing page.
    await page.goto("/en");
    await expect(page).toHaveURL(/\/en\/curricula$/);
    await expect(page.getByTestId("app-nav")).toBeVisible();
  });
});

test.describe("curricula (mocked API)", () => {
  test("the interview materializes the tree; the board opens on it at once on its own route, title edits PATCH, and the index navigates back into it", async ({
    page,
  }) => {
    const mock = await mockCurriculaApi(page, { templates: [] });

    await page.goto("/en/curricula");
    await expect(page).toHaveURL(/\/en\/curricula$/);
    await expect(page.getByTestId("templates-empty")).toBeVisible();

    await page.getByTestId("curricula-generate-button").click();
    await expect(page.getByTestId("interview-dialog")).toBeVisible();

    // Title only. `domain` is GONE — it was one line in one prompt and a retrieval
    // filter that could no longer filter anything; the "scope" step's free-text
    // brief is what it was pretending to be.
    await page.getByTestId("interview-title").fill("Tone Shaping Fundamentals");
    await page.getByTestId("interview-start-submit").click();

    // "who" — the student is OPTIONAL, so "no one in particular" + a level is a
    // complete answer.
    await page.getByTestId("interview-who-level-beginner").click();
    await page.getByTestId("interview-answer-submit").click();

    // "duration" — weeks x sessions/week x minutes. The shape is arithmetic now.
    await page.getByTestId("interview-duration-weeks").fill("6");
    await page.getByTestId("interview-duration-minutes").fill("60");
    await page.getByTestId("interview-answer-submit").click();

    // "scope" — what the course is FOR, in his own words.
    await page.getByTestId("interview-scope-brief").fill("A usable live tone.");
    await page.getByTestId("interview-answer-submit").click();

    // "sources" — accept the (empty, in this mock) default selection.
    await page.getByTestId("interview-answer-submit").click();

    // "outline" — the editor. Accept the model's course as-is here; editing it is
    // interview.spec.ts's whole subject.
    await expect(page.getByTestId("outline-editor")).toBeVisible();
    await page.getByTestId("interview-answer-submit").click();

    // "confirm" — and this is where the money goes.
    await page.getByTestId("interview-confirm-submit").click();

    // NO WAITING ROOM. The tree already exists; the dialog closes and this
    // NAVIGATES to the curriculum's own detail route (Unit A) — the board is on
    // it immediately, with the draft progress bar running.
    await expect(page.getByTestId("interview-dialog")).toBeHidden();
    await expect(page).toHaveURL(/\/en\/curricula\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // Only the root opens expanded by default ("everything is expanded and a
    // chaos" — block-card.tsx) — expand the module to reach its lessons.
    await page.locator('[data-testid="block-card"][data-kind="module"]').getByTestId("block-card-toggle").click();

    // course + module + 2 lessons. Nested BlockCards render INSIDE their parent's
    // DOM subtree (that is what makes the indentation work), so a descendant lookup
    // scoped to the course card would also match its children — count by kind
    // globally instead.
    await expect(page.getByTestId("block-card")).toHaveCount(4);
    await expect(page.locator('[data-testid="block-card"][data-kind="course"]')).toHaveCount(1);
    await expect(page.locator('[data-testid="block-card"][data-kind="module"]')).toHaveCount(1);
    await expect(page.locator('[data-testid="block-card"][data-kind="lesson"]')).toHaveCount(2);
    await expect(page.getByTestId("block-card-title").filter({ hasText: "Signal Chain Basics" })).toBeVisible();
    await expect(page.getByTestId("block-card-title").filter({ hasText: "Pickups and Tone" })).toBeVisible();
    await expect(page.getByTestId("block-card-title").filter({ hasText: "Cables and Signal Integrity" })).toBeVisible();

    expect(mock.calls.interviewStart).toBe(1);
    // Six answers: who, duration, scope, sources, outline, confirm.
    expect(mock.answerBodies).toHaveLength(6);
    expect(mock.answerBodies[0]).toEqual({ student_id: null, level: "beginner" });
    expect(mock.answerBodies[1]).toEqual({ weeks: 6, sessions_per_week: 1, minutes_per_session: 60 });
    expect(mock.answerBodies[5]).toEqual({ approved: true });

    // Rename the course (root) via the detail page HEADER's ⋯ menu
    // (`CurriculumActionsMenu`) -> `PATCH /curricula/{id}`, NOT the board's
    // own root card. The root card has no inline rename any more (review
    // fix, block-card.tsx): `updateBlock`/`PATCH /blocks/{id}` never reached
    // this page's own `title` state (a SEPARATE copy from the board's tree —
    // see the page's own docstring), so a card-level rename used to save
    // silently while the header beside it kept showing the stale name. The
    // header menu is the one rename door for a course root now.
    await page.getByTestId("curriculum-actions-trigger").click();
    await page.getByTestId("curriculum-rename").click();
    await expect(page.getByTestId("curriculum-rename-dialog")).toBeVisible();
    const titleInput = page.getByTestId("curriculum-rename-input");
    await titleInput.fill("Tone Shaping Fundamentals (Revised)");
    await page.getByTestId("curriculum-rename-save").click();

    // Fans out to BOTH the header AND the board's root card (`handleRenamed`
    // -> `setTitle` + a `refreshNonce`-driven `TreeBoard` remount — the page's
    // own docstring on why a plain `setTree` there isn't enough).
    await expect(page.getByTestId("curriculum-detail-title")).toHaveText("Tone Shaping Fundamentals (Revised)");
    await expect(
      page.getByTestId("block-card-title").filter({ hasText: "Tone Shaping Fundamentals (Revised)" }),
    ).toBeVisible();
    expect(mock.calls.curriculumPatch).toBe(1);
    expect(mock.lastBody.curriculumPatch).toEqual({ title: "Tone Shaping Fundamentals (Revised)" });
    // The root card's ⋯ menu keeps every OTHER action, it just lost rename —
    // confirm the affordance is really gone, not merely unused by this test.
    const courseCard = page.locator('[data-testid="block-card"][data-kind="course"]');
    await courseCard.getByTestId("block-card-menu").first().click();
    await expect(page.getByTestId("menu-rename")).toHaveCount(0);
    await expect(page.getByTestId("menu-delete")).toBeVisible();
    await page.keyboard.press("Escape");

    // --- Unit A: `curricula-back` returns to the now-navigable index, which
    // lists the curriculum just created, supports a title search filter, and
    // navigates back into the SAME curriculum from a card click.
    await page.getByTestId("curricula-back").click();
    await expect(page).toHaveURL(/\/en\/curricula$/);
    await expect(page.getByTestId("template-item")).toHaveCount(1);

    await page.getByTestId("curricula-search").fill("nonexistent curriculum");
    await expect(page.getByTestId("template-item")).toHaveCount(0);
    await expect(page.getByTestId("curricula-search-no-match")).toBeVisible();

    await page.getByTestId("curricula-search").fill("Tone Shaping");
    await expect(page.getByTestId("template-item")).toHaveCount(1);
    await expect(page.getByTestId("curricula-search-no-match")).toHaveCount(0);

    await page.getByTestId("template-item").click();
    await expect(page).toHaveURL(/\/en\/curricula\/[0-9a-f-]{36}$/);
    // The renamed title survived the round trip through the index and back —
    // this refetches the SAME curriculum, not a fresh empty one.
    await expect(
      page.getByTestId("block-card-title").filter({ hasText: "Tone Shaping Fundamentals (Revised)" }),
    ).toBeVisible();

    expect(mock.unexpected).toEqual([]);
  });

  test("a failed confirm keeps the dialog open and leaves the index untouched", async ({ page }) => {
    const mock = await mockCurriculaApi(page, { templates: [], confirmFails: true });

    await page.goto("/en/curricula");
    await page.getByTestId("curricula-generate-button").click();
    await page.getByTestId("interview-title").fill("Broken Curriculum");
    await page.getByTestId("interview-start-submit").click();

    await page.getByTestId("interview-answer-submit").click(); // who
    await page.getByTestId("interview-duration-weeks").fill("6");
    await page.getByTestId("interview-duration-minutes").fill("60");
    await page.getByTestId("interview-answer-submit").click(); // duration
    await page.getByTestId("interview-scope-brief").fill("Anything.");
    await page.getByTestId("interview-answer-submit").click(); // scope
    await page.getByTestId("interview-answer-submit").click(); // sources
    await page.getByTestId("interview-answer-submit").click(); // outline
    await page.getByTestId("interview-confirm-submit").click();

    // The error is SHOWN and the interview is still there — he does not lose the
    // outline he just paid 90K tokens for because the last call failed.
    await expect(page.getByTestId("interview-answer-error")).toHaveText(CONFIRM_FAILURE_MESSAGE);
    await expect(page.getByTestId("interview-dialog")).toBeVisible();
    await expect(page.getByTestId("interview-confirm-submit")).toBeEnabled();

    // Nothing was created: no navigation happened, and the index is untouched.
    await expect(page).toHaveURL(/\/en\/curricula$/);
    await expect(page.getByTestId("templates-empty")).toBeVisible();
    expect(mock.unexpected).toEqual([]);
  });
});
