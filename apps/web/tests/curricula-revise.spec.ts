import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the "Revise with AI" drawer on the
// curriculum detail board (Unit D, Task D2b): opening the drawer lazily
// creates a chat session BOUND to this curriculum (`root_id` in the POST
// `/chat` body), a proposed revision renders as a `RevisionPlanCard` — never
// the generic `ApprovalCard` — with each op labeled per the controller's own
// resolved design calls (a `modify_lesson` op reads "Rewrite lesson «X»", an
// `update_blueprint` op reads "Change lesson structure" plus its own note
// that existing lessons keep their content until re-drafted), approving it
// calls `POST /chat/{id}/approvals/{id}/resolve` with the plan applied
// VERBATIM (no `edited_args`), and once the chained `curriculum_draft` job
// succeeds the board refetches and shows the newly `queued` lesson — the
// whole PLAN -> PREVIEW -> APPROVE -> APPLY loop, offline.
//
// Same route-interception convention every other cockpit spec in this
// directory uses (`cockpit.spec.ts`, `chat.spec.ts`): anchored to
// `API_ORIGIN` (never a bare suffix glob — see those files' own docstrings
// for why), explicit CORS headers + OPTIONS handling because these are
// cross-origin credentialed calls even under Playwright's interception, and
// an "unexpected request -> 500" catch-all so a routing mistake fails loudly.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
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
  meta: Record<string, unknown> | null;
  children: FixtureBlock[];
}

/** Mocks the one curriculum this whole spec revises: a course -> one module
 * -> two ALREADY-DRAFTED lessons. `applied()` flips true the moment the mock
 * `/chat/.../resolve` call for `apply_curriculum_revision` lands — mirroring
 * (for this offline mock's purposes) that by the time a real `job_pending`
 * resolve response comes back, the backend's `apply_revision` has already
 * committed its one transaction; every `GET /curricula/{root}` after that
 * point reflects it, exactly like the real chained `curriculum_draft` job
 * would once it succeeds. */
function mockCurriculumTree(rootId: string, moduleId: string, lesson1Id: string, lesson2Id: string, newLessonId: string) {
  let applied = false;

  function tree(): FixtureBlock {
    const lessons: FixtureBlock[] = [
      {
        id: lesson1Id, kind: "lesson", title: "Pickups and Tone", body: "Pickup types and tone shaping.",
        est_minutes: 30, order: 0, language: "en", plane: "content", student_id: null,
        meta: { draft_status: "ready", objective: "Understand pickup types." }, children: [],
      },
      {
        id: lesson2Id, kind: "lesson", title: "Cables and Signal Integrity", body: "Cable quality and tone.",
        est_minutes: 20, order: 1, language: "en", plane: "content", student_id: null,
        meta: { draft_status: "ready", objective: "Understand cable quality." }, children: [],
      },
    ];
    if (applied) {
      lessons.push({
        id: newLessonId, kind: "lesson", title: "DS-1 Distortion", body: null, est_minutes: null,
        order: 2, language: "en", plane: "content", student_id: null,
        meta: { draft_status: "queued", objective: "distortion basics" }, children: [],
      });
    }
    return {
      id: rootId, kind: "course", title: "Tone Shaping Fundamentals", body: null, est_minutes: null,
      order: 0, language: "en", plane: "content", student_id: null,
      meta: { brief: "A usable live tone.", source_ids: null },
      children: [
        {
          id: moduleId, kind: "module", title: "Signal Chain Basics", body: "Understand the guitar signal path.",
          est_minutes: null, order: 0, language: "en", plane: "content", student_id: null,
          meta: { objective: "Understand the guitar signal path.", tier: "library" }, children: lessons,
        },
      ],
    };
  }

  function progress() {
    const total = applied ? 3 : 2;
    const queued = applied ? 1 : 0;
    return { root_id: rootId, total, queued, drafting: 0, ready: 2, failed: 0, done: !applied };
  }

  return { tree, progress, markApplied: () => { applied = true; }, isApplied: () => applied };
}

