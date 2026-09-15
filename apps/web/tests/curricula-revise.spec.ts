import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the "Revise with AI" drawer on the
// curriculum detail board (Unit D, Task D2b; chat overhaul Tasks 3-4):
// opening the drawer resumes (or creates) the ONE chat session bound to this
// curriculum via `GET /curricula/{root}/chat-session` — the persistence fix —
// a proposed revision renders as a `RevisionPlanCard` — never the generic
// `ApprovalCard` — with each op labeled per the controller's own resolved
// design calls (a `modify_lesson` op reads "Rewrite lesson «X»", an
// `update_blueprint` op reads "Change lesson structure" plus its own note
// that existing lessons keep their content until re-drafted), approving it
// calls `POST /chat/{id}/approvals/{id}/resolve` with the plan applied
// VERBATIM (no `edited_args`), and once the chained `curriculum_draft` job
// succeeds the board refetches and shows the newly `queued` lesson — the
// whole PLAN -> PREVIEW -> APPROVE -> APPLY loop, offline. Separate
// `test.describe` blocks below cover the reload-survives persistence fix,
// "Clear chat", and the full-screen toggle.
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
function mockCurriculumTree(
  rootId: string,
  moduleId: string,
  lesson1Id: string,
  lesson2Id: string,
  newLessonId: string,
  // Segment leaves filed under lesson1 — optional and empty by default (most
  // callers only revise at the lesson/module level); the surgical
  // `add_segment`/`edit_segment`/`remove_segment` op tests below pass one so
  // `collectTitles` (`revise-drawer.tsx`) has a REAL name to resolve, proving
  // the plan card names the exact segment rather than a generic label.
  segments: FixtureBlock[] = [],
) {
  let applied = false;
  let draftError: string | null = null;

  function tree(): FixtureBlock {
    const lessons: FixtureBlock[] = [
      {
        id: lesson1Id, kind: "lesson", title: "Pickups and Tone", body: "Pickup types and tone shaping.",
        est_minutes: 30, order: 0, language: "en", plane: "content", student_id: null,
        meta: { draft_status: "ready", objective: "Understand pickup types." }, children: segments,
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
    return {
      root_id: rootId, total, queued, drafting: 0, ready: 2, failed: 0,
      done: !applied, draft_error: draftError,
    };
  }

  return {
    tree, progress,
    markApplied: () => { applied = true; },
    markDraftFailed: (reason: string) => { draftError = reason; },
    isApplied: () => applied,
  };
}

/** Mocks the SERVER-SIDE state `GET /curricula/{root}/chat-session` keeps —
 * "the most recently created session bound to this root" — so both mocks
 * below (`mockCurriculaApi`'s GET handler and `mockChatApi`'s `POST /chat`
 * handler, which is what "Clear chat" calls) agree on which session id is
 * "current" without either one owning the other. Mirrors `routers/
 * curriculum.py`'s own `get_or_create_curriculum_chat_session`: the first GET
 * creates one; a later `POST /chat` (Clear chat, unchanged endpoint) starts a
 * NEW one and becomes what the next GET resumes. */
function createChatSessionStore() {
  let current: string | null = null;
  return {
    getOrCreate(): string {
      if (!current) current = randomUUID();
      return current;
    },
    registerCreated(sessionId: string) {
      current = sessionId;
    },
  };
}

async function mockCurriculaApi(
  page: Page,
  fixture: ReturnType<typeof mockCurriculumTree>,
  chatSessionStore: ReturnType<typeof createChatSessionStore>,
) {
  const calls = { get: 0, progress: 0, chatSession: 0 };
  const chatSessionRootIds: string[] = [];
  const unexpected: string[] = [];

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    const chatSessionMatch = pathname.match(/^\/curricula\/([^/]+)\/chat-session$/);
    if (chatSessionMatch && method === "GET") {
      calls.chatSession++;
      chatSessionRootIds.push(chatSessionMatch[1]);
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ session_id: chatSessionStore.getOrCreate() }),
      });
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
  return { calls, chatSessionRootIds, unexpected };
}

function sseBody(events: Array<{ event: string; data: unknown }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
}

/** Task 0.6 — a drawer turn is a JOB now: the composer POSTs `?async=1`, gets
 * a 202, and polls `GET /jobs/{id}` until the finished `ChatTurnOut` comes
 * back on `progress.turn`. This store is what keeps that indirection
 * invisible to the tests below, which still script a turn with
 * `setNextMessage` and assert on what reaches the screen: `accept()` mints a
 * job id for the scripted turn, and `body()` answers the poll with it.
 *
 * Shared with the `/jobs` mocks further down rather than owned by
 * `mockChatApi` alone, because a later `page.route("/jobs/**")` takes
 * priority over an earlier one — whichever mock ends up owning that route has
 * to answer the turn job too, or the drawer polls a job nobody knows about.
 * Terminal on the FIRST poll: the pending-vs-terminal cadence is the revise
 * job's subject, not this one's (`revise-async.spec.ts` covers the polling
 * itself). */
