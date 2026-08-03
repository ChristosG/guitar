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

interface BlueprintSection {
  key: string;
  label: { el: string; en: string };
  description: string;
  weight: number;
  kind: "prose" | "exercises" | "qa";
  audience: "teacher" | "student" | "both";
  enabled: boolean;
}
interface Blueprint {
  version: number;
  sections: BlueprintSection[];
}

/** `findings.blueprint` on the "structure" step — `resolve_default_blueprint(db)`
 * on the real API, i.e. the settings default pre-filled into the (optional,
 * skippable) editor. Shaped exactly like `app.curriculum.blueprint`'s canonical
 * table; only a couple of sections are enough to exercise the wizard step. */
function makeDefaultBlueprint(): Blueprint {
  const section = (over: Partial<BlueprintSection> & Pick<BlueprintSection, "key">): BlueprintSection => ({
    label: { el: over.key, en: over.key },
    description: `Description for ${over.key}.`,
    weight: 0.1,
    kind: "prose",
    audience: "teacher",
    enabled: true,
    ...over,
  });
  return {
    version: 1,
    sections: [
      section({ key: "warm_up", label: { el: "Ζέσταμα", en: "Warm-up" }, weight: 0.07 }),
      section({ key: "theory", label: { el: "Θεωρία", en: "Theory" }, weight: 0.25 }),
      section({
        key: "exercises", label: { el: "Ασκήσεις", en: "Exercises" }, weight: 0.22,
        kind: "exercises", audience: "student",
        description: "Prose introducing and sequencing the exercises.",
      }),
      section({
        key: "qa_prompts", label: { el: "Ερωτήσεις & συζήτηση", en: "Q&A and discussion" }, weight: 0.06,
        kind: "qa", audience: "teacher",
        description: "How to open the 10-minute discussion block.",
      }),
    ],
  };
}

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

interface PlanningChatRow {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  citations: null;
}

interface MockOptions {
  /** Report the first lesson as drafted on the VERY FIRST poll — the case where the
   * lessons finished between the tree fetch and the first poll. See the test at the
   * bottom of this file: the board must not sit there showing "queued" rows under a
   * progress bar that says they are done. */
  readyFromStart?: boolean;
  /** Pretend a crash orphaned an interview at this step: `GET
   * /curricula/interview/open` returns it, and `GET .../{id}` renders that
   * step's entry state — the resume-chip flow (2026-07-23). */
  orphanAtStep?: string;
  /** Seeds the planning chat's `GET /chat/{id}` (history) with N real turns
   * instead of the default empty transcript. Task 6's layout fix (a definite
   * height on `DialogContent` + an internally-scrolling chat wrapper in
   * `planning-chat.tsx`) has NO test that can catch a regression against an
   * empty transcript — an empty chat never overflows anything. */
  planningHistoryTurns?: number;
}

/** A real multi-turn transcript, long enough to overflow the dialog's
 * `85dvh` cap on a normal viewport — the whole point of the layout fix this
 * seeds a test for (see `planningHistoryTurns` above). */
function makePlanningHistory(turns: number): PlanningChatRow[] {
  return Array.from({ length: turns }, (_, i) => ({
    id: randomUUID(),
    role: i % 2 === 0 ? "user" : "assistant",
    content:
      i % 2 === 0
        ? `Turn ${i}: I want a live-tone-focused course, three weeks, mostly evenings, leaning on my own library before you fill any gaps from general knowledge.`
        : `Turn ${i}: Got it — pedal-first, three weeks, your library first. Want me to sketch a rough week-by-week shape next, or keep talking through the gear list?`,
    created_at: new Date(2026, 0, 1, 0, i).toISOString(),
    citations: null,
  }));
}

