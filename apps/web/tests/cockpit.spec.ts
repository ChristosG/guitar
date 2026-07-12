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
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
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

/** Real backend copy (`app.jobs.runner.run_curriculum_job`'s
 * `GuidedJSONError` branch) reused as the fixture's failure message, so the
 * failed-job test below is directly comparable to a real error. */
const JOB_FAILURE_MESSAGE =
  "Curriculum generation failed (model returned invalid/truncated output). Try again.";

/** Mocks `/curricula`, `/curricula/generate`, `/jobs/{id}` and
 * `/curricula/{id}`/`/blocks/{id}` (PATCH) — the full async
 * generate-then-poll contract (Plan 8 Task 4): `POST /curricula/generate`
 * returns a 202 `{job_id, status: "pending"}` immediately (no tree body any
 * more — see `lib/api.ts`'s `startCurriculumGeneration`); `GET /jobs/{id}`
 * answers "pending" for `pendingPolls` calls, then a terminal status. This
 * is what makes the "non-frozen loading state" assertions below genuine
 * rather than raced past: the dialog's own poll loop waits ~2s between
 * calls (`generate-dialog.tsx`), so with the default `pendingPolls: 1` the
 * loading state is provably visible across a real wait, not just a
 * synchronous tick.
 *
 * `jobOutcome: "succeeded"` (default) resolves with `result_root_id` set to
 * the generated tree's root id, fetchable via the `GET /curricula/{id}`
 * handler below — and only THEN is the new template added to the
 * `/curricula` list, mirroring `run_curriculum_job`'s real behavior (the row
 * is persisted inside the background job, not at enqueue time).
 * `jobOutcome: "failed"` resolves with `error`/`error_kind` set and no
 * `result_root_id` — nothing is added to `templates`. */