async function mockCurriculaApi(page: Page, fixture: ReturnType<typeof mockCurriculumTree>) {
  const calls = { get: 0, progress: 0 };
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const progressMatch = pathname.match(/^\/curricula\/([^/]+)\/progress$/);
    if (progressMatch && method === "GET") {
      calls.progress++;
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(fixture.progress()),
      });
      return;
    }

    const getMatch = pathname.match(/^\/curricula\/([^/]+)$/);
    if (getMatch && method === "GET") {
      calls.get++;
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(fixture.tree()),
      });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  return { calls, unexpected };
}

function sseBody(events: Array<{ event: string; data: unknown }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
}

/** Trimmed-down mock of the Chat API — same shape as `chat.spec.ts`'s own
 * `mockChatApi`, cut to just what this drawer exercises: session creation
 * (capturing the `root_id` the drawer must pass), the stream-falls-back-to-
 * REST transport (this spec never scripts a real streamed answer — the
 * point here is the tool-call/approval path, which the stream endpoint
 * never handles at all — see `routers/chat.py`'s own documented contract),
 * one scripted `.../messages` response (with an optional artificial delay,
 * so the "Planning the revision…" status can actually be caught mid-flight),
 * and resolve. */
async function mockChatApi(page: Page, onResolve?: (approvalId: string) => void) {
  const calls = { create: 0, message: 0, resolve: 0, history: 0, pending: 0, stream: 0, list: 0 };
  const lastBody: { create?: unknown; resolve?: unknown } = {};
  const unexpected: string[] = [];

  let nextMessage: Record<string, unknown> = { status: "answer", content: "OK." };
  let nextMessageDelayMs = 0;
  let nextResolve: Record<string, unknown> = { status: "answer", content: "OK." };

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (pathname === "/chat" && method === "POST") {
      calls.create++;
      lastBody.create = req.postDataJSON();
      await json({ session_id: randomUUID() });
      return;
    }

    // `ChatSessionsProvider` (mounted around `ChatPanel` here so its
    // `useChatSessions()` call has an ancestor — see `revise-drawer.tsx`)
    // fetches this once per mount purely to refresh the (nonexistent, in
    // this drawer) sidebar; an empty list is a complete, correct answer.
    if (pathname === "/chat" && method === "GET") {
      calls.list++;
      await json([]);
      return;
    }

    const streamMatch = pathname.match(/^\/chat\/([^/]+)\/messages\/stream$/);
    if (streamMatch && method === "POST") {
      calls.stream++;
      // Always the honest "server declined to stream this turn" fallback —
      // this spec's whole subject is the tool-call/approval path, which the
      // streaming endpoint never handles (see `routers/chat.py`'s own
      // documented contract); `chat-panel.tsx` falls through to the REST
      // `.../messages` call below exactly the way it always does for a real
      // tool call.
      await route.fulfill({
        status: 200, contentType: "text/event-stream", headers: CORS_HEADERS,
        body: sseBody([{ event: "fallback", data: { reason: "tool_call" } }]),
      });
      return;
    }

    const messagesMatch = pathname.match(/^\/chat\/([^/]+)\/messages$/);
    if (messagesMatch && method === "POST") {
      calls.message++;
      if (nextMessageDelayMs > 0) await new Promise((r) => setTimeout(r, nextMessageDelayMs));
      await json(nextMessage);
      return;
    }

    const resolveMatch = pathname.match(/^\/chat\/([^/]+)\/approvals\/([^/]+)\/resolve$/);
    if (resolveMatch && method === "POST") {
      calls.resolve++;
      lastBody.resolve = req.postDataJSON();
      onResolve?.(resolveMatch[2]);
      await json(nextResolve);
      return;
    }

    const pendingMatch = pathname.match(/^\/chat\/([^/]+)\/pending$/);
    if (pendingMatch && method === "GET") {
      calls.pending++;
      await json(null);
      return;
    }

    const sessionMatch = pathname.match(/^\/chat\/([^/]+)$/);
    if (sessionMatch && method === "GET") {
      calls.history++;
      await json([]);
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(`${API_ORIGIN}/chat`, handler);
  await page.route(`${API_ORIGIN}/chat/**`, handler);

  return {
    calls,
    lastBody,
    unexpected,
    setNextMessage(value: Record<string, unknown>, delayMs = 0) {
      nextMessage = value;
      nextMessageDelayMs = delayMs;
    },
    setNextResolve(value: Record<string, unknown>) {
      nextResolve = value;
    },
  };
}

/** `GET /jobs/{id}` only — same "pending once, then terminal" shape
 * `chat.spec.ts`'s own `mockJobsApi` uses, trimmed to this file's one job. */
async function mockJobsApi(page: Page, { pendingPolls = 1 } = {}) {
  let polls = 0;
  const calls = { job: 0 };

  await page.route(`${API_ORIGIN}/jobs/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    const match = pathname.match(/^\/jobs\/([^/]+)$/);
    if (!match) {
      await route.fulfill({
        status: 500, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
      return;
    }
    calls.job++;
    polls++;
    const base = {
      id: match[1], kind: "curriculum_revise",
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    };
    if (polls <= pendingPolls) {
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ ...base, status: "pending", result_root_id: null, error: null, error_kind: null }),
      });
      return;
    }
    await route.fulfill({
      status: 200, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ ...base, status: "succeeded", result_root_id: null, error: null, error_kind: null }),
    });
  });

  return calls;
}

test.describe("curriculum revise drawer (mocked API)", () => {
  test("propose -> labeled RevisionPlanCard -> approve applies verbatim -> board refreshes with the new lesson", async ({
    page,
  }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const curricula = await mockCurriculaApi(page, fixture);
    const chat = await mockChatApi(page, () => fixture.markApplied());
    const jobs = await mockJobsApi(page, { pendingPolls: 1 });

    await page.goto(`/en/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // The drawer is closed by default and no session exists yet.
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);
    expect(chat.calls.create).toBe(0);

    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("revise-drawer")).toBeVisible();

    // Lazily creates ONE session, bound to THIS curriculum.
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(chat.calls.create).toBe(1);
    expect((chat.lastBody.create as { root_id?: string }).root_id).toBe(rootId);

    // Re-opening (closing then re-opening) must not spend a second session.
    await page.getByTestId("revise-close").click();
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(chat.calls.create).toBe(1);

    // Script a DELAYED awaiting_approval turn so "Planning the revision…"
    // (resolved design call #2) is actually observable mid-flight, not just
    // theoretically reachable.
    const approvalId = randomUUID();
    chat.setNextMessage(
      {
        status: "awaiting_approval",
        approval_id: approvalId,
        tool_name: "apply_curriculum_revision",
        tool_args: {
          root_id: rootId,
          plan: {
            summary: "Add a DS-1 lesson and rewrite the pickups lesson; simplify the lesson structure.",
            ops: [
              {
                op: "insert_lesson",
                module_id: moduleId,
                title: "DS-1 Distortion",
                objective: "distortion basics",
                reason: "Fills a gap after the Tube Screamer material.",
              },
              {
                op: "modify_lesson",
                lesson_id: lesson1Id,
                instruction: "add more detail on single-coil vs humbucker tone",
                reason: "The tutor asked for more depth on pickups.",
              },
              {
                op: "update_blueprint",
                blueprint: { version: 1, sections: [{ id: "intro", label: "Intro", weight: 1 }] },
                reason: "Shorten the intro section on every lesson.",
              },
            ],
          },
        },
        description: "Here is a proposed revision.",
      },
      300,
    );

    await page.getByTestId("chat-input").fill("Add a DS-1 lesson and beef up the pickups lesson.");
    await page.getByTestId("chat-send").click();

    // The synchronous inline planner's own progress state (never a bare
    // spinner) — visible during the artificial delay, gone once the turn
    // lands.
    await expect(page.getByTestId("chat-planning-revision")).toBeVisible();

    // The REVISE variant, not the generic ApprovalCard.
    await expect(page.getByTestId("revision-plan-card")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("chat-planning-revision")).toHaveCount(0);
    await expect(page.getByTestId("approval-card")).toHaveCount(0);

    await expect(page.getByTestId("revision-plan-summary")).toHaveText(
      "Add a DS-1 lesson and rewrite the pickups lesson; simplify the lesson structure.",
    );

    const ops = page.getByTestId("revision-plan-op");
    await expect(ops).toHaveCount(3);
    // insert_lesson carries its own title.
    await expect(ops.nth(0)).toContainText("Add lesson “DS-1 Distortion”");
    await expect(ops.nth(0)).toContainText("Fills a gap after the Tube Screamer material.");
    // modify_lesson: the controller's required wording, with the REAL lesson
    // name resolved from the tree (the op itself carries only `lesson_id`).
    await expect(ops.nth(1)).toContainText("Rewrite lesson “Pickups and Tone”");
    await expect(ops.nth(1)).not.toContainText("edit");
    // update_blueprint: its own label + the "existing lessons keep their
    // content" note, never an auto-redraft.
    await expect(ops.nth(2)).toContainText("Change lesson structure");
    await expect(page.getByTestId("revision-blueprint-note")).toContainText("keep their current content");

    // No raw-JSON edit affordance on this card (unlike the generic ApprovalCard).
    await expect(page.getByTestId("approval-edit")).toHaveCount(0);

    const jobId = randomUUID();
    chat.setNextResolve({ status: "job_pending", job_id: jobId });
    await page.getByTestId("revision-approve").click();

    // Applied VERBATIM: no edited_args on the resolve call.
    expect(chat.calls.resolve).toBe(1);
    expect((chat.lastBody.resolve as { decision: string; edited_args: unknown }).decision).toBe("approve");
    expect((chat.lastBody.resolve as { decision: string; edited_args: unknown }).edited_args).toBeNull();
    expect(fixture.isApplied()).toBe(true);

    await expect(page.getByTestId("revision-plan-card")).toHaveCount(0);
    await expect(page.getByTestId("chat-job-pending")).toBeVisible();
    await expect(page.getByTestId("chat-input")).toBeDisabled();

    await expect(page.getByTestId("chat-job-pending")).toBeHidden({ timeout: 10_000 });
    expect(jobs.job).toBe(2);

    // The tutor is already on the board: no "view curriculum" link, just a
    // plain narration that the revision landed.
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Done — the curriculum below now reflects this revision." }),
    ).toBeVisible();
    await expect(page.getByTestId("chat-message-link")).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    // Close the drawer (its backdrop covers the whole viewport) to reach the
    // board underneath. The board itself refetched (Task D2b's
    // `refreshTree`) and shows the newly queued lesson — expand the module
    // again (the remount that refresh forces resets the board's own
    // expand/collapse state, same as a fresh `rootId` navigation would).
    await page.getByTestId("revise-close").click();
    await page
      .locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle")
      .click();
    await expect(page.getByTestId("block-card-title").filter({ hasText: "DS-1 Distortion" })).toBeVisible();
    await expect(page.locator('[data-testid="block-card"][data-kind="lesson"]')).toHaveCount(3);

    expect(curricula.unexpected).toEqual([]);
    expect(chat.unexpected).toEqual([]);
  });

  test("reject leaves the plan unapplied and re-enables the composer", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    await mockCurriculaApi(page, fixture);
    const chat = await mockChatApi(page);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const approvalId = randomUUID();
    chat.setNextMessage({
      status: "awaiting_approval",
      approval_id: approvalId,
      tool_name: "apply_curriculum_revision",
      tool_args: {
        root_id: rootId,
        plan: {
          summary: "Remove the cables lesson.",
          ops: [{ op: "remove_lesson", lesson_id: lesson2Id, reason: "Redundant with module 2." }],
        },
      },
      description: "Here is a proposed revision.",
    });
    await page.getByTestId("chat-input").fill("Remove the cables lesson.");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("revision-plan-card")).toBeVisible();
    await expect(page.getByTestId("revision-plan-op")).toContainText("Remove lesson “Cables and Signal Integrity”");

    chat.setNextResolve({ status: "answer", content: "Understood, I left the curriculum as it was." });
    await page.getByTestId("revision-reject").click();

    expect((chat.lastBody.resolve as { decision: string }).decision).toBe("reject");
    expect(fixture.isApplied()).toBe(false);
    await expect(page.getByTestId("revision-plan-card")).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Understood, I left the curriculum as it was." }),
    ).toBeVisible();

    expect(chat.unexpected).toEqual([]);
  });
});