async function mockInterviewApi(
  page: Page,
  { readyFromStart = false, planningHistoryTurns = 0, orphanAtStep }: MockOptions = {},
) {
  const interviewId = randomUUID();
  const rootId = randomUUID();

  let step = orphanAtStep ?? "who";
  // Answers already given, keyed by step — what the real API returns as
  // `prior` when a step is revisited (back navigation / resume).
  const savedAnswers: Record<string, unknown> = {};
  let currentOutline: Outline = makeOutline();
  let tree: FixtureBlock | null = null;
  let polls = 0;
  let resumed = 0;
  let redrafted = 0;
  // Flipped by the test when it wants the remaining lessons to land. Deterministic,
  // unlike counting polls: Next's dev-mode StrictMode double-mounts effects, so a
  // poll COUNT is not a clock.
  let finished = false;

  const calls = {
    start: 0, answer: 0, get: 0, progress: 0, artifacts: 0,
    // Part 5 — the planning phase's own three endpoints.
    chatSession: 0, distill: 0, planningBrief: 0,
  };
  const planningSessionId = randomUUID();
  let distillOutcome: "ok" | "empty" = "ok";
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

  /** The ENTRY state of a step — what the real API renders when the step is
   * (re)visited via back navigation or a resume `GET`, `prior` included. The
   * forward-transition payloads in the answer switch below stay the source of
   * truth for the forward flow; this mirrors them for re-entry. */
  const entryState = (s: string) => {
    const prior = savedAnswers[s] ?? null;
    if (s === "who")
      return state({
        step: "who", prior, question: "What level, and what language?",
        options: null,
        findings: {
          levels: ["all_levels", "beginner", "intermediate", "advanced"],
          languages: ["el", "en"],
        },
      });
    if (s === "duration") return state({ step: "duration", prior, question: "How long does this run?" });
    if (s === "scope")
      return state({
        step: "scope", prior, question: "What is this course FOR?",
        options: [
          { value: "library_only", label: "Only my library." },
          { value: "general_knowledge", label: "Fill the gaps from general knowledge." },
        ],
      });
    if (s === "structure")
      return state({
        step: "structure", prior, question: "The lesson structure — an optional step",
        findings: { blueprint: makeDefaultBlueprint() },
      });
    if (s === "sources")
      return state({
        step: "sources", prior, question: "Which sources?",
        options: SOURCES.map((src) => ({
          value: src.id, label: src.title, type: src.type,
          char_count: src.char_count, default_selected: src.default_selected,
        })),
        findings: {
          shape: {
            lessons_total: 3, modules: 1, lessons_per_module: [3],
            target_words_per_lesson: 2200, teaching_minutes: 40, qa_minutes: 10,
          },
        },
      });
    if (s === "outline")
      return state({ step: "outline", question: "Here is the course.", findings: currentOutline });
    return state({ step: "confirm", question: "Ready?", findings: currentOutline });
  };

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

    if (pathname === "/curricula/interview/open" && method === "GET") {
      return json(
        orphanAtStep
          ? { interview_id: interviewId, title: "Orphaned course", step, updated_at: new Date().toISOString() }
          : null,
      );
    }
    if (pathname.match(/^\/curricula\/interview\/[^/]+\/back$/) && method === "POST") {
      const order = ["who", "duration", "scope", "structure", "sources", "outline", "confirm"];
      const i = order.indexOf(step);
      if (i > 0) step = order[i - 1];
      return json(entryState(step));
    }
    if (pathname.match(/^\/curricula\/interview\/[^/]+$/) && method === "GET") {
      return json(entryState(step));
    }
    if (pathname === "/curricula" && method === "GET") return json([]);

    if (pathname === "/curricula/interview" && method === "POST") {
      calls.start++;
      step = "who";
      return json(
        state({
          step: "who",
          question: "What level, and what language?",
          options: null,
          findings: {
            levels: ["all_levels", "beginner", "intermediate", "advanced"],
            languages: ["el", "en"],
          },
        }),
        201,
      );
    }

    // Part 5 — «Θέλεις να το συζητήσουμε πρώτα;»'s three endpoints. Distinct
    // sub-paths under `/curricula/interview/{id}/`, so this must be checked
    // BEFORE the `/answer` match below (which is exact-suffix but these are
    // too, so order only matters for readability here).
    if (pathname.match(/^\/curricula\/interview\/[^/]+\/chat-session$/) && method === "GET") {
      calls.chatSession++;
      return json({ session_id: planningSessionId });
    }
    if (pathname.match(/^\/curricula\/interview\/[^/]+\/distill$/) && method === "POST") {
      calls.distill++;
      if (distillOutcome === "empty") {
        return json({ detail: "the transcript is empty" }, 409);
      }
      return json({ brief: "Wants a 3-week live-tone crash course, pedals-first." });
    }
    if (pathname.match(/^\/curricula\/interview\/[^/]+\/planning-brief$/) && method === "PUT") {
      calls.planningBrief++;
      return route.fulfill({ status: 204, headers: CORS_HEADERS });
    }

    // The planning chat mounts a real `ChatPanel` (Task 5 reuses it wholesale),
    // which hydrates on mount via `GET /chat/{id}` (history) and `GET
    // /chat/{id}/pending` (any HITL approval left open) — an empty transcript
    // by default (no pending approval, matching a session nobody has spoken in
    // yet), or `planningHistoryTurns` real turns when a test asks for them.
    if (pathname === `/chat/${planningSessionId}` && method === "GET") {
      return json(makePlanningHistory(planningHistoryTurns));
    }
    if (pathname === `/chat/${planningSessionId}/pending` && method === "GET") return json(null);
    if (pathname === `/chat/${planningSessionId}/suggestions` && method === "POST") {
      return json({ suggestions: [] });
    }

    if (pathname.match(/^\/curricula\/interview\/[^/]+\/answer$/) && method === "POST") {
      calls.answer++;
      const answer = (req.postDataJSON() as { answer: Record<string, unknown> }).answer;
      answerBodies.push(answer);
      if (["who", "duration", "scope", "structure", "sources"].includes(step)) {
        savedAnswers[step] = answer;
      }

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
        // STEP_ORDER inserted "structure" here between "scope" and "sources"
        // (Plan C, Task 5) — an optional, skippable "lesson structure" step,
        // pre-filled with the settings-default blueprint.
        step = "structure";
        return json(
          state({
            step: "structure",
            question: "The lesson structure — an optional step",
            findings: { blueprint: makeDefaultBlueprint() },
          }),
        );
      }
      if (step === "structure") {
        step = "sources";
        return json(
          state({
            step: "sources",
            question: "Which sources?",
            options: SOURCES.map((s) => ({
              value: s.id, label: s.title, type: s.type,
              char_count: s.char_count, default_selected: s.default_selected,
            })),
            // THE REAL SHAPE OF `findings.shape`: an OBJECT of numbers
            // (`InterviewShape`), never the formatted sentence this fixture used
            // to send — `interview-sources-step.tsx:66-71` reads
            // `shape.lessons_total/modules/lessons_per_module.join("+")/
            // target_words_per_lesson/teaching_minutes/qa_minutes`, and a string
            // has none of those, which is exactly what made this whole file RED.
            findings: {
              shape: {
                lessons_total: 3, modules: 1, lessons_per_module: [3],
                target_words_per_lesson: 2200, teaching_minutes: 40, qa_minutes: 10,
              },
            },
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

    if (pathname.match(/^\/curricula\/[^/]+\/redraft$/) && method === "POST") {
      redrafted++;
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
  // The planning phase's `ChatPanel` hydrates against `/chat/**` — unrouted
  // in this file until Part 5, since nothing before it ever mounted a chat.
  await page.route(`${API_ORIGIN}/chat/**`, handler);

  return {
    calls, answerBodies, unexpected, deepened, refinements,
    resumeCount: () => resumed, redraftCount: () => redrafted, rootId,
    finishDraft: () => {
      finished = true;
    },
    // Flips the next `distill` response to the empty-transcript 409, so a
    // test can assert the localized `distillEmpty` message renders.
    setDistillEmpty: () => {
      distillOutcome = "empty";
    },
  };
}

/** Drive the interview up to (not through) `target`. */
async function startToStep(
  page: Page,
  target: "who" | "duration" | "scope" | "structure" | "sources" | "outline" | "confirm",
) {
  await page.goto("/en/curricula");
  await page.getByTestId("curricula-generate-button").click();
  await expect(page.getByTestId("interview-dialog")).toBeVisible();
  await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
  await page.getByTestId("interview-start-submit").click();
  if (target === "who") return;

  await page.getByTestId("interview-answer-submit").click(); // who: all levels, default language
  if (target === "duration") return;

  await page.getByTestId("interview-duration-weeks").fill("3");
  await page.getByTestId("interview-duration-minutes").fill("60");
  await page.getByTestId("interview-answer-submit").click();
  if (target === "scope") return;

  await page.getByTestId("interview-scope-brief").fill("Get him a usable live tone.");
  await page.getByTestId("interview-answer-submit").click();
  if (target === "structure") return;

  // STRUCTURE IS OPTIONAL AND SKIPPABLE (spec invariant #7) — every step past
  // it is driven with the standard structure, exactly as a tutor who never
  // touches this screen gets.
  await page.getByTestId("interview-structure-skip").click();
  if (target === "sources") return;

  await page.getByTestId("interview-answer-submit").click(); // sources: accept the default selection
  await expect(page.getByTestId("outline-editor")).toBeVisible();
  if (target === "outline") return;

  await page.getByTestId("interview-answer-submit").click(); // outline: accept as-is
  await expect(page.getByTestId("interview-confirm-heading")).toBeVisible();
}

/** THE BOARD RENDERS COLLAPSED BY DEFAULT NOW (`block-card.tsx`: "everything is
 * expanded and a chaos" — only the course root opens expanded), and its
 * `CollapsibleContent` does not just hide a closed card's children, it does not
 * mount them at all. So every assertion below that reaches into a LESSON's own
 * badges (`lesson-status`, `lesson-word-count`, `lesson-deepen` — all rendered
 * in the card's header, unconditionally once the card itself exists) needs its
 * MODULE expanded first; reaching into a SEGMENT (the extend controls) or the
 * LESSON's own body (`lesson-sources-trigger`) needs the LESSON expanded too,
 * one level further in. */
async function expandModule(page: Page, index = 0) {
  const modules = page.locator('[data-testid="block-card"][data-kind="module"]');
  await modules.nth(index).getByTestId("block-card-toggle").click();
}

/** The lesson only GROWS a toggle once it has segments to show (a `queued`
 * lesson has no children yet) — so this waits for it to be `ready` first. */
async function expandFirstLesson(page: Page, moduleIndex = 0) {
  const modules = page.locator('[data-testid="block-card"][data-kind="module"]');
  const lessons = modules.nth(moduleIndex).locator('[data-testid="block-card"][data-kind="lesson"]');
  const first = lessons.first();
  await expect(first.getByTestId("lesson-status")).toHaveAttribute("data-status", "ready");
  await first.getByTestId("block-card-toggle").click();
}

test.describe("the guided interview, v2 (mocked API)", () => {
  test("the first step asks level + language — the roster left the product", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "who");

    // No student options anywhere — the step renders the two selectors the
    // generation actually consumes. The language defaults to the cockpit
    // locale (this run is /en) but is a first-class choice, because a
    // Greek-UI tutor writing English courses is the normal case.
    await expect(page.getByTestId("interview-who-levels")).toBeVisible();
    await expect(page.getByTestId("interview-who-languages")).toBeVisible();

    await page.getByTestId("interview-who-level-beginner").click();
    await page.getByTestId("interview-who-language-el").click();
    await page.getByTestId("interview-answer-submit").click();

    expect(mock.answerBodies[0]).toEqual({ student_id: null, level: "beginner", language: "el" });
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

  test("the scope step deep-links into the Curriculum prompt group, without submitting the step", async ({ page }) => {
    // Plan C, Task 7: a secondary link beside Continue, opening Settings on the
    // exact prompts that will build this course — it must be a plain
    // navigation, never a form submit (he has not answered this step yet).
    await mockInterviewApi(page);
    await startToStep(page, "scope");

    const link = page.getByTestId("interview-scope-prompts-link");
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("href", "/en/settings?promptGroup=curriculum");
    // Still on the scope step — the button did not submit anything.
    await expect(page.getByTestId("interview-scope-brief")).toBeVisible();
  });

  test("the structure step is optional and skippable, pre-filled with the settings default", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "structure");

    await expect(page.getByTestId("interview-structure-step")).toBeVisible();
    // Pre-filled from `findings.blueprint` — he sees a working structure before
    // touching anything (spec invariant #7).
    await expect(page.getByTestId("blueprint-editor")).toBeVisible();
    await expect(page.getByTestId("blueprint-section-warm_up")).toBeVisible();

    await page.getByTestId("interview-structure-skip").click();

    // Skip records NOTHING custom — `_answer_structure` treats this identically
    // to never having visited the step (the settings default is what ends up on
    // the course either way).
    expect(mock.answerBodies.at(-1)).toEqual({ skip: true });
    await expect(page.getByTestId("interview-source-row-book-1")).toBeVisible();
  });

  test("the structure step's edited blueprint is what gets sent on Continue", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "structure");

    await page.getByTestId("blueprint-description-warm_up").fill("A custom warm-up routine.");
    await page.getByTestId("interview-answer-submit").click();

    const sent = mock.answerBodies.at(-1) as { blueprint: { sections: { key: string; description: string }[] } };
    const warmUp = sent.blueprint.sections.find((s) => s.key === "warm_up");
    expect(warmUp?.description).toBe("A custom warm-up routine.");
    await expect(page.getByTestId("interview-source-row-book-1")).toBeVisible();
  });

  test("the sources step echoes the derived shape and lets him drop the filler source", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "sources");

    // He is agreeing to a SIZE before anyone spends his money on it. (The
    // message interpolates the raw number — no thousands separator — so this
    // matches what `steps.sources.shape` in en.json actually renders.)
    await expect(page.getByTestId("interview-shape-echo")).toContainText("~2200 words each");

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

  test("BACK re-renders the previous step with the answer he already gave", async ({ page }) => {
    await mockInterviewApi(page);
    await startToStep(page, "scope");

    // No back button on the very first step; from scope, go back to duration.
    await page.getByTestId("interview-back").click();

    // The three numbers he typed on the way forward are prefilled — Continue
    // re-submits HIS answer, never blank defaults (`prior` on the wire).
    await expect(page.getByTestId("interview-duration-weeks")).toHaveValue("3");
    await expect(page.getByTestId("interview-duration-minutes")).toHaveValue("60");

    // Forward again lands where he left off, values intact server-side.
    await page.getByTestId("interview-answer-submit").click();
    await expect(page.getByTestId("interview-scope-brief")).toBeVisible();
  });

  test("the back button does not render on the first step", async ({ page }) => {
    await mockInterviewApi(page);
    await startToStep(page, "who");
    await expect(page.getByTestId("interview-back")).toHaveCount(0);
  });

  test("a crash-orphaned interview is offered on the index and resumes at its step", async ({ page }) => {
    await mockInterviewApi(page, { orphanAtStep: "scope" });
    await page.goto("/en/curricula");

    // The chip names the orphaned course; clicking re-enters the wizard at
    // whatever step the SERVER says it is on — no restart, nothing lost.
    await expect(page.getByTestId("interview-resume")).toBeVisible();
    await page.getByTestId("interview-resume").click();
    await expect(page.getByTestId("interview-dialog")).toBeVisible();
    await expect(page.getByTestId("interview-scope-brief")).toBeVisible();
  });

  test("dismissing the resume chip hides it and survives a reload", async ({ page }) => {
    await mockInterviewApi(page, { orphanAtStep: "scope" });
    await page.goto("/en/curricula");
    await expect(page.getByTestId("interview-resume")).toBeVisible();
    await page.getByTestId("interview-resume-dismiss").click();
    await expect(page.getByTestId("interview-resume")).toHaveCount(0);
    await page.reload();
    await expect(page.getByTestId("curricula-generate-button")).toBeVisible();
    await expect(page.getByTestId("interview-resume")).toHaveCount(0);
  });

  test("the live footer counts modules, lessons, words and dollars while he edits", async ({ page }) => {
    await mockInterviewApi(page);
    await startToStep(page, "outline");

    // 50 MEANS 50: the whole session is teaching time (no Q&A carve-out any
    // more — Chris, 2026-07-21), so a 50-minute lesson targets 50 x 55
    // words/min = 2,750 words. Three of them: 8,250 words, 3 x $0.12 = $0.36.
    // The same constants `depth.py`/`shape.py` draft against — not a second set.
    const footer = page.getByTestId("outline-footer-summary");
    await expect(footer).toContainText("2 modules");
    await expect(footer).toContainText("3 lessons");
    await expect(footer).toContainText("8,250");
    await expect(footer).toContainText("$0.36");

    // Delete a module -> the money moves. That is why the number is there at all.
    await page.getByTestId("outline-module-delete-1").click();
    await page.getByTestId("confirm-accept").click();
    await expect(footer).toContainText("1 module");
    await expect(footer).toContainText("5,500");
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
    await expandModule(page, 0);
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
    await expandModule(page, 0);
    await expandFirstLesson(page, 0);
    await expect(page.getByTestId("lesson-sources-trigger").first()).toBeVisible(); // segments rendered
    await page.waitForTimeout(300);

    expect(mock.calls.artifacts).toBe(0);
    await expect(page.getByTestId("segment-artifacts-error")).toHaveCount(0);
  });

  test("a drafted lesson's citation deep-links into the Reader AT THE CITED PAGE", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    await expandModule(page, 0);
    await expandFirstLesson(page, 0);
    await expect(page.getByTestId("lesson-sources-trigger").first()).toBeVisible();
    await page.getByTestId("lesson-sources-trigger").first().click();
    await expect(page.getByTestId("lesson-sources-modal")).toBeVisible();
    const pageLink = page.getByTestId("lesson-source-page").first();
    await expect(pageLink).toBeVisible();
    await expect(pageLink).toHaveAttribute("href", "/en/library/book-1?page=56");
    await expect(pageLink).toContainText("p. 56");
    expect(mock.unexpected).toEqual([]);
  });

  test("DEEPEN puts a thin lesson back in the queue", async ({ page }) => {
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();
    await expandModule(page, 0);

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

  test("'Re-draft under the current structure' is confirm-gated and hits the redraft route, never the plain draft one", async ({ page }) => {
    // Plan C, Task 8: the ONLY button that rewrites lessons that already
    // drafted — a count-aware confirm names what is about to happen before it
    // fires, and it must never be reachable as a side effect of anything else.
    const mock = await mockInterviewApi(page);
    await startToStep(page, "confirm");
    await page.getByTestId("interview-confirm-submit").click();

    const button = page.getByTestId("board-redraft");
    await expect(button).toBeVisible();
    await button.click();

    // Names the count — 3 lessons total (2 + 1), none of them under a gap
    // module (this fixture's two modules are "library"/"general_knowledge").
    await expect(page.getByTestId("confirm-title")).toBeVisible();
    await expect(page.getByTestId("confirm-body")).toContainText("3");

    await page.getByTestId("confirm-accept").click();

    await expect.poll(() => mock.redraftCount()).toBe(1);
    expect(mock.resumeCount()).toBe(0);
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
    await expandModule(page, 0);
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
    await expandModule(page, 0);
    await expandFirstLesson(page, 0);

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

  // Part 5 — «Θέλεις να το συζητήσουμε πρώτα;»: an optional planning-chat
  // detour between the intro and the first server step. Skipping it (the
  // primary Start button) must stay byte-for-byte today's flow.
  test.describe("the planning detour (Part 5)", () => {
    test("the intro offers it alongside the primary Start button", async ({ page }) => {
      await mockInterviewApi(page);
      await page.goto("/en/curricula");
      await page.getByTestId("curricula-generate-button").click();
      await expect(page.getByTestId("interview-dialog")).toBeVisible();

      await expect(page.getByTestId("interview-start-submit")).toBeVisible();
      await expect(page.getByTestId("interview-plan-first")).toBeVisible();
    });

    test("the primary Start button skips it — today's flow, unchanged", async ({ page }) => {
      const mock = await mockInterviewApi(page);
      await startToStep(page, "who");

      await expect(page.getByTestId("interview-who-levels")).toBeVisible();
      expect(mock.calls.start).toBe(1);
      // Never touches ANY of the planning endpoints — a tutor who never sees
      // the secondary button costs the server nothing extra.
      expect(mock.calls.chatSession).toBe(0);
      expect(mock.calls.distill).toBe(0);
      expect(mock.calls.planningBrief).toBe(0);
    });

    test("plan-first opens the chat; Skip lands on the first interview step", async ({ page }) => {
      const mock = await mockInterviewApi(page);
      await page.goto("/en/curricula");
      await page.getByTestId("curricula-generate-button").click();
      await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
      await page.getByTestId("interview-plan-first").click();

      // Same `startInterview` call as the primary button — just ONE, landing
      // on the planning chat instead of the "who" step.
      expect(mock.calls.start).toBe(1);
      await expect(page.getByTestId("planning-chat")).toBeVisible();
      await expect(page.getByTestId("chat-input")).toBeVisible();
      // Exactly 1: `planning-chat.tsx`'s mount effect now guards against Next
      // dev mode's StrictMode double-invoke with a ref (`chat-panel.tsx`'s own
      // hydration-guard pattern), so the session bootstrap fires only once.
      await expect.poll(() => mock.calls.chatSession).toBe(1);

      await page.getByTestId("planning-skip").click();

      await expect(page.getByTestId("interview-who-levels")).toBeVisible();
      // Skip proceeds WITHOUT distilling or saving anything.
      expect(mock.calls.distill).toBe(0);
      expect(mock.calls.planningBrief).toBe(0);
    });

    test("Use this plan distills, the brief is editable, and Continue saves it", async ({ page }) => {
      const mock = await mockInterviewApi(page);
      await page.goto("/en/curricula");
      await page.getByTestId("curricula-generate-button").click();
      await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
      await page.getByTestId("interview-plan-first").click();
      await expect(page.getByTestId("planning-chat")).toBeVisible();

      await page.getByTestId("planning-distill").click();
      const editor = page.getByTestId("planning-brief-editor");
      await expect(editor).toHaveValue("Wants a 3-week live-tone crash course, pedals-first.");

      // The tutor audits the machine's understanding before anything is
      // generated — free-form editing, not just an accept button.
      await editor.fill("Wants a 3-week live-tone crash course, pedals-first, no music theory.");
      await page.getByTestId("planning-continue").click();

      await expect(page.getByTestId("interview-who-levels")).toBeVisible();
      expect(mock.calls.distill).toBe(1);
      expect(mock.calls.planningBrief).toBe(1);
    });

    test("a 409 from distill (empty transcript) shows the localized retry message", async ({ page }) => {
      const mock = await mockInterviewApi(page);
      mock.setDistillEmpty();
      await page.goto("/en/curricula");
      await page.getByTestId("curricula-generate-button").click();
      await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
      await page.getByTestId("interview-plan-first").click();
      await expect(page.getByTestId("planning-chat")).toBeVisible();

      await page.getByTestId("planning-distill").click();

      // The backend's 409 detail is English/technical — this is the
      // LOCALIZED message the component substitutes for it (`err.status ===
      // 409` branches to `t("distillEmpty")` rather than showing `err.detail`).
      await expect(page.getByTestId("planning-chat").getByRole("alert")).toHaveText(
        "There's no conversation yet to distill — write what you want first.",
      );
      // Still on the chat phase — nothing was saved, and he can try again.
      await expect(page.getByTestId("chat-input")).toBeVisible();
    });

    test("a real multi-turn transcript scrolls in its own region — Skip and Use this plan never leave the dialog", async ({ page }) => {
      // Task 6's layout fix: `PlanningChat`'s chat wrapper used to have no
      // `overflow-y-auto` of its own, so a transcript taller than the wrapper's
      // flex-computed box just painted past it (`overflow: visible`) instead of
      // clipping and scrolling — never truly off past the viewport, but a
      // genuinely broken, unscrollable mess overlapping whatever sat below it.
      // `DialogContent` getting a DEFINITE height for this phase (`h-[85dvh]`,
      // not just `max-h`) is what makes that wrapper's `flex-1`/`min-h-0` a
      // real, bounded box in the first place. Every other test in this file
      // mocks an EMPTY transcript, which can never reproduce this — nothing to
      // overflow. This one seeds 24 real turns.
      await mockInterviewApi(page, { planningHistoryTurns: 24 });
      await page.goto("/en/curricula");
      await page.getByTestId("curricula-generate-button").click();
      await page.getByTestId("interview-title").fill("Getting a Great Guitar Tone");
      await page.getByTestId("interview-plan-first").click();
      await expect(page.getByTestId("planning-chat")).toBeVisible();

      // The transcript actually hydrated with the seeded history, not just an
      // empty panel that would trivially pass the checks below.
      await expect(page.getByText("Turn 23:", { exact: false })).toBeAttached();

      // Both action buttons stay inside the dialog card, not carried off past
      // its clipped edge.
      await expect(page.getByTestId("planning-skip")).toBeInViewport();
      await expect(page.getByTestId("planning-distill")).toBeInViewport();

      // THE MECHANISM ITSELF: `planning-transcript` must be a genuine scroll
      // container — content taller than its box (`scrollHeight >
      // clientHeight`), AND actually scrollable (`scrollTop` moves when set).
      // A box that merely overflows with `overflow: visible` reports the same
      // scrollHeight/clientHeight gap but ignores `scrollTop` entirely (it is
      // not a scroll container at all) — that is exactly the pre-fix state,
      // and geometry-only checks on sibling buttons do not reliably catch it.
      const transcript = page.getByTestId("planning-transcript");
      const scroll = await transcript.evaluate((el) => {
        const before = { scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
        el.scrollTop = el.scrollHeight;
        return { ...before, scrollTopAfter: el.scrollTop };
      });
      expect(scroll.scrollHeight).toBeGreaterThan(scroll.clientHeight);
      expect(scroll.scrollTopAfter).toBeGreaterThan(0);
    });
  });
});
