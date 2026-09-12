import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Task 3.4 — «Προσθήκη μαθήματος» asks what the lesson should teach.
//
// The module's ⋯ menu used to plant a blank box called «Νέο μάθημα» and leave
// the tutor to write it himself. It now opens a dialog with ONE question —
// "what do you want this lesson to teach?" — and hands his words to the
// planner. What this spec pins down:
//
//  1. THE DOOR. The menu item opens the dialog instead of POSTing. A menu item
//     that silently created a row was the whole complaint.
//  2. THE FLOOR. «Δημιουργία με AI» is dead until the brief is a real
//     sentence (the API's own `min_length=10`), and the dialog SAYS why rather
//     than leaving a disabled button unexplained. A 422 on a three-word brief
//     would be a server round trip to learn what the form already knows.
//  3. THE BODY. What is POSTed is exactly what he filled in: the brief, a
//     `title` of null when he left it to the AI, and the sibling he picked in
//     «Θέση» — a position that silently appended would move his lesson.
//  4. THE JOB. 202 → poll, with «Σχεδιάζω…» on screen while the planner reads
//     the library (20-60s), then the dialog closes and the BOARD refetches —
//     that refetch is what re-arms the draft progress bar over the new
//     `queued` lesson. There is NO «Γράφεται…» step in this dialog and the
//     mocks must not invent one: the runner commits `status="succeeded"` and
//     `progress.phase="drafting"` in the SAME commit
//     (`app/jobs/lesson_generate.py`), so no poll can see one without the
//     other. The board's own status pill is what reports the draft.
//  5. THE OLD BEHAVIOUR SURVIVES. «Κενό μάθημα» still POSTs the plain
//     `/blocks/{module}/lessons` — sometimes he just wants a container — and
//     it lands WHERE HE POINTED: the «Θέση» select is above both buttons, so a
//     blank lesson that ignored it and appended would be a silently wrong
//     answer to a question the form asked him.
//  6. IT NEVER TRAPS HIM. The job runs on the server, not in this dialog, so
//     closing it mid-run cancels nothing: the poll keeps going, the tree still
//     refreshes when the lesson lands, and the board's progress bar takes over.
//  7. …AND BAD NEWS COMES BACK TO FIND HIM. Because he may have closed it, a
//     failure written into an off-screen dialog would be a failure nobody ever
//     reads — worst of all the one he can actually fix (no API key). The
//     dialog reopens itself and says it in Greek.
//
// Same route-interception convention as the cockpit specs next door
// (`lesson-ai-panel.spec.ts`, `tutor-edited.spec.ts`): anchored to
// `API_ORIGIN`, explicit CORS + OPTIONS, and an unmocked-request catch-all so
// a routing mistake fails loudly instead of hanging.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

/** Chris's own words for a lesson about the neck — the first lines of the
 * outline he wrote by hand. Greek on purpose: `el` is the product's default
 * locale, and the brief is the one field that goes to the model verbatim. */
const BRIEF = [
  "Θέλω ένα μάθημα για τον λαιμό της κιθάρας.",
  "Πρώτα οι νότες στις πρώτες πέντε θέσεις, με ονόματα, όχι μόνο σχήματα.",
  "Μετά πώς δένουν τα σχήματα CAGED μεταξύ τους πάνω στην ταστιέρα.",
].join("\n");

/** A Greek course → one module → two READY lessons. Two, not one, because the
 * «Θέση» select is the point: with a single sibling a broken `after` would
 * still look right. */
function tree(rootId: string, moduleId: string, lessonIds: string[]) {
  return {
    id: rootId, kind: "course", title: "Ήχος και τεχνική", body: null, est_minutes: null,
    order: 0, language: "el", plane: "content", student_id: null, meta: {},
    children: [{
      id: moduleId, kind: "module", title: "Ο λαιμός", body: "Στόχος της ενότητας",
      est_minutes: null, order: 0, language: "el", plane: "content", student_id: null, meta: {},
      children: lessonIds.map((id, i) => ({
        id, kind: "lesson", title: i === 0 ? "Πρώτη θέση" : "Σχήματα CAGED",
        body: "Περίληψη", est_minutes: 50, order: i, language: "el", plane: "content",
        student_id: null,
        meta: { draft_status: "ready", word_count: 2400, target_words: 2750, meets_floor: true },
        children: [],
      })),
    }],
  };
}

function progress(rootId: string) {
  return {
    root_id: rootId, total: 2, queued: 0, drafting: 0, ready: 2, failed: 0,
    done: true, draft_error: null,
  };
}

