import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the cockpit shell + Students +
// Curricula pages: every `/students/*` and `/curricula*`/`/blocks/*` call is
// intercepted via `page.route` and answered from an in-memory fixture —
// same convention as `library.spec.ts` (CORS headers + explicit OPTIONS
// handling, because these are cross-origin calls to NEXT_PUBLIC_API_BASE
// even under Playwright's route interception; an "unexpected request ->
// 500" catch-all so a routing mistake fails loudly instead of leaking to a
// real backend).
//
// Route patterns are anchored to the API's own origin (matching
// `lib/api.ts`'s `NEXT_PUBLIC_API_BASE` fallback), not a bare `**/students`-
// style suffix glob: a suffix glob also matches this app's OWN same-origin
// page URLs (`http://localhost:3100/en/students` ends in "/students" just
// as much as the real `http://localhost:8791/students` does), which risks
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

interface FixtureStudent {
  id: string;
  name: string;
  birthdate: string | null;
  level: string | null;
  instrument: string | null;
  preferred_language: string;
  status: string;
  created_at: string;
  updated_at: string;
}

function seedStudent(overrides: Partial<FixtureStudent> = {}): FixtureStudent {
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    name: "Nikos Papadopoulos",
    birthdate: null,
    level: "beginner",
    instrument: "guitar",
    preferred_language: "el",
    status: "active",
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

/** Mocks the two `/students` endpoints this app calls (list + create). */
async function mockStudentsApi(page: Page, initial: FixtureStudent[] = []) {
  const students = [...initial];
  const calls = { list: 0, create: 0 };
  const lastBody: { create?: unknown } = {};
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    if (pathname === "/students" && method === "GET") {
      calls.list++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(students),
      });
      return;
    }
    if (pathname === "/students" && method === "POST") {
      calls.create++;
      const payload = req.postDataJSON() as Partial<FixtureStudent>;
      lastBody.create = payload;
      const created = seedStudent({
        name: payload.name,
        level: payload.level ?? null,
        instrument: payload.instrument ?? null,
        preferred_language: payload.preferred_language ?? "el",
      });
      students.unshift(created);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
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

  await page.route(`${API_ORIGIN}/students`, handler);
  await page.route(`${API_ORIGIN}/students/**`, handler);

  return { students, calls, lastBody, unexpected };
}

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
  const calls = { list: 0, interviewStart: 0, getInterview: 0, answer: 0, progress: 0, get: 0, patch: 0 };
  const answerBodies: unknown[] = [];
  const lastBody: { patch?: unknown } = {};
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
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
  test("the persistent nav shows all 5 sections and links to /students, /library", async ({ page }) => {
    await page.goto("/en/today");

    await expect(page.getByTestId("app-title")).toHaveText(/Guitar Tutor Copilot/);
    await expect(page.getByTestId("today-heading")).toBeVisible();
    await expect(page.getByTestId("today-placeholder-card")).toHaveCount(2);

    for (const section of ["today", "students", "curricula", "library", "notes"]) {
      await expect(page.getByTestId(`nav-${section}`)).toBeVisible();
    }
    await expect(page.getByTestId("nav-students")).toHaveAttribute("href", "/en/students");
    await expect(page.getByTestId("nav-library")).toHaveAttribute("href", "/en/library");

    // Root locale redirects into the shell too.
    await page.goto("/en");
    await expect(page).toHaveURL(/\/en\/today$/);
    await expect(page.getByTestId("app-nav")).toBeVisible();
  });
});