function createTurnJobStore() {
  const turns = new Map<string, Record<string, unknown>>();
  return {
    accept(turn: Record<string, unknown>): string {
      const id = randomUUID();
      turns.set(id, turn);
      return id;
    },
    /** The `GET /jobs/{id}` body for a turn job, or `null` if `id` is some
     * other job entirely (a `curriculum_revise` row, say) — the caller then
     * handles it as it always did. */
    body(id: string) {
      const turn = turns.get(id);
      if (!turn) return null;
      const now = new Date().toISOString();
      return {
        id, kind: "chat_turn", status: "succeeded", result_root_id: null,
        error: null, error_kind: null, progress: { phase: "done", turn },
        created_at: now, updated_at: now,
      };
    },
  };
}

type TurnJobStore = ReturnType<typeof createTurnJobStore>;

/** `GET /jobs/{id}` for a `chat_turn` row, when `id` is one. Returns true if
 * it answered, so every `/jobs` mock can open with one line. */
async function serveTurnJob(route: Route, id: string | undefined, turnJobs?: TurnJobStore) {
  const body = id ? turnJobs?.body(id) : null;
  if (!body) return false;
  await route.fulfill({
    status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body),
  });
  return true;
}

/** Trimmed-down mock of the Chat API — same shape as `chat.spec.ts`'s own
 * `mockChatApi`, cut to just what this drawer exercises: session creation
 * (capturing the `root_id` the drawer must pass), the stream-falls-back-to-
 * REST transport (this spec never scripts a real streamed answer — the
 * point here is the tool-call/approval path, which the stream endpoint
 * never handles at all — see `routers/chat.py`'s own documented contract),
 * one scripted `.../messages` response (with an optional artificial delay,
 * so the "Planning the revision…" status can actually be caught mid-flight),
 * and resolve.
 *
 * `chatSessionStore`, when passed, is the SAME store `mockCurriculaApi`'s
 * `GET .../chat-session` handler reads — the "Clear chat" flow calls this
 * mock's `POST /chat` (unchanged endpoint), and registering the id it hands
 * back here is what makes a LATER `GET .../chat-session` (a reopen, or a
 * reopen after a reload) resume that new session instead of the original
 * one, exactly like the real `get_or_create_curriculum_chat_session`
 * ("most-recently-created wins") does.
 *
 * `history` is a light per-session transcript — just enough for the
 * persistence tests to prove a reload resumes the SAME conversation (not
 * just the same opaque id): every `.../messages` call records the user turn
 * and, when the scripted response carries plain text, the assistant's
 * answer too; `GET /chat/{id}` reads it back. */
