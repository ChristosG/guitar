import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Task 2.5 — «AI στο μάθημα»: the lesson row's one AI door now OPENS something.
//
// What this spec pins down is the whole transport, end to end, on the one
// lesson the tutor pointed at:
//
//  1. SCOPE. The row button opens a panel that says WHICH lesson it is about.
//     A panel that looked the same for every row would be a trap — he would
//     type an instruction meant for «Μπράτσο» and watch another lesson change.
//  2. THE HAND-EDITED STRIP. The sections he wrote himself are named at the
//     top, because they are the reason the «Ενημέρωσε τις υπόλοιπες ενότητες»
//     chip exists at all, and the chip is the fastest real instruction here.
//  3. THE JOB. «Φτιάξε πλάνο» POSTs once, gets a 202 back, polls `GET
//     /jobs/{id}` through a `running` tick, and only then shows the card. The
//     planner runs `claude -p` over the lesson and its sources — minutes, not
//     seconds — so an in-request answer would die at the edge (the same
//     reasoning `revise-async.spec.ts` documents for the drawer's turn).
//     ONE POST: a second one would be a 409 `lesson_busy` from the API, so a
//     double-fire here is a real bug, not a cosmetic one.
//
// Same route-interception convention as every other cockpit spec next door
// (`tutor-edited.spec.ts`, `revise-async.spec.ts`): anchored to `API_ORIGIN`
// rather than a bare suffix glob, explicit CORS headers plus OPTIONS handling
// because these are cross-origin credentialed calls even under interception,
// and an "unexpected request -> 500" catch-all so a routing mistake fails
// loudly instead of quietly hanging.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

/** The eight sections a blueprint-shaped lesson actually has, in order. The
 * panel's strip is driven by `meta.tutor_edited`, so only ONE of them (theory)
 * carries it — the test asserts exactly one chip, which is the assertion that
 * would catch a strip that listed every section instead. */
const SECTIONS = [
  ["warm_up", "Ζέσταμα"],
  ["theory", "Θεωρία"],
  ["exercises", "Ασκήσεις"],
  ["repertoire", "Ρεπερτόριο"],
  ["technique", "Τεχνική"],
  ["ear", "Ακουστική"],
  ["homework", "Για το σπίτι"],
  ["recap", "Ανακεφαλαίωση"],
] as const;

/** A Greek course -> module -> one READY lesson -> eight segments, `theory`
 * hand-edited. Greek on purpose: `el` is the product's default locale and the
 * strings this task adds are read there first. */
