import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the cockpit shell + Students +
// Curricula pages: every `/students/*` and `/curricula*`/`/blocks/*` call is
// intercepted via `page.route` and answered from an in-memory fixture —
// same convention as `knowledge.spec.ts` (CORS headers + explicit OPTIONS
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
// `knowledge.spec.ts` avoids this by coincidence (every `/knowledge/*` API
// path it mocks has a segment after "knowledge", which the bare
// `/en/knowledge` page URL doesn't) — anchoring to the origin here removes
// the ambiguity outright rather than relying on that kind of coincidence.
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

/** Mocks `/curricula`, `/curricula/generate`, `/curricula/{id}` and
 * `/blocks/{id}` (PATCH). `generateDelayMs` deliberately delays the
 * generate response so the loading-state assertions in the test below are
 * exercised for real, not raced past. */
async function mockCurriculaApi(
  page: Page,
  { templates = [] as CurriculumListItemFixture[], generateDelayMs = 800 } = {},
) {
  let currentTree: FixtureBlock | null = null;
  const calls = { list: 0, generate: 0, get: 0, patch: 0 };
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
      const tree = makeGeneratedTree(payload.title, payload.language);
      currentTree = tree;
      templates.unshift({
        id: tree.id,
        title: tree.title,
        language: tree.language,
        target_profile: null,
        created_at: new Date().toISOString(),
      });
      if (generateDelayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, generateDelayMs));
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(tree),
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

  return { calls, lastBody, unexpected };
}

test.describe("cockpit shell", () => {
  test("the persistent nav shows all 5 sections and links to /students, /knowledge", async ({ page }) => {
    await page.goto("/en/today");

    await expect(page.getByTestId("app-title")).toHaveText(/Guitar Tutor Copilot/);
    await expect(page.getByTestId("today-heading")).toBeVisible();
    await expect(page.getByTestId("today-placeholder-card")).toHaveCount(2);

    for (const section of ["today", "students", "curricula", "knowledge", "notes"]) {
      await expect(page.getByTestId(`nav-${section}`)).toBeVisible();
    }
    await expect(page.getByTestId("nav-students")).toHaveAttribute("href", "/en/students");
    await expect(page.getByTestId("nav-knowledge")).toHaveAttribute("href", "/en/knowledge");

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

    // Clear, non-frozen loading state while the (deliberately delayed) mock
    // request is in flight — this is the brief's core requirement.
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
});