/** The board's own calls. `treeCalls` is counted because the refetch after the
 * job is what puts the new lesson on the board at all. */
async function mockCurriculaApi(page: Page, rootId: string, moduleId: string, lessonIds: string[]) {
  const unexpected: string[] = [];
  const counts = { tree: 0 };

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

    if (pathname === `/curricula/${rootId}/chat-session` && method === "GET") {
      return json({ session_id: randomUUID() });
    }
    if (pathname === `/curricula/${rootId}/progress` && method === "GET") {
      return json(progress(rootId));
    }
    if (pathname === `/curricula/${rootId}` && method === "GET") {
      counts.tree++;
      return json(tree(rootId, moduleId, lessonIds));
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });

  return { unexpected, counts };
}

/** Open the board, expand nothing, and open the MODULE's ⋯ → «Προσθήκη
 * μαθήματος». The module row is visible with the root expanded; its lessons
 * are not, and do not need to be. */
async function openAddLessonDialog(page: Page, rootId: string) {
  await page.goto(`/el/curricula/${rootId}`);
  await expect(page.getByTestId("tree-board")).toBeVisible();

  const moduleCard = page.locator('[data-testid="block-card"][data-kind="module"]');
  await moduleCard.getByTestId("block-card-menu").click();
  await page.getByTestId("menu-add-lesson").click();

  const dialog = page.getByTestId("add-lesson-dialog");
  await expect(dialog).toBeVisible();
  return dialog;
}