test.describe("students (mocked API)", () => {
  test("adding a student posts to the API and the card appears", async ({ page }) => {
    const mock = await mockStudentsApi(page, []);

    await page.goto("/en/today");
    await page.getByTestId("nav-students").click();
    await expect(page).toHaveURL(/\/en\/students$/);
    await expect(page.getByTestId("students-empty")).toBeVisible();

    await page.getByTestId("students-add-button").click();
    await expect(page.getByTestId("add-student-dialog")).toBeVisible();

    await page.getByTestId("add-student-name").fill("Maria Ioannou");
    await page.getByTestId("add-student-level").fill("intermediate");
    await page.getByTestId("add-student-instrument").fill("guitar");
    await page.getByTestId("add-student-language").fill("el");
    await page.getByTestId("add-student-submit").click();

    await expect(page.getByTestId("add-student-dialog")).toBeHidden();

    const row = page.getByTestId("student-item").filter({ hasText: "Maria Ioannou" });
    await expect(row).toBeVisible();
    await expect(row.getByTestId("student-level")).toContainText("intermediate");
    await expect(row.getByTestId("student-instrument")).toContainText("guitar");

    expect(mock.calls.create).toBe(1);
    const body = mock.lastBody.create as {
      name: string;
      level?: string;
      instrument?: string;
      preferred_language?: string;
    };
    expect(body.name).toBe("Maria Ioannou");
    expect(body.level).toBe("intermediate");
    expect(body.preferred_language).toBe("el");
    expect(mock.unexpected).toEqual([]);
  });
});

