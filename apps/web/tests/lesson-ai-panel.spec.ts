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
//  4. THE CARD (Task 2.6). The plan is a PROPOSAL he edits: ticks he can undo,
//     a brief per section, a note. What the apply POSTs must be what is ticked
//     at the moment he presses it — a card that sent the planner's original
//     verdicts would rewrite a section he had just untick-ed, on a lesson he
//     curated by hand. And a re-plan must genuinely replace the card, note box
//     included, rather than paint a new summary over the old ticks.
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

/** The sections the apply rewrites, and the only two the card's ticks should
 * end up sending. Named once so the POST-body assertion and the post-apply
 * tree cannot drift apart. */
const APPLIED = ["warm_up", "exercises"];

/** A Greek course -> module -> one READY lesson -> eight segments, `theory`
 * hand-edited. Greek on purpose: `el` is the product's default locale and the
 * strings this task adds are read there first.
 *
 * `applied` is the SAME tree after the apply job finished: the two rewritten
 * sections carry new text, and the lesson carries the `prev_segments` snapshot
 * plus the `ai_instruction` stamp the API writes — which is exactly what the
 * done state's «Τι άλλαξε;» reads. The refetch after an apply is the only way
 * the panel ever sees it, so the mock has to change underneath the page the
 * way the real API does. */
