import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Stage 6.9 — THE OUTLINE EDITOR AND THE BOARD.
//
// Chris: "i need more user engagement here. the man might want to change
// something. might need to extend a module, delete one or add one more. we have
// NOTHING of those bro."
//
// The assertion this whole file exists for is the one in "THE EDITED OUTLINE IS THE
// ONE THAT GETS BUILT": the tutor deletes a module, adds one, renames a lesson and
// reorders — and the dict that goes back to `POST .../answer` is HIS outline, not
// the model's. `_answer_outline` stores that dict verbatim and `materialize_outline`
// persists exactly it, so what leaves the browser here is what ends up in his
// database.
//
// Origin-anchored routes + explicit CORS/OPTIONS handling, same convention as
// `cockpit.spec.ts` (a bare `**/curricula` suffix glob is unsafe: this app's OWN
// `/en/curricula` page URL also ends in "/curricula").
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  // Must echo the app's real origin + `Allow-Credentials`, not "*": every call in
  // `lib/api.ts` is `credentials: "include"` (the auth slice), and a browser rejects
  // a wildcard-ACAO response to a credentialed request outright — the page then
  // renders its "could not load" state and the assertions below fail for a reason
  // that has nothing to do with what they check.
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

interface FixtureSource {
  id: string;
  title: string;
  type: string;
  char_count: number;
  default_selected: boolean;
}

interface OutlineLesson {
  title: string;
  objective: string;
  est_minutes: number;
}
interface OutlineModule {
  title: string;
  objective: string;
  tier: string;
  coverage_note: string;
  lessons: OutlineLesson[];
}
interface Outline {
  title: string;
  modules: OutlineModule[];
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
  meta: Record<string, unknown> | null;
  artifacts: unknown[];
  children: FixtureBlock[];
}

const SOURCES: FixtureSource[] = [
  { id: "book-1", title: "Getting Great Guitar Sounds", type: "pdf", char_count: 220_000, default_selected: true },
  { id: "spine-1", title: "Guitar Tone & Gear — Course Spine", type: "text", char_count: 16_141, default_selected: true },
];

/** The model's outline — the thing the tutor is about to disagree with. */
function makeOutline(): Outline {
  return {
    title: "Getting a Great Guitar Tone",
    modules: [
      {
        title: "Pedals & Effects",
        objective: "Shape tone with stompboxes.",
        tier: "library",
        coverage_note: "Covered by Getting Great Guitar Sounds, pp. 56-75.",
        lessons: [
          { title: "Overdrive", objective: "Drive the front end.", est_minutes: 50 },
          { title: "Delay", objective: "Time-based effects.", est_minutes: 50 },
        ],
      },
      {
        title: "Vibrato",
        objective: "Expressive pitch variation.",
        tier: "general_knowledge",
        coverage_note: "Not in your library — I would be writing this from what I know.",
        lessons: [{ title: "Hand vibrato", objective: "Wrist motion.", est_minutes: 50 }],
      },
    ],
  };
}

/** The materialized tree the board opens on — every lesson `queued`, not one word
 * drafted. That is the real post-confirm state: `materialize_outline` runs AT
 * confirm, and the draft fan-out fills these in afterwards. */
function makeTree(rootId: string, outline: Outline): FixtureBlock {
  const block = (over: Partial<FixtureBlock>): FixtureBlock => ({
    id: randomUUID(), kind: "module", title: "", body: null, est_minutes: null,
    order: 0, language: "en", plane: "content", student_id: null, meta: null,
    artifacts: [], children: [], ...over,
  });

  return block({
    id: rootId,
    kind: "course",
    title: outline.title,
    meta: {
      library: {
        token_count: 92_400, fits: true, full_context: true,
        sources: [{ ref: "S1", id: "book-1", title: SOURCES[0].title }],
      },
      shape: { target_words_per_lesson: 2200 },
    },
    children: outline.modules.map((m, mi) =>
      block({
        kind: "module",
        title: m.title,
        order: mi,
        meta: { tier: m.tier, coverage_note: m.coverage_note, objective: m.objective },
        children: m.lessons.map((l, li) =>
          block({
            kind: "lesson",
            title: l.title,
            order: li,
            est_minutes: l.est_minutes,
            meta: { draft_status: "queued", objective: l.objective },
          }),
        ),
      }),
    ),
  });
}

