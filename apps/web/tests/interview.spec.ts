import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the guided curriculum-authoring
// interview (Plan 12 Task 3, G2's frontend) — `InterviewDialog` and its 5
// step components. Origin-anchored routes + explicit CORS/OPTIONS handling,
// same convention as `cockpit.spec.ts` (that file's own docstring explains
// why a bare `**/curricula` suffix glob is unsafe here: this app's OWN
// `/en/curricula` page URL also ends in "/curricula").
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
  "Access-Control-Allow-Headers": "content-type",
};

interface FixtureModule {
  title: string;
  objective: string;
  gap: boolean;
  passages: { source_title: string; page_no: number | null; score: number }[];
}

interface FixtureSource {
  id: string;
  title: string;
  type: string;
  char_count: number;
  default_selected: boolean;
}

interface FixtureTreeNode {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  est_minutes: number | null;
  order: number;
  language: string;
  plane: string;
  student_id: string | null;
  children: FixtureTreeNode[];
}

function makeTree(rootId: string, title: string): FixtureTreeNode {
  return {
    id: rootId, kind: "course", title, body: null, est_minutes: null,
    order: 0, language: "en", plane: "content", student_id: null,
    children: [
      {
        id: randomUUID(), kind: "module", title: "Pedals & Effects", body: null,
        est_minutes: null, order: 0, language: "en", plane: "content",
        student_id: null, children: [],
      },
    ],
  };
}

/** Drives the full 5-step interview state machine (`who -> duration ->
 * sources -> preview -> confirm`) exactly the way `app.curriculum.
 * interview.answer_interview` does on the real API: one interview at a
 * time, `step` advances only on a valid answer, and the "confirm" step's
 * approval returns a 202 `{job_id}` — never another `InterviewStateOut` —
 * mirroring `answer_curriculum_interview`'s own two-shape response
 * (`InterviewDialog`'s `isJobAccepted` is what this whole test suite is
 * ultimately checking client-side). */