async function mockChatApi(
  page: Page,
  onResolve?: (approvalId: string) => void,
  chatSessionStore?: ReturnType<typeof createChatSessionStore>,
) {
  const calls = { create: 0, message: 0, resolve: 0, history: 0, pending: 0, stream: 0, list: 0, suggestions: 0 };
  const lastBody: { create?: unknown; resolve?: unknown } = {};
  const unexpected: string[] = [];
  const history = new Map<string, Array<{ id: string; role: string; content: string | null; created_at: string }>>();
  // The open approval a turn left behind, per session — what `GET
  // /chat/{id}/pending` answers with. It matters now that a drawer turn runs
  // as a job: the panel no longer applies the turn it got back, it re-hydrates
  // from the server, so an `awaiting_approval` turn has to be VISIBLE there
  // the way the real API makes it (`_respond_to_turn` opens the row before the
  // turn returns).
  const pendingApprovals = new Map<string, Record<string, unknown>>();
  const turnJobs = createTurnJobStore();

  let nextMessage: Record<string, unknown> = { status: "answer", content: "OK." };
  let nextMessageDelayMs = 0;
  let nextResolve: Record<string, unknown> = { status: "answer", content: "OK." };
  // Suggestion chips (chat overhaul, Piece B) — `[]` is the harmless default
  // the real endpoint always answers with on any internal failure, so every
  // EXISTING test in this file gets it without touching its own body (see
  // `chat.spec.ts`'s own `mockChatApi` for the identical convention).
  let nextSuggestions: Record<string, unknown> = { suggestions: [] };

  function record(sessionId: string, role: "user" | "assistant", content: string | null) {
    const rows = history.get(sessionId) ?? [];
    rows.push({ id: randomUUID(), role, content, created_at: new Date().toISOString() });
    history.set(sessionId, rows);
  }

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
      const sessionId = randomUUID();
      chatSessionStore?.registerCreated(sessionId);
      await json({ session_id: sessionId });
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

    const suggestionsMatch = pathname.match(/^\/chat\/([^/]+)\/suggestions$/);
    if (suggestionsMatch && method === "POST") {
      calls.suggestions++;
      await json(nextSuggestions);
      return;
    }

    const messagesMatch = pathname.match(/^\/chat\/([^/]+)\/messages$/);
    if (messagesMatch && method === "POST") {
      calls.message++;
      const sessionId = messagesMatch[1];
      const body = req.postDataJSON() as { content: string };
      record(sessionId, "user", body.content);
      if (nextMessageDelayMs > 0) await new Promise((r) => setTimeout(r, nextMessageDelayMs));
      // The server persists the model's narration as the trailing assistant
      // row whichever shape the turn took — a plain answer's text, or the
      // sentence that introduces a proposed mutation (which is where the
      // approval card's description comes from on a hydrate).
      const narration =
        typeof nextMessage.content === "string" ? nextMessage.content
        : typeof nextMessage.description === "string" ? nextMessage.description
        : null;
      if (narration !== null) record(sessionId, "assistant", narration);
      if (nextMessage.status === "awaiting_approval") {
        pendingApprovals.set(sessionId, {
          id: nextMessage.approval_id,
          tool_name: nextMessage.tool_name,
          tool_args: nextMessage.tool_args,
          created_at: new Date().toISOString(),
        });
      }
      // The drawer's door (Task 0.6): `?async=1` gets a 202 and the scripted
      // turn rides home on the job. Every turn sent from this drawer takes it.
      if (new URL(req.url()).searchParams.get("async") === "1") {
        await json({ job_id: turnJobs.accept(nextMessage), status: "pending" }, 202);
        return;
      }
      await json(nextMessage);
      return;
    }

    const resolveMatch = pathname.match(/^\/chat\/([^/]+)\/approvals\/([^/]+)\/resolve$/);
    if (resolveMatch && method === "POST") {
      calls.resolve++;
      lastBody.resolve = req.postDataJSON();
      pendingApprovals.delete(resolveMatch[1]);
      onResolve?.(resolveMatch[2]);
      await json(nextResolve);
      return;
    }

    const pendingMatch = pathname.match(/^\/chat\/([^/]+)\/pending$/);
    if (pendingMatch && method === "GET") {
      calls.pending++;
      await json(pendingApprovals.get(pendingMatch[1]) ?? null);
      return;
    }

    const sessionMatch = pathname.match(/^\/chat\/([^/]+)$/);
    if (sessionMatch && method === "GET") {
      calls.history++;
      await json(history.get(sessionMatch[1]) ?? []);
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
  // Every turn is a job, so every test in this file polls `/jobs` — including
  // the ones with no `mockJobsApi` of their own. A test that DOES register one
  // registers it later and therefore wins this route; that mock delegates back
  // through `serveTurnJob`.
  await page.route(`${API_ORIGIN}/jobs/**`, async (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const { pathname } = new URL(req.url());
    if (await serveTurnJob(route, pathname.match(/^\/jobs\/([^/]+)$/)?.[1], turnJobs)) return;
    unexpected.push(`${req.method()} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });

  return {
    calls,
    lastBody,
    unexpected,
    turnJobs,
    setNextMessage(value: Record<string, unknown>, delayMs = 0) {
      nextMessage = value;
      nextMessageDelayMs = delayMs;
    },
    setNextResolve(value: Record<string, unknown>) {
      nextResolve = value;
    },
    /** Queues the NEXT `.../suggestions` response — see `chat.spec.ts`'s
     * identical helper for why `[]` is the default this doesn't need to touch. */
    setNextSuggestions(value: string[]) {
      nextSuggestions = { suggestions: value };
    },
  };
}

/** `GET /jobs/{id}` only — same "pending once, then terminal" shape
 * `chat.spec.ts`'s own `mockJobsApi` uses, trimmed to this file's one job. */
async function mockJobsApi(page: Page, { pendingPolls = 1, turnJobs }: { pendingPolls?: number; turnJobs?: TurnJobStore } = {}) {
  let polls = 0;
  const calls = { job: 0 };

  await page.route(`${API_ORIGIN}/jobs/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    const match = pathname.match(/^\/jobs\/([^/]+)$/);
    // A `chat_turn` row first — it is not this mock's job and must not spend
    // its pending/terminal cadence or show up in `calls.job`.
    if (await serveTurnJob(route, match?.[1], turnJobs)) return;
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

/** Two-stage `GET /jobs/{id}` mock for the chained-draft-failure test: the
 * `curriculum_revise` row succeeds (the tree is right) carrying the chained
 * `curriculum_draft` row's id on `progress.draft_job_id`, and that draft row is
 * `failed` — the exact silent-"queued" shape the fix surfaces. */
async function mockJobsRevisePlusFailedDraft(
  page: Page,
  { reviseJobId, draftJobId, draftError, turnJobs }:
    { reviseJobId: string; draftJobId: string; draftError: string; turnJobs?: TurnJobStore },
) {
  const calls = { revise: 0, draft: 0 };
  await page.route(`${API_ORIGIN}/jobs/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    const match = pathname.match(/^\/jobs\/([^/]+)$/);
    const id = match?.[1];
    if (await serveTurnJob(route, id, turnJobs)) return;
    const base = { created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (id === reviseJobId) {
      calls.revise++;
      // Pending once, then succeeded WITH the chained draft id on progress.
      if (calls.revise <= 1) {
        await json({ id, ...base, kind: "curriculum_revise", status: "pending", result_root_id: null, error: null, error_kind: null });
      } else {
        await json({
          id, ...base, kind: "curriculum_revise", status: "succeeded", result_root_id: null,
          error: null, error_kind: null, progress: { phase: "drafting", draft_job_id: draftJobId },
        });
      }
      return;
    }
    if (id === draftJobId) {
      calls.draft++;
      await json({
        id, ...base, kind: "curriculum_draft", status: "failed", result_root_id: null,
        error: draftError, error_kind: "upstream", progress: null,
      });
      return;
    }
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });
  return calls;
}

/** Same two-stage shape as `mockJobsRevisePlusFailedDraft` above, but for the
 * chat overhaul Task 5 scenario: the chained `curriculum_draft` row reports
 * `status="succeeded"` — the 429 RULE means a rate-limited lesson goes back
 * to `queued`, NOT `failed`, so the job finishes "successfully" while a
 * lesson still needs writing. Paired in the test with a curriculum fixture
 * whose post-apply `progress()` reports `queued > 0` (`mockCurriculumTree`'s
 * own default — the newly inserted lesson is genuinely still `queued` right
 * after apply, which is exactly what a rate-limited requeue also looks
 * like). */
async function mockJobsReviseePlusSucceededDraft(
  page: Page,
  { reviseJobId, draftJobId, turnJobs }:
    { reviseJobId: string; draftJobId: string; turnJobs?: TurnJobStore },
) {
  const calls = { revise: 0, draft: 0 };
  await page.route(`${API_ORIGIN}/jobs/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    const match = pathname.match(/^\/jobs\/([^/]+)$/);
    const id = match?.[1];
    if (await serveTurnJob(route, id, turnJobs)) return;
    const base = { created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (id === reviseJobId) {
      calls.revise++;
      if (calls.revise <= 1) {
        await json({ id, ...base, kind: "curriculum_revise", status: "pending", result_root_id: null, error: null, error_kind: null });
      } else {
        await json({
          id, ...base, kind: "curriculum_revise", status: "succeeded", result_root_id: null,
          error: null, error_kind: null, progress: { phase: "drafting", draft_job_id: draftJobId },
        });
      }
      return;
    }
    if (id === draftJobId) {
      calls.draft++;
      await json({
        id, ...base, kind: "curriculum_draft", status: "succeeded", result_root_id: null,
        error: null, error_kind: null, progress: null,
      });
      return;
    }
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
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
    const chatSessionStore = createChatSessionStore();
    const curricula = await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, () => fixture.markApplied(), chatSessionStore);
    const jobs = await mockJobsApi(page, { pendingPolls: 1, turnJobs: chat.turnJobs });

    await page.goto(`/en/curricula/${rootId}`);
    await expect(page.getByTestId("tree-board")).toBeVisible();

    // The drawer is closed by default and no session exists yet.
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);
    expect(curricula.calls.chatSession).toBe(0);

    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("revise-drawer")).toBeVisible();

    // Resumes (or creates) the ONE session bound to THIS curriculum via the
    // curriculum-scoped GET-or-create endpoint — never a bare `POST /chat`.
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(curricula.calls.chatSession).toBe(1);
    expect(curricula.chatSessionRootIds).toEqual([rootId]);
    expect(chat.calls.create).toBe(0);

    // Re-opening (closing then re-opening) must not spend a second session.
    await page.getByTestId("revise-close").click();
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(curricula.calls.chatSession).toBe(1);

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
            // Server-computed by `compute_impact` (`curriculum/revise.py`,
            // 2026-07-20, Spec A) — one `modify_lesson` rewrite above, no
            // removals, so `destructive` is true on `rewrites` alone.
            impact: {
              rewrites: 1, segment_additions: 0, segment_edits: 0, segment_removals: 0,
              lesson_removals: 0, lessons_added: 1, blueprint_changed: true, destructive: true,
            },
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

    // The honest blast-radius banner (Spec A, Task 5) — amber, because a
    // `modify_lesson` rewrite is present, naming ONLY the rewrite count
    // (removals is 0, so that sentence drops out entirely).
    const impactBanner = page.getByTestId("plan-impact");
    await expect(impactBanner).toBeVisible();
    await expect(impactBanner).toHaveAttribute("data-destructive", "true");
    await expect(impactBanner).toContainText("1 lesson will be rewritten from scratch");
    await expect(impactBanner).toContainText("existing text will be replaced");
    await expect(impactBanner).not.toContainText("removed");

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

  test("a chained-draft failure surfaces in the chat AND on the board — never a silent queued", async ({
    page,
  }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();
    const reviseJobId = randomUUID();
    const draftJobId = randomUUID();
    const draftError = "No lesson could be drafted. Check Settings, then Resume.";

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    // The apply lands (tree gets the new queued lesson) but the chained draft dies.
    const chat = await mockChatApi(page, () => {
      fixture.markApplied();
      fixture.markDraftFailed(draftError);
    }, chatSessionStore);
    await mockJobsRevisePlusFailedDraft(page, { reviseJobId, draftJobId, draftError, turnJobs: chat.turnJobs });

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
          summary: "Add a DS-1 lesson.",
          ops: [{
            op: "insert_lesson", module_id: moduleId, title: "DS-1 Distortion",
            objective: "distortion basics", reason: "Fills a gap.",
          }],
        },
      },
      description: "Here is a proposed revision.",
    });
    await page.getByTestId("chat-input").fill("Add a DS-1 lesson.");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("revision-plan-card")).toBeVisible();

    chat.setNextResolve({ status: "job_pending", job_id: reviseJobId });
    await page.getByTestId("revision-approve").click();

    // The chat does NOT stop at a bare "applied": it waits on the chained draft
    // and reports the failure with the job's own reason.
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "writing the new lessons failed" }),
    ).toBeVisible({ timeout: 10_000 });
    // The REASON is the localized one, never the job's own English string:
    // `jobErrorText` maps `error_kind: "upstream"` to `jobErrors.generic`.
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "This failed unexpectedly. Try again." }),
    ).toBeVisible();
    await expect(page.getByTestId("chat-message").filter({ hasText: draftError })).toHaveCount(0);
    // ...and never the plain success narration.
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Done — the curriculum below now reflects this revision." }),
    ).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    // The board, underneath, shows the SAME reason on its progress bar rather
    // than a silent "queued".
    await page.getByTestId("revise-close").click();
    await expect(page.getByTestId("draft-progress-draft-error")).toBeVisible();
    await expect(page.getByTestId("draft-progress-draft-error")).toContainText(draftError);
  });

  test("reject leaves the plan unapplied and re-enables the composer", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, undefined, chatSessionStore);

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

  // Spec A / Task 5 — the three surgical segment ops (`add_segment`,
  // `edit_segment`, `remove_segment`) and the calm register of the impact
  // banner: a plan with none of `rewrites`/`lesson_removals`/
  // `segment_removals` is NOT destructive, so the banner names only the
  // additive/edit counts instead of going amber.
  test("a surgical-only revision (segment ops) shows the calm banner and the new op labels", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();
    const segmentId = randomUUID();

    // A real, titled segment under lesson1 — so `edit_segment`'s
    // `blockTitles[segment_id]` lookup resolves to an ACTUAL name, not the
    // generic `unknownBlock` fallback (2026-07-20 review follow-up: the
    // approval card must name the exact segment a surgical op targets).
    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId, [
      {
        id: segmentId, kind: "segment", title: "Tone Controls Explained", body: "How tone knobs shape sound.",
        est_minutes: 5, order: 0, language: "en", plane: "content", student_id: null,
        meta: null, children: [],
      },
    ]);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, undefined, chatSessionStore);

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
          summary: "Add a tuning-check segment and tighten one segment's wording.",
          ops: [
            {
              op: "add_segment",
              lesson_id: lesson1Id,
              title: "Quick tuning check",
              instruction: "Add a short note on checking tuning before practice.",
              reason: "The tutor asked for a quick tuning reminder.",
            },
            {
              op: "edit_segment",
              segment_id: segmentId,
              instruction: "Tighten the wording, no new content.",
              reason: "The paragraph on tone was too wordy.",
            },
          ],
          // No rewrites/removals anywhere in this plan, so `compute_impact`
          // marks it non-destructive.
          impact: {
            rewrites: 0, segment_additions: 1, segment_edits: 1, segment_removals: 0,
            lesson_removals: 0, lessons_added: 0, blueprint_changed: false, destructive: false,
          },
        },
      },
      description: "Here is a proposed revision.",
    });
    await page.getByTestId("chat-input").fill("Add a tuning tip and tighten one segment.");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("revision-plan-card")).toBeVisible();

    const ops = page.getByTestId("revision-plan-op");
    await expect(ops).toHaveCount(2);
    // add_segment carries its OWN proposed title — never a bare "to lesson".
    await expect(ops.nth(0)).toContainText("Add segment “Quick tuning check” to lesson “Pickups and Tone”");
    // edit_segment carries only a bare `segment_id` — the card must resolve
    // it to the REAL segment name via `blockTitles`, exactly like
    // `modify_lesson` resolves a bare `lesson_id`. `remove_segment` shares
    // the same resolution and is destructive, so a tutor seeing a generic
    // "Edit segment"/"Remove segment" here would be approving blind.
    await expect(ops.nth(1)).toContainText("Edit segment “Tone Controls Explained”");

    const impactBanner = page.getByTestId("plan-impact");
    await expect(impactBanner).toBeVisible();
    await expect(impactBanner).toHaveAttribute("data-destructive", "false");
    await expect(impactBanner).toContainText(
      "Targeted change: 1 new segment, 1 edit — everything else stays as is.",
    );

    chat.setNextResolve({ status: "answer", content: "Understood, I left the curriculum as it was." });
    await page.getByTestId("revision-reject").click();

    expect(chat.unexpected).toEqual([]);
  });

  // 2026-07-20 hotfix: `validate_ops` can silently drop a proposed op (a
  // missing required field, a shorthand id that never resolves, a section_key
  // the blueprint doesn't have). Before this, the plan the tutor saw on the
  // approval card gave no sign that anything had been cut — he'd approve a
  // SMALLER plan than what he asked for without knowing it. `plan.dropped`
  // (server-computed, same story as `plan.impact`) must render as an amber
  // warning naming what was rejected and why, BEFORE he approves.
  test("a plan with dropped ops shows the amber warning with the rejection reasons", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, undefined, chatSessionStore);

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
          summary: "Enable homework and file it under the Pickups lesson.",
          ops: [
            {
              op: "set_section_enabled",
              section_key: "homework",
              enabled: true,
              reason: "The tutor asked for a homework section.",
            },
          ],
          impact: {
            rewrites: 0, segment_additions: 0, segment_edits: 0, segment_removals: 0,
            lesson_removals: 0, lessons_added: 0, blueprint_changed: true, destructive: false,
          },
          // The repair pass survived ONE op but still lost this add_segment —
          // mirrors the production incident's shape (`curriculum/revise.py`'s
          // `validate_ops`), rendered verbatim by `RevisionPlanCard`.
          dropped: [
            {
              op: { op: "add_segment", lesson_id: "L1", section_key: "homework" },
              reason: "lesson_id='L1' does not resolve",
            },
          ],
        },
      },
      description: "Here is a proposed revision.",
    });
    await page.getByTestId("chat-input").fill("Add a homework section to every lesson.");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("revision-plan-card")).toBeVisible();

    // The new op label, enable variant.
    const ops = page.getByTestId("revision-plan-op");
    await expect(ops).toHaveCount(1);
    await expect(ops.nth(0)).toContainText("Enable section “homework”");

    const droppedWarning = page.getByTestId("plan-dropped");
    await expect(droppedWarning).toBeVisible();
    await expect(droppedWarning).toContainText("1 proposed change was rejected");
    await expect(droppedWarning).toContainText("see why before you approve");
    const reasons = page.getByTestId("plan-dropped-reason");
    await expect(reasons).toHaveCount(1);
    await expect(reasons.nth(0)).toContainText("add_segment");
    await expect(reasons.nth(0)).toContainText("lesson_id='L1' does not resolve");

    chat.setNextResolve({ status: "answer", content: "Understood, I left the curriculum as it was." });
    await page.getByTestId("revision-reject").click();

    expect(chat.unexpected).toEqual([]);
  });

  test("a plan with no dropped ops renders no warning", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, undefined, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    chat.setNextMessage({
      status: "awaiting_approval",
      approval_id: randomUUID(),
      tool_name: "apply_curriculum_revision",
      tool_args: {
        root_id: rootId,
        plan: {
          summary: "Enable homework everywhere.",
          ops: [
            { op: "set_section_enabled", section_key: "homework", enabled: true, reason: "r" },
          ],
          dropped: [],
        },
      },
      description: "Here is a proposed revision.",
    });
    await page.getByTestId("chat-input").fill("Add homework.");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("revision-plan-card")).toBeVisible();

    await expect(page.getByTestId("plan-dropped")).toHaveCount(0);

    chat.setNextResolve({ status: "answer", content: "Understood, I left the curriculum as it was." });
    await page.getByTestId("revision-reject").click();

    expect(chat.unexpected).toEqual([]);
  });
});