/** A drafted lesson, carrying the two things the tutor could never see before: a
 * word count, and page citations that deep-link into the Reader. */
function draftLesson(lesson: FixtureBlock): FixtureBlock {
  const citations = [
    { source_id: "book-1", source_ref: "S1", source_title: "Getting Great Guitar Sounds", page: 56 },
  ];
  return {
    ...lesson,
    meta: { ...(lesson.meta ?? {}), draft_status: "ready", word_count: 2340, meets_floor: true, citations },
    children: [
      {
        id: randomUUID(), kind: "segment", title: "Theory",
        body: "The overdrive circuit clips the signal...",
        est_minutes: 10, order: 0, language: "en", plane: "content", student_id: null,
        meta: { section: "theory", citations }, artifacts: [], children: [],
      },
    ],
  };
}

interface MockOptions {
  /** Report the first lesson as drafted on the VERY FIRST poll — the case where the
   * lessons finished between the tree fetch and the first poll. See the test at the
   * bottom of this file: the board must not sit there showing "queued" rows under a
   * progress bar that says they are done. */
  readyFromStart?: boolean;
}

async function mockInterviewApi(page: Page, { readyFromStart = false }: MockOptions = {}) {
  const interviewId = randomUUID();
  const rootId = randomUUID();

  let step = "who";
  let currentOutline: Outline = makeOutline();
  let tree: FixtureBlock | null = null;
  let polls = 0;
  let resumed = 0;
  // Flipped by the test when it wants the remaining lessons to land. Deterministic,
  // unlike counting polls: Next's dev-mode StrictMode double-mounts effects, so a
  // poll COUNT is not a clock.
  let finished = false;

  const calls = { start: 0, answer: 0, get: 0, progress: 0, artifacts: 0 };
  const answerBodies: unknown[] = [];
  const deepened: string[] = [];
  const refinements: string[] = [];
  const unexpected: string[] = [];

  function findBlock(node: FixtureBlock | null, id: string): FixtureBlock | null {
    if (!node) return null;
    if (node.id === id) return node;
    for (const child of node.children) {
      const hit = findBlock(child, id);
      if (hit) return hit;
    }
    return null;
  }

  const state = (over: Record<string, unknown>) => ({
    interview_id: interviewId, options: null, findings: null, error: null, ...over,
  });

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname, search } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (body: unknown, status = 200) =>
      route.fulfill({
        status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body),
      });

    // THE N+1 TRIPWIRE. Every segment leaf used to fire one of these — ~120 in
    // parallel on a single board render, which IS the "Could not load attached
    // artifacts" error. Counted here, asserted to be zero below.
    if (pathname === "/artifacts" && search.includes("block_id")) {
      calls.artifacts++;
      return json([]);
    }

    if (pathname === "/curricula" && method === "GET") return json([]);

    if (pathname === "/curricula/interview" && method === "POST") {
      calls.start++;
      step = "who";
      return json(
        state({
          step: "who",
          question: "Who is this curriculum for?",
          options: [{ value: "none", label: "No particular student", kind: "none" }],
          findings: { levels: ["all_levels", "beginner", "intermediate", "advanced"] },
        }),
        201,
      );
    }

    if (pathname.match(/^\/curricula\/interview\/[^/]+\/answer$/) && method === "POST") {
      calls.answer++;
      const answer = (req.postDataJSON() as { answer: Record<string, unknown> }).answer;
      answerBodies.push(answer);

      if (step === "who") {
        step = "duration";
        return json(state({ step: "duration", question: "How long does this run?" }));
      }
      if (step === "duration") {
        step = "scope";
        return json(
          state({
            step: "scope",
            question: "What is this course FOR?",
            options: [
              { value: "library_only", label: "Only my library." },
              { value: "general_knowledge", label: "Fill the gaps from general knowledge." },
            ],
          }),
        );
      }
      if (step === "scope") {
        step = "sources";
        return json(
          state({
            step: "sources",
            question: "Which sources?",
            options: SOURCES.map((s) => ({
              value: s.id, label: s.title, type: s.type,
              char_count: s.char_count, default_selected: s.default_selected,
            })),
            findings: { shape: "3 sessions -> 1 module x 3 lessons -> ~2,200 words each" },
          }),
        );
      }
      if (step === "sources") {
        // EXACTLY WHAT THE REAL API DOES: it advances to "outline" with NOTHING in
        // `findings`. Generating the outline is the expensive call and it belongs to
        // the step that shows it, so it only happens on that step's own
        // `{regenerate: true}` answer — which the dialog fires the moment it lands
        // here. If it ever stops doing that, the tutor gets an empty editor, and
        // this mock is what catches it.
        step = "outline";
        return json(state({ step: "outline", question: "Here is the course.", findings: null }));
      }
      if (step === "outline") {
        if (answer.regenerate === true) {
          return json(state({ step: "outline", question: "Here is the course.", findings: currentOutline }));
        }
        // THE EDITED OUTLINE REPLACES THE MODEL'S, exactly as `_answer_outline` does.
        currentOutline = answer.outline as Outline;
        step = "confirm";
        return json(state({ step: "confirm", question: "Ready?", findings: currentOutline }));
      }
      // confirm: MATERIALIZE, then 202 with BOTH ids — the tree already exists.
      tree = makeTree(rootId, currentOutline);
      return json({ job_id: randomUUID(), root_id: rootId, status: "pending" }, 202);
    }

    const progressMatch = pathname.match(/^\/curricula\/([^/]+)\/progress$/);
    if (progressMatch && method === "GET") {
      calls.progress++;
      polls++;
      const total = tree ? tree.children.flatMap((m) => m.children).length : 0;

      // THE DRAFT, MOCKED HONESTLY, ON THE REAL TIMELINE:
      //   poll 1        nothing written yet — the tree the board opened on is all
      //                 `queued`, because materialization happens at confirm and
      //                 the fan-out has not produced anything yet.
      //   poll 2..      module 1's first lesson has landed. The other two are still
      //                 queued, AND HE CAN ALREADY READ IT. That is the claim.
      //   after finish() everything is drafted and the polling parks itself.
      const ready = total === 0 ? 0 : finished ? total : polls > 1 || readyFromStart ? 1 : 0;
      if (tree && ready > 0) {
        const all = ready === total;
        tree = {
          ...tree,
          children: tree.children.map((m) => ({
            ...m,
            children: m.children.map((l, i) => {
              // ALREADY DRAFTED LESSONS ARE LEFT ALONE. Re-deriving them on every
              // poll would hand back fresh segment ids and a fresh body every 2
              // seconds — quietly undoing an Extend-with-chat the tutor just made,
              // which is exactly what the real API does NOT do (the refine is
              // persisted; the next tree fetch returns the rewritten prose).
              if (l.meta?.draft_status === "ready") return l;
              return all || (m.order === 0 && i === 0) ? draftLesson(l) : l;
            }),
          })),
        };
      }
      return json({
        root_id: progressMatch[1], total, queued: total - ready, drafting: 0,
        ready, failed: 0, done: total > 0 && ready === total,
      });
    }

    if (pathname.match(/^\/curricula\/[^/]+\/draft$/) && method === "POST") {
      resumed++;
      return json({ job_id: randomUUID(), status: "pending" }, 202);
    }

    const deepenMatch = pathname.match(/^\/blocks\/([^/]+)\/deepen$/);
    if (deepenMatch && method === "POST") {
      deepened.push(deepenMatch[1]);
      return json({ job_id: randomUUID(), status: "pending" }, 202);
    }

    // EXTEND WITH CHAT: the API rewrites the block and stashes `prev_body` for the
    // Undo, and answers with the refreshed block. This mock returns exactly that
    // shape, so the board's optimistic replace is exercised against the real one.
    const refineMatch = pathname.match(/^\/blocks\/([^/]+)\/refine$/);
    if (refineMatch && method === "POST") {
      const { instruction } = req.postDataJSON() as { instruction: string };
      refinements.push(instruction);
      const found = findBlock(tree, refineMatch[1]);
      if (!found) return json({ detail: "not found" }, 404);
      // Persisted, like the real PATCH: `prev_body` is stashed on the block itself,
      // so the Undo survives a reload and the next tree fetch returns the rewrite.
      found.meta = { ...(found.meta ?? {}), prev_body: found.body, refined: true };
      found.body = `REWRITTEN: ${instruction}`;
      return json(found);
    }

    const getMatch = pathname.match(/^\/curricula\/([^/]+)$/);
    if (getMatch && method === "GET") {
      calls.get++;
      return tree && tree.id === getMatch[1] ? json(tree) : json({ detail: "not found" }, 404);
    }

    unexpected.push(`${method} ${pathname}`);
    return json({ detail: "unmocked request in test" }, 500);
  }

  await page.route(`${API_ORIGIN}/curricula`, handler);
  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/blocks/**`, handler);
  await page.route(`${API_ORIGIN}/artifacts**`, handler);

  return {
    calls, answerBodies, unexpected, deepened, refinements,
    resumeCount: () => resumed, rootId,
    finishDraft: () => {
      finished = true;
    },
  };
}

/** Drive the interview up to (not through) `target`. */
async function startToStep(
  page: Page,
  target: "who" | "duration" | "scope" | "sources" | "outline" | "confirm",
) {
  await page.goto("/en/curricula");
  await page.getByTestId("curricula-generate-button").click();
  await expect(page.getByTestId("interview-dialog")).toBeVisible();
  await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
  await page.getByTestId("interview-start-submit").click();
  if (target === "who") return;

  await page.getByTestId("interview-answer-submit").click(); // who: no student, all levels
  if (target === "duration") return;

  await page.getByTestId("interview-duration-weeks").fill("3");
  await page.getByTestId("interview-duration-minutes").fill("60");
  await page.getByTestId("interview-answer-submit").click();
  if (target === "scope") return;

  await page.getByTestId("interview-scope-brief").fill("Get him a usable live tone.");
  await page.getByTestId("interview-answer-submit").click();
  if (target === "sources") return;

  await page.getByTestId("interview-answer-submit").click(); // sources: accept the default selection
  await expect(page.getByTestId("outline-editor")).toBeVisible();
  if (target === "outline") return;

  await page.getByTestId("interview-answer-submit").click(); // outline: accept as-is
  await expect(page.getByTestId("interview-confirm-heading")).toBeVisible();
}