async function mockCurriculaApi(
  page: Page,
  {
    templates = [] as CurriculumListItemFixture[],
    pendingPolls = 1,
    jobOutcome = "succeeded" as "succeeded" | "failed",
  } = {},
) {
  let currentTree: FixtureBlock | null = null;
  let jobPolls = 0;
  const calls = { list: 0, generate: 0, job: 0, get: 0, patch: 0 };
  const lastBody: { generate?: unknown; patch?: unknown } = {};
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
    if (pathname === "/curricula/generate" && method === "POST") {
      calls.generate++;
      const payload = req.postDataJSON() as { title: string; language: string };
      lastBody.generate = payload;
      currentTree = makeGeneratedTree(payload.title, payload.language);
      jobPolls = 0;
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({ job_id: randomUUID(), status: "pending" }),
      });
      return;
    }
    const jobMatch = pathname.match(/^\/jobs\/([^/]+)$/);
    if (jobMatch && method === "GET") {
      calls.job++;
      jobPolls++;
      const base = { id: jobMatch[1], kind: "curriculum", created_at: new Date().toISOString() };
      const updated_at = new Date().toISOString();
      if (jobPolls <= pendingPolls) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS_HEADERS,
          body: JSON.stringify({
            ...base,
            updated_at,
            status: "pending",
            result_root_id: null,
            error: null,
            error_kind: null,
          }),
        });
        return;
      }
      if (jobOutcome === "failed") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS_HEADERS,
          body: JSON.stringify({
            ...base,
            updated_at,
            status: "failed",
            result_root_id: null,
            error: JOB_FAILURE_MESSAGE,
            error_kind: "upstream",
          }),
        });
        return;
      }
      // succeeded — only now does the generated curriculum "exist" for the
      // template list, mirroring the real background job's timing (see this
      // function's docstring above).
      if (currentTree) {
        templates.unshift({
          id: currentTree.id,
          title: currentTree.title,
          language: currentTree.language,
          target_profile: null,
          created_at: updated_at,
        });
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          ...base,
          updated_at,
          status: "succeeded",
          result_root_id: currentTree?.id ?? null,
          error: null,
          error_kind: null,
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
  await page.route(`${API_ORIGIN}/jobs/**`, handler);

  return { calls, lastBody, unexpected };
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
  test("generating shows a non-frozen loading state, renders the nested tree, and title edits PATCH", async ({
    page,
  }) => {
    const mock = await mockCurriculaApi(page, { templates: [] });

    await page.goto("/en/today");
    await page.getByTestId("nav-curricula").click();
    await expect(page).toHaveURL(/\/en\/curricula$/);
    await expect(page.getByTestId("templates-empty")).toBeVisible();
    await expect(page.getByTestId("board-empty")).toBeVisible();

    await page.getByTestId("curricula-generate-button").click();
    await expect(page.getByTestId("generate-dialog")).toBeVisible();

    await page.getByTestId("generate-title").fill("Tone Shaping Fundamentals");
    await page.getByTestId("generate-domain").fill("tone");
    await page.getByTestId("generate-level").fill("beginner");
    await page.getByTestId("generate-language").fill("en");
    await page.getByTestId("generate-hours").fill("6");
    await page.getByTestId("generate-submit").click();

    // Clear, non-frozen loading state while the job is enqueued and polled
    // (the mock answers "pending" once, then "succeeded" — see
    // `mockCurriculaApi`'s docstring) — this is the brief's core requirement.
    await expect(page.getByTestId("generate-loading")).toBeVisible();
    await expect(page.getByTestId("generate-submit")).toBeDisabled();
    await expect(page.getByTestId("generate-title")).toBeDisabled();

    // The dialog must not be dismissible mid-flight (Escape is a no-op).
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("generate-dialog")).toBeVisible();

    await expect(page.getByTestId("generate-dialog")).toBeHidden({ timeout: 10_000 });

    // course + module + 2 lessons. Nested BlockCards render *inside* their
    // parent's DOM subtree (that's what makes the indentation work), so a
    // descendant lookup scoped to "the course card" would also match its
    // module/lesson children — filter by the badge's own text globally
    // instead of scoping to a parent locator.
    await expect(page.getByTestId("block-card")).toHaveCount(4);
    await expect(page.getByTestId("block-card-kind").filter({ hasText: "Course" })).toHaveCount(1);
    await expect(page.getByTestId("block-card-kind").filter({ hasText: "Module" })).toHaveCount(1);
    await expect(page.getByTestId("block-card-kind").filter({ hasText: "Lesson" })).toHaveCount(2);
    await expect(page.getByTestId("block-card-title").filter({ hasText: "Signal Chain Basics" })).toBeVisible();
    await expect(page.getByTestId("block-card-title").filter({ hasText: "Pickups and Tone" })).toBeVisible();
    await expect(page.getByTestId("block-card-title").filter({ hasText: "Cables and Signal Integrity" })).toBeVisible();

    expect(mock.calls.generate).toBe(1);
    // Exactly 2 polls: the mock's default `pendingPolls: 1` answers "pending"
    // once, then "succeeded" — proving the dialog actually polled `GET
    // /jobs/{id}` rather than trusting the enqueue response alone.
    expect(mock.calls.job).toBe(2);
    const genBody = mock.lastBody.generate as {
      title: string;
      language: string;
      domain?: string;
      profile: Record<string, unknown>;
      target_minutes_total?: number;
    };
    expect(genBody.title).toBe("Tone Shaping Fundamentals");
    expect(genBody.domain).toBe("tone");
    expect(genBody.profile).toEqual({ level: "beginner" });
    expect(genBody.target_minutes_total).toBe(360);

    // Inline-edit the course (root) card's title -> PATCH /blocks/{id}.
    const courseTitle = page.getByTestId("block-card-title").filter({ hasText: "Tone Shaping Fundamentals" });
    await courseTitle.click();
    const titleInput = page.getByTestId("block-card-title-input");
    await titleInput.fill("Tone Shaping Fundamentals (Revised)");
    await titleInput.press("Enter");

    await expect(
      page.getByTestId("block-card-title").filter({ hasText: "Tone Shaping Fundamentals (Revised)" }),
    ).toBeVisible();
    expect(mock.calls.patch).toBe(1);
    expect(mock.lastBody.patch).toEqual({ title: "Tone Shaping Fundamentals (Revised)" });

    expect(mock.unexpected).toEqual([]);
  });

  test("shows the job's error and keeps the dialog open when generation fails", async ({ page }) => {
    const mock = await mockCurriculaApi(page, { templates: [], jobOutcome: "failed" });

    await page.goto("/en/today");
    await page.getByTestId("nav-curricula").click();
    await expect(page).toHaveURL(/\/en\/curricula$/);

    await page.getByTestId("curricula-generate-button").click();
    await page.getByTestId("generate-title").fill("Broken Curriculum");
    await page.getByTestId("generate-language").fill("en");
    await page.getByTestId("generate-submit").click();

    await expect(page.getByTestId("generate-loading")).toBeVisible();
    await expect(page.getByTestId("generate-error")).toHaveText(JOB_FAILURE_MESSAGE, { timeout: 10_000 });

    // NOT the success handoff: the dialog stays open and re-submittable.
    await expect(page.getByTestId("generate-dialog")).toBeVisible();
    await expect(page.getByTestId("generate-submit")).toBeEnabled();
    expect(mock.calls.generate).toBe(1);
    expect(mock.calls.job).toBe(2); // pending, then failed

    // Nothing was generated: board and template list are untouched.
    await expect(page.getByTestId("board-empty")).toBeVisible();
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