// Chat overhaul Task 4 — persistence. Before this, `handleOpen` called
// `createChatSession` unconditionally: fine within one page visit (this
// component's own `sessionId || creating` guard already kept it to one call
// per mount, which the tests above pin), but a page RELOAD resets that React
// state, so every reload spent a brand-new session and orphaned whatever
// conversation was already under way. These tests prove the fix works where
// it counts: ACROSS A RELOAD, and through the explicit "Clear chat" escape
// hatch.
test.describe("curriculum revise drawer — chat session persistence (mocked API)", () => {
  test("a reload resumes the SAME curriculum chat session, not a new one", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    const curricula = await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, undefined, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(curricula.calls.chatSession).toBe(1);

    chat.setNextMessage({ status: "answer", content: "A DS-1 sits well after the Tube Screamer." });
    await page.getByTestId("chat-input").fill("Where should a DS-1 go?");
    await page.getByTestId("chat-send").click();
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "A DS-1 sits well after the Tube Screamer." }),
    ).toBeVisible();

    // A full page reload — the drawer's own React state (and the whole
    // component tree) is gone. Re-opening must resume the SAME session, and
    // its transcript, not spend a fresh one.
    await page.reload();
    await expect(page.getByTestId("tree-board")).toBeVisible();
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);

    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    // Called again (a genuinely new mount), but resolved to the SAME session
    // — no `POST /chat` was ever needed for this, and the prior exchange is
    // still there.
    expect(curricula.calls.chatSession).toBe(2);
    expect(chat.calls.create).toBe(0);
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Where should a DS-1 go?" }),
    ).toBeVisible();
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "A DS-1 sits well after the Tube Screamer." }),
    ).toBeVisible();

    expect(curricula.unexpected).toEqual([]);
    expect(chat.unexpected).toEqual([]);
  });

  test("Clear chat starts a fresh conversation, and a later reload resumes THAT one", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    const curricula = await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, undefined, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    chat.setNextMessage({ status: "answer", content: "Sure, here's the old answer." });
    await page.getByTestId("chat-input").fill("A question for the old conversation");
    await page.getByTestId("chat-send").click();
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Sure, here's the old answer." }),
    ).toBeVisible();

    // Clear chat is guarded by a NON-destructive confirm (nothing is
    // deleted — the old session just stops being the one resumed here).
    await page.getByTestId("revise-clear-chat").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-cancel").click();
    await expect(page.getByTestId("confirm-dialog")).toHaveCount(0);
    // Cancelling changed nothing.
    expect(chat.calls.create).toBe(0);
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Sure, here's the old answer." }),
    ).toBeVisible();

    await page.getByTestId("revise-clear-chat").click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-accept").click();

    // A brand-new session, bound to the SAME curriculum — and the panel
    // remounted onto it: the old exchange is gone from view (a different,
    // empty conversation), not because it was deleted.
    expect(chat.calls.create).toBe(1);
    expect((chat.lastBody.create as { root_id?: string }).root_id).toBe(rootId);
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Sure, here's the old answer." }),
    ).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    // Close, reload, and reopen: the NEW session (not the original one) is
    // what a fresh mount resumes from now on — "most recently created wins".
    await page.getByTestId("revise-close").click();
    await page.reload();
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    expect(curricula.calls.chatSession).toBe(2);
    expect(chat.calls.create).toBe(1); // Clear chat is still the only POST /chat ever made.
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Sure, here's the old answer." }),
    ).toHaveCount(0);

    expect(curricula.unexpected).toEqual([]);
    expect(chat.unexpected).toEqual([]);
  });
});