test.describe("the guided interview, v2 (mocked API)", () => {
  test("the student is OPTIONAL, and the level selector stands in for him", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "who");

    // Chris: "this has to be optional dude.. the student part here has to be
    // TOTALLY optional." So it is an option with its own button, not a blank field.
    await expect(page.getByTestId("interview-who-option-none")).toBeVisible();
    await expect(page.getByTestId("interview-who-levels")).toBeVisible();

    await page.getByTestId("interview-who-level-beginner").click();
    await page.getByTestId("interview-answer-submit").click();

    expect(mock.answerBodies[0]).toEqual({ student_id: null, level: "beginner" });
    await expect(page.getByTestId("interview-duration-weeks")).toBeVisible();
  });

  test("the scope step asks what the course is FOR, and what to do about the gaps", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "scope");

    await page.getByTestId("interview-scope-brief").fill("Get him a usable live tone by week 3.");
    await page.getByTestId("interview-policy-library_only").check();
    await page.getByTestId("interview-answer-submit").click();

    expect(mock.answerBodies.at(-1)).toEqual({
      brief: "Get him a usable live tone by week 3.",
      gap_policy: "library_only",
    });
  });

  test("the sources step echoes the derived shape and lets him drop the filler source", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "sources");

    // He is agreeing to a SIZE before anyone spends his money on it.
    await expect(page.getByTestId("interview-shape-echo")).toContainText("~2,200 words each");

    await expect(page.getByTestId("interview-source-spine-1")).toBeChecked();
    await page.getByTestId("interview-source-spine-1").uncheck();
    await page.getByTestId("interview-answer-submit").click();

    const answer = mock.answerBodies.at(-1) as { source_ids: string[] };
    expect(answer.source_ids).toEqual(["book-1"]);
  });

  test("every module carries a TIER — the standing answer to 'what if my library says nothing?'", async ({ page }) => {
    await mockInterviewApi(page);
    await startToStep(page, "outline");

    const badges = page.getByTestId("outline-editor").getByTestId("tier-badge");
    await expect(badges).toHaveCount(2);
    await expect(badges.nth(0)).toHaveAttribute("data-tier", "library");
    await expect(badges.nth(1)).toHaveAttribute("data-tier", "general_knowledge");

    // And the grounding panel says WHY, in the model's own words, having read the
    // whole book — not a cosine score with a 0.021 separation margin.
    await expect(page.getByTestId("outline-module-coverage-0")).toContainText("pp. 56-75");
    await expect(page.getByTestId("outline-module-coverage-1")).toContainText("Not in your library");
  });

  test("the live footer counts modules, lessons, words and dollars while he edits", async ({ page }) => {
    await mockInterviewApi(page);
    await startToStep(page, "outline");

    // A 50-minute lesson is 40 taught minutes + 10 of Q&A; 40 x 55 words/min =
    // 2,200 words. Three of them: 6,600 words, 3 x $0.12 = $0.36. The same
    // constants `depth.py`/`shape.py` draft against — not a second set.
    const footer = page.getByTestId("outline-footer-summary");
    await expect(footer).toContainText("2 modules");
    await expect(footer).toContainText("3 lessons");
    await expect(footer).toContainText("6,600");
    await expect(footer).toContainText("$0.36");

    // Delete a module -> the money moves. That is why the number is there at all.
    await page.getByTestId("outline-module-delete-1").click();
    await page.getByTestId("confirm-accept").click();
    await expect(footer).toContainText("1 module");
    await expect(footer).toContainText("4,400");
    await expect(footer).toContainText("$0.24");
  });

  test("THE EDITED OUTLINE IS THE ONE THAT GETS BUILT", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "outline");

    // 1. DELETE the module his library does not cover.
    await page.getByTestId("outline-module-delete-1").click();
    await expect(page.getByTestId("confirm-title")).toContainText("Vibrato");
    await page.getByTestId("confirm-accept").click();
    await expect(page.getByTestId("outline-module")).toHaveCount(1);

    // 2. ADD one of his own, and send it to the web.
    await page.getByTestId("outline-add-module").click();
    await page.getByTestId("outline-module-title-1").fill("Modern Amp Modellers");
    await page.getByTestId("outline-module-tier-1").selectOption("web");
    await page.getByTestId("outline-add-lesson-1").click();
    await page.getByTestId("outline-lesson-title-1-0").fill("Kemper vs Quad Cortex");

    // 3. RENAME a lesson, and give another one a longer slot.
    await page.getByTestId("outline-lesson-title-0-0").fill("Overdrive & the Tube Screamer");
    await page.getByTestId("outline-lesson-minutes-0-1").fill("90");

    // 4. REORDER — up/down buttons, not drag-and-drop.
    await page.getByTestId("outline-lesson-down-0-0").click();

    await page.getByTestId("interview-answer-submit").click();
    await expect(page.getByTestId("interview-confirm-heading")).toBeVisible();

    // What went over the wire is HIS course. `_answer_outline` stores this dict
    // verbatim, and `materialize_outline` builds exactly it.
    const sent = (mock.answerBodies.at(-1) as { outline: Outline }).outline;
    expect(sent.modules).toHaveLength(2);

    expect(sent.modules[0].title).toBe("Pedals & Effects");
    expect(sent.modules[0].lessons.map((l) => l.title)).toEqual([
      "Delay",
      "Overdrive & the Tube Screamer",
    ]);
    // The reorder carried the 90-minute edit with the lesson it was made on.
    expect(sent.modules[0].lessons[0].est_minutes).toBe(90);

    expect(sent.modules[1].title).toBe("Modern Amp Modellers");
    expect(sent.modules[1].tier).toBe("web");
    expect(sent.modules[1].lessons).toEqual([
      { title: "Kemper vs Quad Cortex", objective: "", est_minutes: 50 },
    ]);

    // And the deleted module is GONE — not hidden, not disabled. Gone.
    expect(sent.modules.map((m) => m.title)).not.toContain("Vibrato");
    expect(mock.unexpected).toEqual([]);
  });

  test("confirm materializes the tree and the board opens on it INSTANTLY — no waiting room", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");

    await page.getByTestId("interview-confirm-submit").click();

    // No job-poll spinner. The dialog closes and the board is already there, on a
    // real curriculum, with every lesson queued.
    await expect(page.getByTestId("interview-dialog")).toBeHidden();
    await expect(page.getByTestId("tree-board")).toBeVisible();
    await expect(page.getByTestId("draft-progress")).toBeVisible();
    expect(mock.answerBodies.at(-1)).toEqual({ approved: true });

    // He reads module 1 while the rest is still being written. THE FLAGSHIP CLAIM.
    await expect(page.getByTestId("lesson-status").first()).toHaveAttribute("data-status", "ready");
    await expect(page.getByTestId("lesson-word-count").first()).toContainText("2,340");
    await expect(page.getByTestId("draft-progress")).toHaveAttribute("data-done", "false");

    // ...and then the rest lands, without a reload.
    mock.finishDraft();
    await expect(page.getByTestId("draft-progress")).toHaveAttribute("data-done", "true", { timeout: 15_000 });
    await expect(page.getByTestId("draft-progress-count")).toContainText("3 of 3");
  });

  test("THE BOARD MAKES ZERO /artifacts?block_id= REQUESTS", async ({ page }) => {
    // ~120 of them, in parallel, on one render. That stampede is the actual cause of
    // "Could not load attached artifacts" — not a bug in the artifacts endpoint,
    // just far too many of it at once. They arrive embedded in the tree now.
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    await expect(page.getByTestId("tree-board")).toBeVisible();
    await expect(page.getByTestId("block-card").first()).toBeVisible();
    await expect(page.getByTestId("provenance-chip").first()).toBeVisible(); // segments rendered
    await page.waitForTimeout(300);

    expect(mock.calls.artifacts).toBe(0);
    await expect(page.getByTestId("segment-artifacts-error")).toHaveCount(0);
  });

  test("a drafted segment's citation deep-links into the Reader AT THE CITED PAGE", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    const chip = page.getByTestId("provenance-chip").first();
    await expect(chip).toBeVisible();
    await expect(chip).toHaveAttribute("href", "/en/library/book-1?page=56");
    await expect(chip).toContainText("p. 56");
    expect(mock.unexpected).toEqual([]);
  });

  test("DEEPEN puts a thin lesson back in the queue", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    // By KIND, not just by "a card containing a word count": BlockCards nest inside
    // one another (that is what makes the indentation work), so the course card also
    // "contains" every lesson's word count — and `.first()` would hand back the root.
    const drafted = page
      .locator('[data-testid="block-card"][data-kind="lesson"]')
      .filter({ has: page.getByTestId("lesson-word-count") })
      .first();
    await expect(drafted).toBeVisible();

    await drafted.getByTestId("lesson-deepen").click();

    // One request, and the row says so immediately — his click visibly did
    // something, and the next progress poll will show it drafting.
    await expect.poll(() => mock.deepened.length).toBe(1);
    await expect(drafted.getByTestId("lesson-status")).toHaveAttribute("data-status", "queued");
  });

  test("a board whose lessons finished a moment ago is NOT left showing stale rows", async ({ page }) => {
    // The board fetches the tree, then the progress bar starts polling. If the draft
    // finished in between, the first poll reports lessons ready that the tree it is
    // sitting on says are queued — and with no previous count to be greater than,
    // nothing refetches. Every row reads "queued" under a full progress bar, forever.
    const mock = await mockInterviewApi(page, { readyFromStart: true });
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    await expect(page.getByTestId("tree-board")).toBeVisible();
    await expect(page.getByTestId("lesson-status").first()).toHaveAttribute("data-status", "ready");
    await expect(page.getByTestId("lesson-word-count").first()).toBeVisible();
    expect(mock.unexpected).toEqual([]);
  });

  test("EXTEND WITH CHAT rewrites one block and offers an Undo", async ({ page }) => {
    // Chris: "a button to Extend with chat where the user writes e.g. change this
    // and give more detail about the Amp — and it actually follows his instruction."
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    const segment = page.locator('[data-testid="block-card"][data-kind="segment"]').first();
    await expect(segment).toBeVisible();
    await expect(segment.getByTestId("extend-undo")).toHaveCount(0); // nothing to undo yet

    await segment.getByTestId("extend-toggle").click();
    await segment.getByTestId("extend-instruction").fill("give more detail about the Amp");
    await segment.getByTestId("extend-submit").click();

    await expect(segment.getByTestId("block-card-body")).toContainText("REWRITTEN: give more detail about the Amp");
    // The Undo appears because the API stashed `prev_body` — it is on the block, so
    // it is still there after a reload, not just in this tab's memory.
    await expect(segment.getByTestId("extend-undo")).toBeVisible();
    expect(mock.refinements).toEqual(["give more detail about the Amp"]);
  });
});