function tree(rootId: string, moduleId: string, lessonId: string, applied = false) {
  return {
    id: rootId, kind: "course", title: "Ήχος", body: null, est_minutes: null,
    order: 0, language: "el", plane: "content", student_id: null, meta: {},
    children: [{
      id: moduleId, kind: "module", title: "Ξύλα", body: null, est_minutes: null,
      order: 0, language: "el", plane: "content", student_id: null, meta: {},
      children: [{
        id: lessonId, kind: "lesson", title: "Μπράτσο", body: "Περίληψη",
        est_minutes: 50, order: 0, language: "el", plane: "content", student_id: null,
        meta: applied
          ? {
            draft_status: "ready", word_count: 2000, target_words: 2750, meets_floor: false,
            ai_instruction: "Ενημέρωσε τις υπόλοιπες ενότητες με βάση τις αλλαγές μου",
            prev_segments: SECTIONS.map(([section, title]) => ({
              section, title, body: `Κείμενο για ${title}.`,
            })),
          }
          : { draft_status: "ready", word_count: 2400, target_words: 2750, meets_floor: true },
        children: SECTIONS.map(([section, title], i) => ({
          id: randomUUID(), kind: "segment", title,
          body: applied && APPLIED.includes(section)
            ? `Νέο κείμενο για ${title}.`
            : `Κείμενο για ${title}.`,
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

/** What the planner hands back on a RE-PLAN with «χωρίς ασκήσεις» — a smaller
 * plan, so the card visibly becomes a different card rather than keeping the
 * first one's ticks under a new summary. */
function narrowerPlan() {
  return {
    summary: "Θα ξαναγράψω 1 ενότητα.",
    note_to_tutor: "Άφησα τις ασκήσεις όπως ήταν, όπως ζήτησες.",
    dropped: [],
    impact: { rewrite_count: 1, est_words: 300 },
    sections: SECTIONS.map(([section, title]) => ({
      section, title,
      action: section === "warm_up" ? "rewrite" : "keep",
      reason: section === "warm_up" ? "αναφέρει παλιά θεωρία" : "δεν επηρεάζεται",
      brief: section === "warm_up" ? "πες για τη Strat" : "",
      tutor_edited: section === "theory",
    })),
  };
}

async function mockCurriculaApi(page: Page, rootId: string, moduleId: string, lessonId: string, sessionId: string) {
  const unexpected: string[] = [];
  // Flipped by the apply job's poll, so every refetch AFTER the apply sees the
  // post-apply tree — the same ordering the real API gives the page.
  const state = { applied: false };

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
      return json(tree(rootId, moduleId, lessonId, state.applied));
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });

  return { unexpected, state };
}

test.describe("«AI στο μάθημα» panel — scope, the tutor-edited strip, plan-through-a-job (mocked API)", () => {
  test("the panel opens scoped to the lesson, plans via a job and shows the card", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonId = randomUUID();
    const sessionId = randomUUID();
    const jobId = randomUUID();
    const applyJobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonId, sessionId);

    let planCalls = 0;
    let applyCalls = 0;
    let applyPolls = 0;
    let polls = 0;
    const instructions: string[] = [];
    let applyBody: { instruction: string; note?: string; sections: { section: string; brief: string }[] } | null = null;

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
      if (pathname === `/blocks/${lessonId}/ai/apply` && req.method() === "POST") {
        applyCalls++;
        applyBody = req.postDataJSON() as typeof applyBody;
        await route.fulfill({
          status: 202, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ job_id: applyJobId, status: "pending" }),
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
      const { pathname } = new URL(req.url());
      const now = new Date().toISOString();

      if (pathname === `/jobs/${applyJobId}`) {
        applyPolls++;
        // Two `running` ticks on purpose: the rewrite takes minutes in real
        // life, and the seconds this buys the test are what the "the card
        // survives the wait" assertions below run inside.
        const stillGoing = applyPolls < 3;
        // The lesson's segments are replaced the moment this job reports done,
        // so the tree the page refetches next must already be the new one.
        if (!stillGoing) api.state.applied = true;
        await route.fulfill({
          status: 200, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            id: applyJobId, kind: "lesson_ai", status: stillGoing ? "running" : "succeeded",
            result_root_id: null, error: null, error_kind: null,
            progress: stillGoing
              ? { phase: "rewriting" }
              : { phase: "done", rewritten: APPLIED, word_count: 2000 },
            created_at: now, updated_at: now,
          }),
        });
        return;
      }

      polls++;
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
    // AND IT SAYS HOW LONG IT HAS BEEN (Task 4.3). The tutor's first live
    // afternoon had an apply run 19 minutes; a spinner with no clock on it is
    // indistinguishable from a hung one, and the panel now waits 40 minutes
    // rather than 10, so the wait has to account for itself out loud.
    await expect(panel.getByTestId("lesson-ai-elapsed")).toContainText("λεπτά");

    await expect(panel.getByTestId("lesson-plan-card")).toContainText("Θα ξαναγράψω 3 ενότητες.");
    // The status goes away once the card is there — a panel that kept saying
    // "φτιάχνω πλάνο…" under a finished plan would be lying.
    await expect(panel.getByTestId("lesson-ai-status")).toHaveCount(0);
    await expect(panel.getByTestId("lesson-ai-error")).toHaveCount(0);

    // THE CARD. The planner's verdicts arrive as the ticks: `rewrite` sections
    // start ticked, `keep` sections do not — and `theory`, which he wrote
    // himself, is one of the ones left alone.
    const card = panel.getByTestId("lesson-plan-card");
    await expect(card.getByTestId("lesson-plan-check-warm_up")).toBeChecked();
    await expect(card.getByTestId("lesson-plan-check-theory")).not.toBeChecked();
    await expect(card.getByTestId("lesson-plan-row-theory")).toContainText("δική σου");
    // The impact line counts HIS ticks, not the planner's number — the moment
    // he unticks one, the sentence has to follow him.
    await expect(card.getByTestId("lesson-plan-impact")).toContainText("3");
    await card.getByTestId("lesson-plan-check-recap").uncheck();
    await expect(card.getByTestId("lesson-plan-impact")).toContainText("2");
    // An unticked section has no brief box — there is nothing to brief.
    await expect(card.getByTestId("lesson-plan-brief-recap")).toHaveCount(0);
    await expect(card.getByTestId("lesson-plan-brief-warm_up")).toHaveValue("πες για τη Strat");

    await card.getByTestId("lesson-plan-note").fill("και πιο σύντομα");
    await card.getByTestId("lesson-ai-apply").click();

    // THE CARD SURVIVES THE WAIT. An apply that fails comes back to this very
    // card, so unmounting it while the job runs would silently throw away the
    // ticks, the briefs and the note he just spent a minute on. It stays —
    // with both buttons dead, because a second POST would be a 409.
    await expect(card).toBeVisible();
    await expect(card.getByTestId("lesson-ai-apply")).toBeDisabled();
    await expect(card.getByTestId("lesson-ai-replan")).toBeDisabled();
    await expect(card.getByTestId("lesson-plan-check-recap")).not.toBeChecked();
    await expect(card.getByTestId("lesson-plan-note")).toHaveValue("και πιο σύντομα");

    await expect(panel.getByTestId("lesson-ai-done")).toBeVisible();
    // The refetched tree carries `prev_segments`, so the way back is on screen.
    await expect(panel.getByTestId("lesson-what-changed-trigger")).toBeVisible();
    await expect(panel.getByTestId("lesson-ai-error")).toHaveCount(0);

    expect(planCalls).toBe(1);
    expect(instructions[0]).toContain("Ενημέρωσε");
    // It polled — the 202 was not mistaken for the answer.
    expect(polls).toBeGreaterThanOrEqual(2);

    // EXACTLY the two sections still ticked, with their briefs, and his note.
    // Sending `recap` here would rewrite a section he explicitly untick-ed.
    expect(applyCalls).toBe(1);
    const sent = applyBody as unknown as {
      note?: string;
      sections: { section: string; brief: string }[];
    };
    expect(sent.sections.map((s) => s.section)).toEqual(APPLIED);
    expect(sent.sections.find((s) => s.section === "warm_up")?.brief).toBe("πες για τη Strat");
    expect(sent.note).toBe("και πιο σύντομα");

    expect(api.unexpected).toEqual([]);
  });

  test("re-plan sends the note and replaces the card", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonId = randomUUID();
    const sessionId = randomUUID();
    const firstJobId = randomUUID();
    const secondJobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonId, sessionId);

    const planBodies: { instruction: string; note?: string }[] = [];

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${lessonId}/ai/plan` && req.method() === "POST") {
        planBodies.push(req.postDataJSON() as { instruction: string; note?: string });
        await route.fulfill({
          status: 202, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({
            job_id: planBodies.length === 1 ? firstJobId : secondJobId,
            status: "pending",
          }),
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
      const { pathname } = new URL(req.url());
      const second = pathname === `/jobs/${secondJobId}`;
      const now = new Date().toISOString();
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: second ? secondJobId : firstJobId, kind: "lesson_ai", status: "succeeded",
          result_root_id: null, error: null, error_kind: null,
          progress: { phase: "done", plan: second ? narrowerPlan() : plan() },
          created_at: now, updated_at: now,
        }),
      });
    });

    await page.goto(`/el/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();
    await page
      .locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle")
      .click();
    await page.getByTestId("lesson-ai").first().click();

    const panel = page.getByTestId("lesson-ai-panel");
    await panel.getByTestId("lesson-ai-chip-harder").click();
    await panel.getByTestId("lesson-ai-plan").click();

    const card = panel.getByTestId("lesson-plan-card");
    await expect(card).toContainText("Θα ξαναγράψω 3 ενότητες.");
    await expect(card.getByTestId("lesson-plan-impact")).toContainText("3");

    // «Δεν μου αρέσει» — the note box is the re-plan's argument, not the apply's.
    await card.getByTestId("lesson-plan-note").fill("χωρίς ασκήσεις");
    await card.getByTestId("lesson-ai-replan").click();

    // The SECOND plan replaces the first one, ticks and all: one section now.
    await expect(card).toContainText("Θα ξαναγράψω 1 ενότητα.");
    await expect(card.getByTestId("lesson-plan-impact")).toContainText("1");
    await expect(card.getByTestId("lesson-plan-check-exercises")).not.toBeChecked();
    await expect(card.getByTestId("lesson-plan-check-warm_up")).toBeChecked();
    // A fresh plan is a fresh card — the old note is not still in the box.
    await expect(card.getByTestId("lesson-plan-note")).toHaveValue("");

    expect(planBodies).toHaveLength(2);
    expect(planBodies[0].instruction).toContain("δύσκολες");
    expect(planBodies[0].note ?? "").toBe("");
    expect(planBodies[1].instruction).toContain("δύσκολες");
    expect(planBodies[1].note).toBe("χωρίς ασκήσεις");
    expect(api.unexpected).toEqual([]);
  });

  test("one unanswered poll does not kill the plan — the next one lands it", async ({ page }) => {
    // A plan is minutes of `claude -p`, and over minutes one `GET /jobs/{id}`
    // will occasionally not answer: the laptop slept, the wifi blinked, the
    // tunnel in front of the API recycled a connection. The JOB never noticed.
    // The panel used to read that single 502 as "this failed", show «κάτι πήγε
    // στραβά» over a plan that was one second away, and send him to a retry
    // that 409s `lesson_busy` — because the job it told him had failed is still
    // holding the lesson. So: a miss is a miss, and the next poll decides.
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lessonId = randomUUID();
    const sessionId = randomUUID();
    const jobId = randomUUID();

    const api = await mockCurriculaApi(page, rootId, moduleId, lessonId, sessionId);

    await page.route(`${API_ORIGIN}/blocks/**`, async (route: Route) => {
      const req = route.request();
      const { pathname } = new URL(req.url());
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      if (pathname === `/blocks/${lessonId}/ai/plan` && req.method() === "POST") {
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

    let polls = 0;
    await page.route(`${API_ORIGIN}/jobs/**`, async (route: Route) => {
      const req = route.request();
      if (req.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: CORS_HEADERS });
        return;
      }
      polls++;
      // Poll 1: the blink. Poll 2: the job was done all along.
      if (polls === 1) {
        await route.fulfill({
          status: 502, contentType: "application/json", headers: CORS_HEADERS,
          body: JSON.stringify({ detail: "bad gateway" }),
        });
        return;
      }
      const now = new Date().toISOString();
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({
          id: jobId, kind: "lesson_ai", status: "succeeded",
          result_root_id: null, error: null, error_kind: null,
          progress: { phase: "done", plan: plan() },
          created_at: now, updated_at: now,
        }),
      });
    });

    await page.goto(`/el/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();
    await page
      .locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle")
      .click();
    await page.getByTestId("lesson-ai").first().click();

    const panel = page.getByTestId("lesson-ai-panel");
    await panel.getByTestId("lesson-ai-chip-harder").click();
    await panel.getByTestId("lesson-ai-plan").click();

    // The card lands, and no error was ever shown on the way there.
    await expect(panel.getByTestId("lesson-plan-card")).toContainText("Θα ξαναγράψω 3 ενότητες.", {
      timeout: 15_000,
    });
    await expect(panel.getByTestId("lesson-ai-error")).toHaveCount(0);
    expect(polls).toBeGreaterThanOrEqual(2);
    expect(api.unexpected).toEqual([]);
  });
});