// Chat overhaul Task 3 — full-screen toggle.
test.describe("curriculum revise drawer — full-screen toggle (mocked API)", () => {
  test("expands the drawer to full screen and back, and the choice survives a close/re-open", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    await mockChatApi(page, undefined, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const drawer = page.getByTestId("revise-drawer");
    const panel = drawer.locator("aside");
    await expect(drawer).toHaveAttribute("data-fullscreen", "false");
    // `max-w-lg`, widened from `md` (2026-09-15): at 1366×768 the `md` panel
    // squeezed the composer and the scope block into ~380px of Greek prose.
    // The assertion is still "the side panel has its side-panel width", it is
    // just a different number now.
    await expect(panel).toHaveClass(/max-w-lg/);

    await page.getByTestId("revise-fullscreen-toggle").click();
    await expect(drawer).toHaveAttribute("data-fullscreen", "true");
    await expect(panel).toHaveClass(/max-w-full/);
    // The transcript survives the toggle — it's a layout change, not a remount.
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    // Back to the side panel.
    await page.getByTestId("revise-fullscreen-toggle").click();
    await expect(drawer).toHaveAttribute("data-fullscreen", "false");
    await expect(panel).toHaveClass(/max-w-lg/);

    // Expand again, then close and re-open — plain component state, so the
    // choice survives the close/re-open (the component itself never
    // unmounts; only the `{open && ...}` block does).
    await page.getByTestId("revise-fullscreen-toggle").click();
    await expect(drawer).toHaveAttribute("data-fullscreen", "true");
    await page.getByTestId("revise-close").click();
    await expect(drawer).toHaveCount(0);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("revise-drawer")).toHaveAttribute("data-fullscreen", "true");
  });

  // Chat overhaul, Piece B review follow-up: in full-screen mode the backdrop
  // button is entirely covered by the drawer itself (`max-w-full` leaves
  // nothing to click), so the X in the corner used to be the ONLY way out.
  // Escape must close it regardless of where focus sits — including inside
  // the chat composer input, which is the realistic case (the tutor was just
  // typing).
  test("Escape closes the drawer in full-screen mode, even while the composer has focus", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    await mockChatApi(page, undefined, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const drawer = page.getByTestId("revise-drawer");
    await page.getByTestId("revise-fullscreen-toggle").click();
    await expect(drawer).toHaveAttribute("data-fullscreen", "true");

    await page.getByTestId("chat-input").click();
    await page.keyboard.press("Escape");
    await expect(drawer).toHaveCount(0);
  });

  test("Escape also closes the drawer in the ordinary side-panel mode", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    await mockChatApi(page, undefined, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("revise-drawer")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("revise-drawer")).toHaveCount(0);
  });
});