test.describe("curricula (mocked API)", () => {
  test("the interview materializes the tree; the board opens on it at once on its own route, title edits PATCH, and the index navigates back into it", async ({
    page,
  }) => {
    const mock = await mockCurriculaApi(page, { templates: [] });

    await page.goto("/en/today");
    await page.getByTestId("nav-curricula").click();
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

    // Rename the course (root) card via its ⋯ menu -> PATCH /blocks/{id}.
    // ("RENAME IS AN ACTION, NOT A TITLE CLICK" — block-card.tsx's own
    // tutor-friendly-rewrite docstring: the title text itself isn't
    // clickable any more.)
    const courseCard = page.locator('[data-testid="block-card"][data-kind="course"]');
    // Descendant BlockCards nest inside the course card's own DOM subtree, so a
    // scoped lookup also matches every child row's menu button — `.first()` is
    // the course's own (it renders before any of its children in DOM order).
    await courseCard.getByTestId("block-card-menu").first().click();
    await page.getByTestId("menu-rename").click();
    const titleInput = page.getByTestId("block-card-title-input");
    await titleInput.fill("Tone Shaping Fundamentals (Revised)");
    await titleInput.press("Enter");

    await expect(
      page.getByTestId("block-card-title").filter({ hasText: "Tone Shaping Fundamentals (Revised)" }),
    ).toBeVisible();
    expect(mock.calls.patch).toBe(1);
    expect(mock.lastBody.patch).toEqual({ title: "Tone Shaping Fundamentals (Revised)" });

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

// --- Student detail (mocked API) --------------------------------------------
//
// Plan 6 Task 4: the student-detail page (assignments/progress/recent
// lessons + the assign-curriculum and log-a-lesson flows). Deliberately a
// NEW, self-contained mock (`mockStudentDetailApi` below), not a retrofit of
// `mockStudentsApi`/`mockCurriculaApi` above — this page's endpoint
// combination (detail/progress/lessons/assign/blocks, all at once) doesn't
// overlap either existing helper's scope, and extending them risked the
// already-green tests those helpers back. Same CORS/OPTIONS/unexpected-
// request-500 conventions as every mock above.

interface FixtureAssignment {
  assignment_id: string;
  curriculum_block_id: string;
  title: string;
}

interface FixtureProgress {
  id: string;
  student_id: string;
  block_id: string;
  status: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

function seedProgress(overrides: Partial<FixtureProgress> = {}): FixtureProgress {
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    student_id: "",
    block_id: randomUUID(),
    status: "practicing",
    notes: null,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

interface FixtureLessonLog {
  id: string;
  student_id: string;
  session_block_id: string;
  date: string | null;
  taught: boolean;
  notes: string | null;
  homework: string | null;
  created_at: string;
  updated_at: string;
}

function seedLessonLog(overrides: Partial<FixtureLessonLog> = {}): FixtureLessonLog {
  const now = new Date().toISOString();
  return {
    id: randomUUID(),
    student_id: "",
    session_block_id: randomUUID(),
    date: null,
    taught: false,
    notes: null,
    homework: null,
    created_at: now,
    updated_at: now,
    ...overrides,
  };
}

/** Mocks the whole student-detail page's API surface for one student:
 * `GET /students` (list, so the "click through from the roster" test can
 * find the row), `GET /students/{id}/detail` (stateful — reflects every
 * mutation below on its NEXT call), `POST /students/{id}/progress` (upsert
 * by block_id, same semantics as the real `upsert_progress`), `POST
 * /students/{id}/lessons` (always appends), `GET /curricula` (the assign
 * dialog's picker) + `POST /curricula/{root_id}/assign` (records a new
 * assignment + returns a trivial cloned tree), and `GET /blocks/{id}`
 * (resolves a bare block id to its title for `BlockTitle`). */
async function mockStudentDetailApi(
  page: Page,
  {
    student,
    assignments = [],
    progress = [],
    lessons = [],
    curricula = [],
    blocksById = {},
  }: {
    student: FixtureStudent;
    assignments?: FixtureAssignment[];
    progress?: FixtureProgress[];
    lessons?: FixtureLessonLog[];
    curricula?: CurriculumListItemFixture[];
    blocksById?: Record<string, { id: string; title: string }>;
  },
) {
  const state = { assignments: [...assignments], progress: [...progress], lessons: [...lessons] };
  const calls = { students: 0, detail: 0, progress: 0, lessons: 0, curricula: 0, assign: 0, block: 0 };
  const lastBody: { progress?: unknown; lessons?: unknown; assign?: unknown } = {};
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/students" && method === "GET") {
      calls.students++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify([student]),
      });
      return;
    }
    if (pathname === `/students/${student.id}/detail` && method === "GET") {
      calls.detail++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          student,
          assignments: state.assignments,
          progress: state.progress,
          recent_lessons: state.lessons,
        }),
      });
      return;
    }
    if (pathname === `/students/${student.id}/progress` && method === "POST") {
      calls.progress++;
      const payload = req.postDataJSON() as { block_id: string; status: string; notes?: string | null };
      lastBody.progress = payload;
      const now = new Date().toISOString();
      const existingIdx = state.progress.findIndex((p) => p.block_id === payload.block_id);
      if (existingIdx >= 0) {
        state.progress[existingIdx] = {
          ...state.progress[existingIdx],
          status: payload.status,
          notes: payload.notes ?? null,
          updated_at: now,
        };
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS_HEADERS,
          body: JSON.stringify(state.progress[existingIdx]),
        });
        return;
      }
      const created = seedProgress({
        student_id: student.id,
        block_id: payload.block_id,
        status: payload.status,
        notes: payload.notes ?? null,
      });
      state.progress.unshift(created);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
      });
      return;
    }
    if (pathname === `/students/${student.id}/lessons` && method === "POST") {
      calls.lessons++;
      const payload = req.postDataJSON() as {
        session_block_id: string;
        date?: string | null;
        taught?: boolean;
        notes?: string | null;
        homework?: string | null;
      };
      lastBody.lessons = payload;
      const created = seedLessonLog({
        student_id: student.id,
        session_block_id: payload.session_block_id,
        date: payload.date ?? null,
        taught: payload.taught ?? false,
        notes: payload.notes ?? null,
        homework: payload.homework ?? null,
      });
      state.lessons.unshift(created);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
      });
      return;
    }
    if (pathname === "/curricula" && method === "GET") {
      calls.curricula++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(curricula),
      });
      return;
    }
    const assignMatch = pathname.match(/^\/curricula\/([^/]+)\/assign$/);
    if (assignMatch && method === "POST") {
      calls.assign++;
      const payload = req.postDataJSON() as { student_id: string };
      lastBody.assign = payload;
      const rootId = assignMatch[1];
      const template = curricula.find((c) => c.id === rootId);
      const title = template?.title ?? "Untitled";
      state.assignments.unshift({ assignment_id: randomUUID(), curriculum_block_id: rootId, title });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          id: randomUUID(),
          kind: "course",
          title,
          body: null,
          est_minutes: null,
          order: 0,
          language: template?.language ?? "en",
          plane: "content",
          student_id: payload.student_id,
          children: [],
        }),
      });
      return;
    }
    const blockMatch = pathname.match(/^\/blocks\/([^/]+)$/);
    if (blockMatch && method === "GET") {
      calls.block++;
      const block = blocksById[blockMatch[1]];
      if (!block) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          headers: CORS_HEADERS,
          body: JSON.stringify({ detail: "block not found" }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          id: block.id,
          kind: "lesson",
          title: block.title,
          body: null,
          est_minutes: null,
          order: 0,
          language: "en",
          plane: "content",
          student_id: null,
          children: [],
        }),
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

  await page.route(`${API_ORIGIN}/students`, handler);
  await page.route(`${API_ORIGIN}/students/**`, handler);
  await page.route(`${API_ORIGIN}/curricula`, handler);
  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/blocks/**`, handler);

  return { calls, lastBody, state, unexpected };
}

test.describe("student detail (mocked API)", () => {
  test("opens from the students list and shows assignments, progress, and recent lessons", async ({ page }) => {
    const student = seedStudent({ name: "Elena Nikolaou", level: "intermediate", instrument: "guitar" });
    const blockA = randomUUID();
    const rootTemplate = randomUUID();

    const mock = await mockStudentDetailApi(page, {
      student,
      assignments: [
        { assignment_id: randomUUID(), curriculum_block_id: rootTemplate, title: "Tone Shaping Fundamentals" },
      ],
      progress: [
        seedProgress({ student_id: student.id, block_id: blockA, status: "practicing", notes: "sounding good" }),
      ],
      lessons: [
        seedLessonLog({
          student_id: student.id,
          session_block_id: blockA,
          date: "2026-01-15",
          taught: true,
          notes: "good session",
          homework: "practice 15 min/day",
        }),
      ],
      blocksById: { [blockA]: { id: blockA, title: "Pickups and Tone" } },
    });

    await page.goto("/en/students");
    const row = page.getByTestId("student-item").filter({ hasText: "Elena Nikolaou" });
    await row.getByTestId("student-name").click();

    await expect(page).toHaveURL(new RegExp(`/en/students/${student.id}$`));
    await expect(page.getByTestId("student-detail-heading")).toHaveText("Elena Nikolaou");
    await expect(page.getByTestId("student-level")).toContainText("intermediate");

    await expect(page.getByTestId("assignment-item")).toHaveCount(1);
    await expect(page.getByTestId("assignment-title")).toHaveText("Tone Shaping Fundamentals");

    await expect(page.getByTestId("progress-item")).toHaveCount(1);
    await expect(page.getByTestId("progress-block-title")).toContainText("Pickups and Tone");
    await expect(page.getByTestId("progress-status-practicing")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("progress-notes")).toHaveText("sounding good");

    await expect(page.getByTestId("lesson-item")).toHaveCount(1);
    await expect(page.getByTestId("lesson-block-title")).toContainText("Pickups and Tone");
    await expect(page.getByTestId("lesson-notes")).toHaveText("good session");
    await expect(page.getByTestId("lesson-homework")).toContainText("practice 15 min/day");

    expect(mock.calls.detail).toBeGreaterThanOrEqual(1);
    expect(mock.unexpected).toEqual([]);
  });

  test("changing a progress status posts the new status while preserving existing notes", async ({ page }) => {
    const student = seedStudent({ name: "Progress Student" });
    const blockA = randomUUID();
    const mock = await mockStudentDetailApi(page, {
      student,
      progress: [
        seedProgress({ student_id: student.id, block_id: blockA, status: "practicing", notes: "sounding good" }),
      ],
      blocksById: { [blockA]: { id: blockA, title: "Pickups and Tone" } },
    });

    await page.goto(`/en/students/${student.id}`);
    await expect(page.getByTestId("progress-item")).toHaveCount(1);
    await expect(page.getByTestId("progress-status-practicing")).toHaveAttribute("aria-pressed", "true");

    await page.getByTestId("progress-status-mastered").click();

    await expect(page.getByTestId("progress-status-mastered")).toHaveAttribute("aria-pressed", "true");
    expect(mock.calls.progress).toBe(1);
    // Notes were NOT part of this click — they must still be resent
    // unchanged (see `ProgressInput`'s docstring in `lib/api.ts`): the API
    // overwrites `notes` wholesale, so omitting it here would silently wipe
    // the existing note.
    expect(mock.lastBody.progress).toEqual({ block_id: blockA, status: "mastered", notes: "sounding good" });
    expect(mock.unexpected).toEqual([]);
  });

  test("assigning a curriculum posts to /curricula/{root}/assign and the assignment appears", async ({ page }) => {
    const student = seedStudent({ name: "Assign Student" });
    const rootId = randomUUID();
    const mock = await mockStudentDetailApi(page, {
      student,
      curricula: [
        { id: rootId, title: "Rhythm Basics", language: "en", target_profile: null, created_at: new Date().toISOString() },
      ],
    });

    await page.goto(`/en/students/${student.id}`);
    await expect(page.getByTestId("student-assignments-empty")).toBeVisible();

    await page.getByTestId("assign-curriculum-button").click();
    await expect(page.getByTestId("assign-curriculum-dialog")).toBeVisible();
    await expect(page.getByTestId("assign-curriculum-select")).toBeVisible();
    await page.getByTestId("assign-curriculum-select").selectOption({ value: rootId });
    await page.getByTestId("assign-curriculum-submit").click();

    await expect(page.getByTestId("assign-curriculum-dialog")).toBeHidden();
    await expect(page.getByTestId("assignment-item").filter({ hasText: "Rhythm Basics" })).toBeVisible();

    expect(mock.calls.assign).toBe(1);
    expect(mock.lastBody.assign).toEqual({ student_id: student.id });
    expect(mock.unexpected).toEqual([]);
  });

  test("logging a lesson posts to /students/{id}/lessons and it appears in recent lessons", async ({ page }) => {
    const student = seedStudent({ name: "Lesson Student" });
    const blockA = randomUUID();
    const mock = await mockStudentDetailApi(page, {
      student,
      blocksById: { [blockA]: { id: blockA, title: "Session 1" } },
    });

    await page.goto(`/en/students/${student.id}`);
    await expect(page.getByTestId("student-lessons-empty")).toBeVisible();

    await page.getByTestId("log-lesson-block-id").fill(blockA);
    await page.getByTestId("log-lesson-date").fill("2026-02-01");
    await page.getByTestId("log-lesson-notes").fill("worked on strumming");
    await page.getByTestId("log-lesson-homework").fill("practice daily");
    await page.getByTestId("log-lesson-submit").click();

    await expect(page.getByTestId("lesson-item")).toHaveCount(1);
    await expect(page.getByTestId("lesson-notes")).toHaveText("worked on strumming");
    await expect(page.getByTestId("lesson-homework")).toContainText("practice daily");

    expect(mock.calls.lessons).toBe(1);
    expect(mock.lastBody.lessons).toEqual({
      session_block_id: blockA,
      date: "2026-02-01",
      taught: true, // this form's own default, unchanged in this test
      notes: "worked on strumming",
      homework: "practice daily",
    });
    expect(mock.unexpected).toEqual([]);
  });
});