test.describe("«Προσθήκη μαθήματος» — the brief, the position, and the job (mocked API)", () => {
  test("the dialog asks for a brief, POSTs it, and polls the job to the board", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonIds = [randomUUID(), randomUUID()];
    const jobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonIds);

    let generateCalls = 0;
    let blankCalls = 0;
    let polls = 0;
    let sent: { brief: string; title: string | null; after: string | null } | null = null;

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${moduleId}/lessons/generate` && req.method() === "POST") {
        generateCalls++;
        sent = req.postDataJSON() as typeof sent;
        await route.fulfill({
          status: 202, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ job_id: jobId, status: "pending" }),
        });
        return;
      }
      if (pathname === `/blocks/${moduleId}/lessons` && req.method() === "POST") {
        blankCalls++;
        await route.fulfill({
          status: 500, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ detail: "the AI path must not POST the blank lesson" }),
        });
        return;
      }
      await route.fulfill({
        status: 500, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
    });

    await page.route(`${API_ORIGIN}/jobs/**`, async (route: Route) => {
      const req = route.request();
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      polls++;
      // EXACTLY THE TWO STATES THE RUNNER WRITES: `running` with `phase:
      // "planning"` while the library call runs, then one commit that flips
      // the job to `succeeded` AND writes `phase: "drafting"` with the lesson
      // it just planted. A `running` tick carrying `phase: "drafting"` would
      // be fiction, and a UI built on it would show a state nobody can reach.
      const done = polls >= 2;
      const now = new Date().toISOString();
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: jobId, kind: "lesson_generate", status: done ? "succeeded" : "running",
          result_root_id: done ? rootId : null, error: null, error_kind: null,
          progress: done
            ? { phase: "drafting", lesson_id: randomUUID(), draft_job_id: randomUUID() }
            : { phase: "planning" },
          created_at: now, updated_at: now,
        }),
      });
    });

    const dialog = await openAddLessonDialog(page, rootId);
    const treeCallsBefore = api.counts.tree;

    // THE FLOOR. Empty: dead, and no scolding yet — he has not typed anything.
    const generate = dialog.getByTestId("add-lesson-generate");
    await expect(generate).toBeDisabled();
    await expect(dialog.getByTestId("add-lesson-too-short")).toHaveCount(0);

    // Nine characters: still dead, and NOW it says why.
    await dialog.getByTestId("add-lesson-brief").fill("ο λαιμός");
    await expect(generate).toBeDisabled();
    await expect(dialog.getByTestId("add-lesson-too-short")).toBeVisible();

    // The counter is always on screen, so the ceiling is a number he can see
    // before he is over it: "8/20000" while he is nine characters in.
    await expect(dialog.getByTestId("add-lesson-brief-count")).toHaveText("8/20000");

    // THE CEILING. This box takes PASTED text — a syllabus, a chat log — so it
    // is reachable, and reaching it silently would mean a long paste, a click,
    // and the API's 422 (whose English detail this dialog refuses to show him).
    await dialog.getByTestId("add-lesson-brief").fill("α".repeat(20_001));
    await expect(dialog.getByTestId("add-lesson-brief-count")).toHaveText("20001/20000");
    await expect(dialog.getByTestId("add-lesson-too-long")).toBeVisible();
    await expect(generate).toBeDisabled();

    await dialog.getByTestId("add-lesson-brief").fill(BRIEF);
    await expect(dialog.getByTestId("add-lesson-too-short")).toHaveCount(0);
    await expect(dialog.getByTestId("add-lesson-too-long")).toHaveCount(0);
    await expect(generate).toBeEnabled();

    // THE POSITION. «Μετά από: Πρώτη θέση» — between the two, not appended.
    const after = dialog.getByTestId("add-lesson-after");
    await expect(after.locator("option")).toHaveCount(3); // «Στο τέλος» + two lessons
    await after.selectOption(lessonIds[0]);

    await generate.click();

    // THE WAIT IS NAMED, not a bare spinner.
    const status = dialog.getByTestId("add-lesson-status");
    await expect(status).toContainText("Σχεδιάζω", { timeout: 10_000 });
    // And it says HOW LONG (Task 4.3): this dialog now waits 40 minutes, which
    // only reads as "working" if the wait accounts for itself.
    await expect(dialog.getByTestId("add-lesson-elapsed")).toContainText("λεπτά");
    // And it says he is free to go — the job is the server's, not this
    // dialog's, and a tutor held hostage by a 10-minute spinner is the bug.
    await expect(dialog.getByTestId("add-lesson-close-hint")).toBeVisible();
    // Nothing to retry while the job runs — a second POST is a second lesson.
    await expect(generate).toBeDisabled();

    // It closes on `succeeded`, and the board refetches: the new lesson is
    // `queued`, which is what re-arms the draft progress bar.
    await expect(dialog).toBeHidden({ timeout: 15_000 });
    await expect(dialog.getByTestId("add-lesson-error")).toHaveCount(0);
    expect(api.counts.tree).toBeGreaterThan(treeCallsBefore);
    expect(api.counts.tree).toBeGreaterThanOrEqual(2);

    // EXACTLY ONE POST, with exactly what he filled in.
    expect(generateCalls).toBe(1);
    expect(blankCalls).toBe(0);
    const body = sent as unknown as { brief: string; title: string | null; after: string | null };
    expect(body.brief).toBe(BRIEF);
    // He left the title to the AI — an empty string here would become a lesson
    // literally called "".
    expect(body.title).toBeNull();
    expect(body.after).toBe(lessonIds[0]);

    // It POLLED — the 202 was not mistaken for the answer, and the `running`
    // tick before it was not mistaken for a result either.
    expect(polls).toBeGreaterThanOrEqual(2);
    expect(api.unexpected).toEqual([]);
  });

  test("«Κενό μάθημα» creates the blank lesson where he pointed", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonIds = [randomUUID(), randomUUID()];

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonIds);

    const blankBodies: { title: string; after: string | null }[] = [];
    let generateCalls = 0;

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${moduleId}/lessons` && req.method() === "POST") {
        blankBodies.push(req.postDataJSON() as { title: string; after: string | null });
        await route.fulfill({
          status: 201, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            id: randomUUID(), kind: "lesson", title: "Νέο μάθημα", body: null,
            est_minutes: null, order: 2, language: "el", plane: "content",
            student_id: null, meta: { draft_status: "queued" }, children: [],
          }),
        });
        return;
      }
      if (pathname === `/blocks/${moduleId}/lessons/generate`) generateCalls++;
      await route.fulfill({
        status: 500, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
    });

    const dialog = await openAddLessonDialog(page, rootId);
    const treeCallsBefore = api.counts.tree;

    // No brief needed for a blank box — that button is alive from the start.
    await expect(dialog.getByTestId("add-lesson-empty")).toBeEnabled();
    // «Θέση» is ONE control above TWO buttons: it has to mean the same thing
    // for both. A blank lesson that appended after he picked a position would
    // be the form ignoring an answer it asked for.
    await dialog.getByTestId("add-lesson-after").selectOption(lessonIds[0]);
    await dialog.getByTestId("add-lesson-empty").click();

    await expect(dialog).toBeHidden({ timeout: 10_000 });
    expect(blankBodies).toHaveLength(1);
    expect(blankBodies[0].title).toBe("Νέο μάθημα");
    expect(blankBodies[0].after).toBe(lessonIds[0]);
    expect(generateCalls).toBe(0);
    expect(api.counts.tree).toBeGreaterThan(treeCallsBefore);
    expect(api.unexpected).toEqual([]);
  });

  test("closing the dialog mid-job cancels nothing — the lesson still lands", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonIds = [randomUUID(), randomUUID()];
    const jobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonIds);

    let generateCalls = 0;
    let polls = 0;

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${moduleId}/lessons/generate` && req.method() === "POST") {
        generateCalls++;
        await route.fulfill({
          status: 202, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ job_id: jobId, status: "pending" }),
        });
        return;
      }
      await route.fulfill({
        status: 500, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
    });

    await page.route(`${API_ORIGIN}/jobs/**`, async (route: Route) => {
      const req = route.request();
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      polls++;
      // Four `running` ticks — long enough that the dialog is CLOSED for most
      // of this job's life, which is the whole point of the test.
      const done = polls >= 5;
      const now = new Date().toISOString();
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: jobId, kind: "lesson_generate", status: done ? "succeeded" : "running",
          result_root_id: done ? rootId : null, error: null, error_kind: null,
          progress: { phase: "planning" },
          created_at: now, updated_at: now,
        }),
      });
    });

    const dialog = await openAddLessonDialog(page, rootId);
    const treeCallsBefore = api.counts.tree;

    await dialog.getByTestId("add-lesson-brief").fill(BRIEF);
    await dialog.getByTestId("add-lesson-generate").click();
    await expect(dialog.getByTestId("add-lesson-status")).toBeVisible({ timeout: 10_000 });

    // THE WAY OUT IS OPEN while the job runs. The dialog's X is the control;
    // a disabled one here would pin him to a ten-minute wait for work that is
    // not even happening in his browser.
    const close = dialog.locator('[data-slot="dialog-close"]').first();
    await expect(close).toBeEnabled();
    await close.click();
    await expect(dialog).toBeHidden();

    // …and the job it started keeps going: the poll survives the close, and
    // the tree is refetched when the lesson lands, which is what puts the new
    // `queued` row (and the progress bar) on the board.
    await expect
      .poll(() => api.counts.tree, { timeout: 25_000 })
      .toBeGreaterThan(treeCallsBefore);
    expect(polls).toBeGreaterThanOrEqual(5);
    expect(generateCalls).toBe(1);
    expect(api.unexpected).toEqual([]);
  });

  test("a failure after he closed the dialog comes back and finds him", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonIds = [randomUUID(), randomUUID()];
    const jobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonIds);

    let polls = 0;

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${moduleId}/lessons/generate` && req.method() === "POST") {
        await route.fulfill({
          status: 202, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ job_id: jobId, status: "pending" }),
        });
        return;
      }
      await route.fulfill({
        status: 500, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
    });

    await page.route(`${API_ORIGIN}/jobs/**`, async (route: Route) => {
      const req = route.request();
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      polls++;
      // The failure the tutor is MOST likely to hit and the only one he can
      // act on himself: no key. `error_kind: "auth"` is the taxonomy the API
      // maintains so this never reaches him as the runner's English prose.
      const failed = polls >= 3;
      const now = new Date().toISOString();
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: jobId, kind: "lesson_generate", status: failed ? "failed" : "running",
          result_root_id: null,
          error: failed ? "No API key is configured. Open Settings, add your key." : null,
          error_kind: failed ? "auth" : null,
          progress: { phase: "planning" },
          created_at: now, updated_at: now,
        }),
      });
    });

    const dialog = await openAddLessonDialog(page, rootId);

    await dialog.getByTestId("add-lesson-brief").fill(BRIEF);
    await dialog.getByTestId("add-lesson-generate").click();
    await expect(dialog.getByTestId("add-lesson-status")).toBeVisible({ timeout: 10_000 });

    await dialog.locator('[data-slot="dialog-close"]').first().click();
    await expect(dialog).toBeHidden();

    // IT COMES BACK WITH THE BAD NEWS. An error painted into a closed dialog
    // is an error nobody reads — and this one needs him to go and do
    // something (paste a key), so silence would cost him the lesson twice.
    await expect(dialog).toBeVisible({ timeout: 25_000 });
    const error = dialog.getByTestId("add-lesson-error");
    // The localized `auth` sentence from `jobErrors`, not the job's English
    // `error` string.
    await expect(error).toContainText("κλειδί API");
    await expect(error).not.toContainText("No API key");
    // And it is a form again, not a frozen spinner: he can retry right here.
    await expect(dialog.getByTestId("add-lesson-status")).toHaveCount(0);
    await expect(dialog.getByTestId("add-lesson-generate")).toBeEnabled();
    // The brief he wrote is still in the box — retyping it would be the second
    // punishment for someone else's missing key.
    await expect(dialog.getByTestId("add-lesson-brief")).toHaveValue(BRIEF);

    expect(api.unexpected).toEqual([]);
  });
});