// Chat overhaul Task 5 (review fix) — accurate "applied" status. Before this,
// a chained draft job reporting `status="succeeded"` always meant a flat
// "applied", even when the 429 rate-limit rule (`jobs/curriculum_draft.py`)
// requeued a lesson back to `queued` instead of marking it `failed` — a
// silent gap between what the chat said and what the board actually showed.
test.describe("curriculum revise drawer — accurate applied status (mocked API)", () => {
  test("a succeeded draft job that still leaves a lesson queued says so, not a flat 'applied'", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();
    const reviseJobId = randomUUID();
    const draftJobId = randomUUID();

    // `mockCurriculumTree`'s own default: once applied, the new lesson is
    // `queued` on `/progress` (`markApplied` -> `queued: 1`) — precisely the
    // shape a rate-limited requeue leaves behind, whether or not any
    // drafting actually ran.
    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);
    const chat = await mockChatApi(page, () => fixture.markApplied(), chatSessionStore);
    await mockJobsReviseePlusSucceededDraft(page, { reviseJobId, draftJobId, turnJobs: chat.turnJobs });

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
          summary: "Add a DS-1 lesson.",
          ops: [{
            op: "insert_lesson", module_id: moduleId, title: "DS-1 Distortion",
            objective: "distortion basics", reason: "Fills a gap.",
          }],
        },
      },
      description: "Here is a proposed revision.",
    });
    await page.getByTestId("chat-input").fill("Add a DS-1 lesson.");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("revision-plan-card")).toBeVisible();

    chat.setNextResolve({ status: "job_pending", job_id: reviseJobId });
    await page.getByTestId("revision-approve").click();

    // The chained draft job settles "succeeded" — but `/progress` still shows
    // the new lesson `queued`, so the chat must say a resume is needed rather
    // than the flat success narration.
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "some lessons still need writing" }),
    ).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByTestId("chat-message").filter({ hasText: "Done — the curriculum below now reflects this revision." }),
    ).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    expect(chat.unexpected).toEqual([]);
  });
});