async function mockInterviewApi(
  page: Page,
  {
    students = [] as { id: string; name: string }[],
    sources = [] as FixtureSource[],
    modules = [] as FixtureModule[],
    tree,
    pendingPolls = 1,
  }: {
    students?: { id: string; name: string }[];
    sources?: FixtureSource[];
    modules?: FixtureModule[];
    tree?: FixtureTreeNode;
    pendingPolls?: number;
  } = {},
) {
  const interviewId = randomUUID();
  let step = "who";
  let courseTitle = "";
  let jobId: string | null = null;
  let jobPolls = 0;
  const calls = { start: 0, answer: 0, job: 0, get: 0 };
  const answerBodies: unknown[] = [];
  const unexpected: string[] = [];

  function gapCount() {
    return modules.filter((m) => m.gap).length;
  }

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/curricula/interview" && method === "POST") {
      calls.start++;
      const payload = req.postDataJSON() as { title: string; domain?: string | null };
      courseTitle = payload.title;
      step = "who";
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          interview_id: interviewId,
          step: "who",
          question: "Who is this curriculum for? Pick one of your students, or answer with a name for someone new.",
          options: students.map((s) => ({ value: s.id, label: s.name })),
          findings: null,
          error: null,
        }),
      });
      return;
    }

    const answerMatch = pathname.match(/^\/curricula\/interview\/([^/]+)\/answer$/);
    if (answerMatch && method === "POST") {
      calls.answer++;
      const payload = req.postDataJSON() as { answer: unknown };
      answerBodies.push(payload.answer);

      if (step === "who") {
        step = "duration";
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            interview_id: interviewId, step: "duration",
            question: "How many weeks should this run, and how many minutes per session?",
            options: null, findings: null, error: null,
          }),
        });
        return;
      }
      if (step === "duration") {
        step = "sources";
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            interview_id: interviewId, step: "sources",
            question: "Which of your library sources should this draw on?",
            options: sources.map((s) => ({
              value: s.id, label: s.title, type: s.type,
              char_count: s.char_count, default_selected: s.default_selected,
            })),
            findings: null, error: null,
          }),
        });
        return;
      }
      if (step === "sources") {
        step = "preview";
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            interview_id: interviewId, step: "preview",
            question: "Here's the proposed outline and what your library actually supports for it. Proceed to confirm?",
            options: null,
            findings: { course_title: courseTitle, modules, gap_count: gapCount() },
            error: null,
          }),
        });
        return;
      }
      if (step === "preview") {
        step = "confirm";
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            interview_id: interviewId, step: "confirm",
            question: "Ready to generate? Approve to enqueue it.",
            options: null,
            findings: { course_title: courseTitle, modules, gap_count: gapCount() },
            error: null,
          }),
        });
        return;
      }
      if (step === "confirm") {
        jobId = randomUUID();
        jobPolls = 0;
        await route.fulfill({
          status: 202, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ job_id: jobId, status: "pending" }),
        });
        return;
      }
    }

    const jobMatch = pathname.match(/^\/jobs\/([^/]+)$/);
    if (jobMatch && method === "GET") {
      calls.job++;
      jobPolls++;
      const base = { id: jobMatch[1], kind: "curriculum", created_at: new Date().toISOString() };
      const updated_at = new Date().toISOString();
      if (jobPolls <= pendingPolls) {
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ ...base, updated_at, status: "pending", result_root_id: null, error: null, error_kind: null }),
        });
        return;
      }
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          ...base, updated_at, status: "succeeded",
          result_root_id: tree?.id ?? null, error: null, error_kind: null,
        }),
      });
      return;
    }

    if (pathname === "/curricula" && method === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: "[]" });
      return;
    }

    const getMatch = pathname.match(/^\/curricula\/([^/]+)$/);
    if (getMatch && method === "GET") {
      calls.get++;
      const found = tree && tree.id === getMatch[1] ? tree : null;
      await route.fulfill({
        status: found ? 200 : 404, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(found ?? { detail: "not found" }),
      });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(`${API_ORIGIN}/curricula`, handler);
  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/jobs/**`, handler);

  return { calls, answerBodies, unexpected, interviewId: () => interviewId };
}

const SOURCES: FixtureSource[] = [
  { id: "book-1", title: "Getting Great Guitar Sounds", type: "pdf", char_count: 220_000, default_selected: true },
  { id: "spine-1", title: "Guitar Tone & Gear — Course Spine", type: "text", char_count: 16_141, default_selected: true },
];

const MODULES: FixtureModule[] = [
  {
    title: "Pedals & Effects", objective: "Shape tone with stompboxes.", gap: false,
    passages: [
      { source_title: "Getting Great Guitar Sounds", page_no: 56, score: 0.71 },
      { source_title: "Getting Great Guitar Sounds", page_no: 75, score: 0.66 },
    ],
  },
  { title: "Vibrato", objective: "Add expressive pitch variation.", gap: true, passages: [] },
];

async function startToStep(page: Page, targetStep: "who" | "duration" | "sources" | "preview" | "confirm") {
  await page.goto("/en/curricula");
  await page.getByTestId("curricula-generate-button").click();
  await expect(page.getByTestId("interview-dialog")).toBeVisible();
  await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
  await page.getByTestId("interview-start-submit").click();

  if (targetStep === "who") return;
  await page.getByTestId("interview-who-new-toggle").click();
  await page.getByTestId("interview-who-name").fill("Nikos");
  await page.getByTestId("interview-answer-submit").click();

  if (targetStep === "duration") return;
  await page.getByTestId("interview-duration-weeks").fill("6");
  await page.getByTestId("interview-duration-minutes").fill("30");
  await page.getByTestId("interview-answer-submit").click();

  if (targetStep === "sources") return;
  await page.getByTestId("interview-answer-submit").click(); // accept default source selection

  if (targetStep === "preview") return;
  await page.getByTestId("interview-answer-submit").click(); // proceed
}

test.describe("guided curriculum interview (mocked API)", () => {
  test("advances step by step and renders each step's question", async ({ page }) => {
    const mock = await mockInterviewApi(page, { students: [{ id: "stu-1", name: "Maria" }], sources: SOURCES, modules: MODULES });

    await startToStep(page, "who");
    await expect(page.getByTestId("interview-dialog")).toContainText(/who is this curriculum for/i);
    await expect(page.getByTestId("interview-who-option-stu-1")).toContainText("Maria");

    await page.getByTestId("interview-who-option-stu-1").click();
    await page.getByTestId("interview-answer-submit").click();
    await expect(page.getByTestId("interview-dialog")).toContainText(/how long should this run/i);
    await expect(page.getByTestId("interview-duration-weeks")).toBeVisible();

    await page.getByTestId("interview-duration-weeks").fill("6");
    await page.getByTestId("interview-duration-minutes").fill("30");
    await page.getByTestId("interview-answer-submit").click();
    await expect(page.getByTestId("interview-dialog")).toContainText(/which of your library sources/i);

    expect(mock.calls.start).toBe(1);
    expect(mock.calls.answer).toBe(2);
    expect(mock.answerBodies[0]).toEqual({ student_id: "stu-1" });
    expect(mock.answerBodies[1]).toEqual({ weeks: 6, minutes_per_session: 30 });
    expect(mock.unexpected).toEqual([]);
  });

  test("the sources step lists his sources with char_counts and lets him deselect the filler source", async ({ page }) => {
    const mock = await mockInterviewApi(page, { sources: SOURCES, modules: MODULES });
    await startToStep(page, "sources");

    // Both real book and synthetic filler are visible with their char counts —
    // nothing pre-excluded, the tutor sees both.
    await expect(page.getByTestId("interview-source-row-book-1")).toContainText("Getting Great Guitar Sounds");
    await expect(page.getByTestId("interview-source-charcount-book-1")).toContainText("220000");
    await expect(page.getByTestId("interview-source-spine-1")).toBeChecked();
    await expect(page.getByTestId("interview-source-charcount-spine-1")).toContainText("16141");

    // Deselect the filler in one click.
    await page.getByTestId("interview-source-spine-1").uncheck();
    await expect(page.getByTestId("interview-source-spine-1")).not.toBeChecked();
    await expect(page.getByTestId("interview-source-book-1")).toBeChecked();

    await page.getByTestId("interview-answer-submit").click();

    const sourcesAnswer = mock.answerBodies.at(-1) as { source_ids: string[] };
    expect(sourcesAnswer.source_ids).toContain("book-1");
    expect(sourcesAnswer.source_ids).not.toContain("spine-1");
  });

  test("the preview step shows found passages per module and flags a gap", async ({ page }) => {
    await mockInterviewApi(page, { sources: SOURCES, modules: MODULES });
    await startToStep(page, "preview");

    const grounded = page.getByTestId("interview-module").filter({ hasText: "Pedals & Effects" });
    await expect(grounded).toContainText("Getting Great Guitar Sounds");
    await expect(grounded.getByTestId("interview-gap-0")).toHaveCount(0);

    const gapModule = page.getByTestId("interview-module").filter({ hasText: "Vibrato" });
    await expect(gapModule).toContainText(/nothing in your library/i);
    await expect(page.getByTestId("interview-preview-summary")).toContainText("1");
  });

  test("a passage citation links to /[locale]/library/{source_id}?page={n}", async ({ page }) => {
    await mockInterviewApi(page, { sources: SOURCES, modules: MODULES });
    await startToStep(page, "preview");

    const link = page.getByTestId("interview-passage-0-0");
    await expect(link).toHaveAttribute("href", "/en/library/book-1?page=56");
  });

  test("confirm posts the chosen source_ids + allow_general, gets 202, polls, and lands on the curriculum", async ({ page }) => {
    const rootId = randomUUID();
    const tree = makeTree(rootId, "Getting a Great Guitar Tone");
    const mock = await mockInterviewApi(page, { sources: SOURCES, modules: MODULES, tree, pendingPolls: 1 });

    await startToStep(page, "confirm");
    await expect(page.getByTestId("interview-dialog")).toContainText(/ready to generate/i);
    await expect(page.getByTestId("interview-allow-general")).toBeVisible(); // MODULES has a gap

    await page.getByTestId("interview-allow-general").check();
    await page.getByTestId("interview-confirm-submit").click();

    await expect(page.getByTestId("interview-job-loading")).toBeVisible();
    await expect(page.getByTestId("interview-dialog")).toBeHidden({ timeout: 10_000 });

    await expect(page.getByTestId("block-card-title").filter({ hasText: "Getting a Great Guitar Tone" })).toBeVisible();

    const confirmAnswer = mock.answerBodies.at(-1) as { approved: boolean; allow_general: boolean };
    expect(confirmAnswer).toEqual({ approved: true, allow_general: true });
    expect(mock.calls.job).toBe(2); // pending once, then succeeded
    expect(mock.unexpected).toEqual([]);
  });
});