function tree(rootId: string, moduleId: string, lessonId: string) {
  return {
    id: rootId, kind: "course", title: "Ήχος", body: null, est_minutes: null,
    order: 0, language: "el", plane: "content", student_id: null, meta: {},
    children: [{
      id: moduleId, kind: "module", title: "Ξύλα", body: null, est_minutes: null,
      order: 0, language: "el", plane: "content", student_id: null, meta: {},
      children: [{
        id: lessonId, kind: "lesson", title: "Μπράτσο", body: "Περίληψη",
        est_minutes: 50, order: 0, language: "el", plane: "content", student_id: null,
        meta: { draft_status: "ready", word_count: 2400, target_words: 2750, meets_floor: true },
        children: SECTIONS.map(([section, title], i) => ({
          id: randomUUID(), kind: "segment", title, body: `Κείμενο για ${title}.`,
          est_minutes: 6, order: i, language: "el", plane: "content", student_id: null,
          meta: section === "theory"
            ? {
              section,
              tutor_edited: {
                at: "2026-09-12T09:00:00Z",
                prev_body: "Παλιό κείμενο για Θεωρία.",
                count: 1,
              },
            }
            : { section },
          children: [],
        })),
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

/** The plan the job hands back on `progress.plan` — the shape
 * `curriculum/lesson_ai.py` actually produces: a summary the tutor reads
 * first, one row per section with a `keep`/`rewrite` verdict, and the impact
 * numbers. `theory` is `keep` BECAUSE he edited it, which is the behaviour
 * the whole tutor-edited strip exists to make visible. */
function plan() {
  return {
    summary: "Θα ξαναγράψω 3 ενότητες.",
    note_to_tutor: "",
    dropped: [],
    impact: { rewrite_count: 3, est_words: 900 },
    sections: SECTIONS.map(([section, title]) => {
      if (section === "theory") {
        return { section, title, action: "keep", reason: "δική σου", brief: "", tutor_edited: true };
      }
      if (section === "warm_up") {
        return {
          section, title, action: "rewrite", reason: "αναφέρει παλιά θεωρία",
          brief: "πες για τη Strat", tutor_edited: false,
        };
      }
      if (section === "exercises" || section === "recap") {
        return {
          section, title, action: "rewrite", reason: "πρέπει να ακολουθεί τη θεωρία",
          brief: "ευθυγράμμισε με τη νέα θεωρία", tutor_edited: false,
        };
      }
      return { section, title, action: "keep", reason: "δεν επηρεάζεται", brief: "", tutor_edited: false };
    }),
  };
}

async function mockCurriculaApi(page: Page, rootId: string, moduleId: string, lessonId: string, sessionId: string) {
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
    if (pathname === `/curricula/${rootId}` && method === "GET") return json(tree(rootId, moduleId, lessonId));

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });

  return { unexpected };
}

test.describe("«AI στο μάθημα» panel — scope, the tutor-edited strip, plan-through-a-job (mocked API)", () => {
  test("the panel opens scoped to the lesson, plans via a job and shows the card", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonId = randomUUID();
    const sessionId = randomUUID();
    const jobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonId, sessionId);

    let planCalls = 0;
    let polls = 0;
    const instructions: string[] = [];

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${lessonId}/ai/plan` && req.method() === "POST") {
        planCalls++;
        instructions.push((req.postDataJSON() as { instruction: string }).instruction);
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
      const now = new Date().toISOString();
      // The FIRST poll is deliberately `running`: the panel must keep polling
      // rather than read the first response as the answer.
      const running = polls < 2;
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: jobId, kind: "lesson_ai", status: running ? "running" : "succeeded",
          result_root_id: null, error: null, error_kind: null,
          progress: running ? { phase: "planning" } : { phase: "done", plan: plan() },
          created_at: now, updated_at: now,
        }),
      });
    });

    await page.goto(`/el/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // Only the root opens expanded, so the module has to be opened before its
    // lesson row exists at all.
    await page
      .locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle")
      .click();

    await page.getByTestId("lesson-ai").first().click();

    const panel = page.getByTestId("lesson-ai-panel");
    await expect(panel).toBeVisible();
    // WHICH lesson — the header names it, and the module it sits in.
    await expect(panel).toContainText("Μπράτσο");
    await expect(panel).toContainText("Ξύλα");

    // Exactly one section was hand-edited, so exactly one chip.
    await expect(panel.getByTestId("lesson-ai-edited-chip")).toHaveCount(1);
    await expect(panel.getByTestId("lesson-ai-edited-chip")).toContainText("Θεωρία");

    // The chip that only exists BECAUSE something was hand-edited fills the box.
    await panel.getByTestId("lesson-ai-chip-propagate").click();
    await expect(panel.getByTestId("lesson-ai-instruction")).toHaveValue(/Ενημέρωσε/);

    await panel.getByTestId("lesson-ai-plan").click();
    await expect(panel.getByTestId("lesson-ai-status")).toBeVisible();

    await expect(panel.getByTestId("lesson-plan-card")).toContainText("Θα ξαναγράψω 3 ενότητες.");
    // The status goes away once the card is there — a panel that kept saying
    // "φτιάχνω πλάνο…" under a finished plan would be lying.
    await expect(panel.getByTestId("lesson-ai-status")).toHaveCount(0);
    await expect(panel.getByTestId("lesson-ai-error")).toHaveCount(0);

    expect(planCalls).toBe(1);
    expect(instructions[0]).toContain("Ενημέρωσε");
    // It polled — the 202 was not mistaken for the answer.
    expect(polls).toBeGreaterThanOrEqual(2);
    expect(api.unexpected).toEqual([]);
  });
});