// Whole-branch review, finding #4 — a segment's lifecycle (`queued` while it
// waits for `generate_segment`, `failed` if that call raised) was invisible
// anywhere in the UI: a tutor looking at the board had no way to tell a
// stranded `queued` segment apart from a `done` one with a short body, and no
// way to see WHY one had failed short of checking the API by hand. Reuses
// `mockCurriculumTree`'s existing `segments` param (already exercised above
// for the revise plan card's op-labeling) with `meta.segment_status` set —
// this time to render the board itself, never opening the revise drawer.
test.describe("curriculum board — segment status chip (mocked API)", () => {
  test("a queued segment and a failed segment each render their own status chip", async ({ page }) => {
    const rootId = randomUUID();
    const moduleId = randomUUID();
    const lesson1Id = randomUUID();
    const lesson2Id = randomUUID();
    const newLessonId = randomUUID();
    const queuedSegmentId = randomUUID();
    const failedSegmentId = randomUUID();

    const fixture = mockCurriculumTree(rootId, moduleId, lesson1Id, lesson2Id, newLessonId, [
      {
        id: queuedSegmentId, kind: "segment", title: "Quick tuning check", body: "",
        est_minutes: 5, order: 0, language: "en", plane: "content", student_id: null,
        meta: { segment_status: "queued" }, children: [],
      },
      {
        id: failedSegmentId, kind: "segment", title: "Bonus: Pedal chains", body: "",
        est_minutes: 5, order: 1, language: "en", plane: "content", student_id: null,
        meta: { segment_status: "failed", segment_error: "the model is down" }, children: [],
      },
    ]);
    const chatSessionStore = createChatSessionStore();
    await mockCurriculaApi(page, fixture, chatSessionStore);

    await page.goto(`/en/curricula/${rootId}`);

    // Expand module -> lesson1 to reach the segment cards underneath.
    await page.locator('[data-testid="block-card"][data-kind="module"]')
      .getByTestId("block-card-toggle").click();
    await page.locator('[data-testid="block-card"][data-kind="lesson"]').first()
      .getByTestId("block-card-toggle").click();

    const segmentCards = page.locator('[data-testid="block-card"][data-kind="segment"]');
    await expect(segmentCards).toHaveCount(2);

    const queuedChip = segmentCards.nth(0).getByTestId("segment-status");
    await expect(queuedChip).toBeVisible();
    await expect(queuedChip).toHaveAttribute("data-status", "queued");
    await expect(queuedChip).toHaveText("Waiting…");

    const failedChip = segmentCards.nth(1).getByTestId("segment-status");
    await expect(failedChip).toBeVisible();
    await expect(failedChip).toHaveAttribute("data-status", "failed");
    await expect(failedChip).toHaveText("Failed");
    await expect(failedChip).toHaveAttribute("title", "the model is down");
  });
});
