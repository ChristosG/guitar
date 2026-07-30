import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Deterministic, offline coverage of the Chat cockpit page (Plan 5 Task 5):
// every `/chat/*` and `/jobs/*` call is intercepted via `page.route` and
// answered from a small in-memory fixture — no backend/model call needed.
// This proves the UI wiring (composer -> POST .../messages; an
// `awaiting_approval` turn -> ApprovalCard + a disabled composer; Approve/
// Reject -> POST .../resolve; a `job_pending` resolve -> poll `GET /jobs/{id}`
// -> success narration + curriculum link), not the agent loop/model behavior
// itself (that's Plan 5 Tasks 2-4's own tests on the API side).
//
// Anchored to `API_ORIGIN` with two `page.route` calls (a bare `/chat` +
// `/chat/**`) — same origin-anchored convention `cockpit.spec.ts` settled on,
// and for the same reason both `cockpit.spec.ts` and `artifacts.spec.ts`
// document explicitly: a bare suffix glob like `**/chat/**` would ALSO match
// this app's own same-origin `/en/chat` page URL, and
// `**/chat/**` specifically would never even match the bare `POST /chat`
// session-create call in the first place — that URL has no "/chat/"
// substring for the glob's trailing `/**` to anchor on. Anchoring on origin
// instead sidesteps both problems.
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
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

/** Encodes a list of `{event, data}` pairs as an SSE response body — the
 * exact wire shape `app/routers/chat.py`'s `POST .../messages/stream`
 * produces (see that module's docstring): one `event: <name>\ndata:
 * <json>\n\n` block per server-sent event, in order. */
function sseBody(events: Array<{ event: string; data: unknown }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
}

/** Wires up an in-memory mock of the Chat API. Each write endpoint's NEXT
 * response is queued explicitly by the test right before the action that
 * triggers it (`setNextMessage`/`setNextResolve`/`setNextStream`) — simpler
 * than modeling the real agent loop.
 *
 * It does, however, REMEMBER (Plan 13 Stage 5.6): every turn it answers is
 * also recorded into a transcript, an approval left open stays open, and a
 * title is derived from the first user message — so `GET /chat`,
 * `GET /chat/{id}` and `GET /chat/{id}/pending` answer with state the tests'
 * own earlier actions produced. Without that, "reload and the conversation is
 * still there" would be asserting against a fixture rather than against
 * anything the UI did, and the reload proof would be worthless.
 *
 * The stream endpoint defaults to a single `event: fallback` SSE response
 * (mirroring the real API's own honest-simplification contract — Plan 11
 * Task 3, C4: the streaming endpoint only ever succeeds for a genuine
 * plain-answer turn and falls back to nothing otherwise, leaving the CLIENT
 * to retry via the existing REST `.../messages` endpoint) so every EXISTING
 * test in this file (written before streaming existed) still exercises the
 * exact same REST approve/reject/job flow it always did, with zero changes
 * to those tests' own bodies — `chat-panel.tsx` always attempts the stream
 * first, and a `fallback` event is this mock's default, deliberately, so
 * that attempt is a harmless no-op ahead of the REST call those tests
 * already script via `setNextMessage`. */
interface MockRow {
  id: string;
  role: "user" | "assistant";
  content: string | null;
  created_at: string;
  citations?: unknown;
}

interface MockPending {
  id: string;
  tool_name: string;
  tool_args: Record<string, unknown>;
  status: string;
  created_at: string;
}

async function mockChatApi(page: Page) {
  const calls = {
    create: 0, message: 0, resolve: 0, history: 0, pending: 0, stream: 0, list: 0, rename: 0,
    remove: 0, suggestions: 0,
  };
  const lastBody: { message?: unknown; resolve?: unknown; stream?: unknown; rename?: unknown } = {};
  const unexpected: string[] = [];
  let nextMessage: Record<string, unknown> = { status: "answer", content: "OK." };
  let nextResolve: Record<string, unknown> = { status: "answer", content: "OK." };
  let nextStream: string = sseBody([{ event: "fallback", data: { reason: "tool_call" } }]);
  // Suggestion chips (chat overhaul, Piece B) — the real endpoint always
  // answers 200 (see `routers/chat.py`'s `get_chat_suggestions`: every
  // internal failure degrades to `{"suggestions": []}` rather than a
  // 4xx/5xx), so `[]` is the harmless default every EXISTING test in this
  // file gets without touching its own body — this fetch fires non-blocking
  // after an assistant answer renders and none of those tests assert on it.
  let nextSuggestions: Record<string, unknown> = { suggestions: [] };

  // The PERSISTED state — the whole point of Stage 5.6 and the reason this
  // mock got a memory. `history`/`pending` are what a reload reads back, and
  // `titles` is the server's own "name a session after its first user
  // message" rule (a truncation, no model call).
  const history = new Map<string, MockRow[]>();
  const pending = new Map<string, MockPending>();
  const titles = new Map<string, string>();
  /** Most-recently-active first, and — exactly like `GET /chat`'s INNER JOIN
   * — a session appears only once it has a message. */
  let order: string[] = [];

  function record(sessionId: string, rows: Array<Omit<MockRow, "id" | "created_at">>) {
    const existing = history.get(sessionId) ?? [];
    const stamped = rows.map((r) => ({ ...r, id: randomUUID(), created_at: new Date().toISOString() }));
    history.set(sessionId, [...existing, ...stamped]);

    const firstUser = [...existing, ...stamped].find((r) => r.role === "user" && r.content);
    if (firstUser?.content && !titles.has(sessionId)) {
      titles.set(sessionId, firstUser.content.slice(0, 60));
    }
    order = [sessionId, ...order.filter((id) => id !== sessionId)];
  }

  function summaries() {
    return order.map((id) => {
      const rows = (history.get(id) ?? []).filter((r) => r.content);
      const now = new Date().toISOString();
      return {
        id,
        title: titles.get(id) ?? null,
        locale: "en",
        student_id: null,
        created_at: now,
        updated_at: now,
        message_count: rows.length,
        last_message_at: rows.at(-1)?.created_at ?? now,
        preview: rows.at(-1)?.content ?? null,
      };
    });
  }

  /** Applies whatever a scripted turn response implies for the PERSISTED
   * state, so the mock stays self-consistent under a reload: an
   * `awaiting_approval` turn leaves an open approval AND an assistant row
   * carrying the narration — which is precisely where the resumed card's
   * description has to come from (`MessageOut` has no description field). */
  function applyTurn(sessionId: string, turn: Record<string, unknown>) {
    if (turn.status === "awaiting_approval") {
      pending.set(sessionId, {
        id: String(turn.approval_id),
        tool_name: String(turn.tool_name),
        tool_args: (turn.tool_args ?? {}) as Record<string, unknown>,
        status: "pending",
        created_at: new Date().toISOString(),
      });
      record(sessionId, [{ role: "assistant", content: (turn.description as string) ?? null }]);
      return;
    }
    if (typeof turn.content === "string") {
      record(sessionId, [{ role: "assistant", content: turn.content, citations: turn.citations }]);
    }
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
      await json({ session_id: randomUUID() });
      return;
    }

    if (pathname === "/chat" && method === "GET") {
      calls.list++;
      await json(summaries());
      return;
    }

    const streamMatch = pathname.match(/^\/chat\/([^/]+)\/messages\/stream$/);
    if (streamMatch && method === "POST") {
      calls.stream++;
      const body = req.postDataJSON() as { content: string };
      lastBody.stream = body;
      // A stream only PERSISTS on `done` — a `fallback` writes nothing at
      // all, which is exactly what makes the client's REST retry safe (see
      // `post_message_stream`'s docstring). Mirrored here so a fallback
      // followed by a REST send doesn't double-record the user turn.
      if (nextStream.includes("event: done")) {
        const text = [...nextStream.matchAll(/event: delta\ndata: (.+)\n\n/g)]
          .map((m) => (JSON.parse(m[1]) as { text: string }).text)
          .join("");
        const done = nextStream.match(/event: done\ndata: (.+)\n\n/);
        const citations = done ? (JSON.parse(done[1]) as { citations: unknown }).citations : null;
        record(streamMatch[1], [
          { role: "user", content: body.content },
          { role: "assistant", content: text, citations },
        ]);
      }
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: CORS_HEADERS,
        body: nextStream,
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
      const body = req.postDataJSON() as { content: string };
      lastBody.message = body;
      record(messagesMatch[1], [{ role: "user", content: body.content }]);
      applyTurn(messagesMatch[1], nextMessage);
      await json(nextMessage);
      return;
    }

    const resolveMatch = pathname.match(/^\/chat\/([^/]+)\/approvals\/([^/]+)\/resolve$/);
    if (resolveMatch && method === "POST") {
      calls.resolve++;
      lastBody.resolve = req.postDataJSON();
      pending.delete(resolveMatch[1]);
      applyTurn(resolveMatch[1], nextResolve);
      await json(nextResolve);
      return;
    }

    const pendingMatch = pathname.match(/^\/chat\/([^/]+)\/pending$/);
    if (pendingMatch && method === "GET") {
      calls.pending++;
      await json(pending.get(pendingMatch[1]) ?? null);
      return;
    }

    const sessionMatch = pathname.match(/^\/chat\/([^/]+)$/);
    if (sessionMatch && method === "GET") {
      calls.history++;
      await json(history.get(sessionMatch[1]) ?? []);
      return;
    }
    if (sessionMatch && method === "PATCH") {
      calls.rename++;
      const body = req.postDataJSON() as { title: string };
      lastBody.rename = body;
      titles.set(sessionMatch[1], body.title);
      const now = new Date().toISOString();
      await json({
        id: sessionMatch[1], title: body.title, locale: "en", student_id: null,
        created_at: now, updated_at: now,
      });
      return;
    }
    if (sessionMatch && method === "DELETE") {
      calls.remove++;
      // The cascade, mocked: transcript and open approval go with it.
      history.delete(sessionMatch[1]);
      pending.delete(sessionMatch[1]);
      titles.delete(sessionMatch[1]);
      order = order.filter((id) => id !== sessionMatch[1]);
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
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

  await page.route(`${API_ORIGIN}/chat`, handler);
  await page.route(`${API_ORIGIN}/chat/**`, handler);

  return {
    calls,
    lastBody,
    unexpected,
    summaries,
    setNextMessage(value: Record<string, unknown>) {
      nextMessage = value;
    },
    setNextResolve(value: Record<string, unknown>) {
      nextResolve = value;
    },
    /** Queues the raw SSE body the NEXT `.../messages/stream` call answers
     * with — build it with `sseBody` (exported alongside this helper isn't
     * needed; tests in this file call it directly). Overrides the
     * fallback-by-default body documented on `mockChatApi` above. */
    setNextStream(value: string) {
      nextStream = value;
    },
    /** Queues the NEXT `.../suggestions` response — defaults to `{
     * suggestions: [] }` (see the field's own comment above) so a test only
     * needs this when it actually wants chips to appear. */
    setNextSuggestions(value: string[]) {
      nextSuggestions = { suggestions: value };
    },
  };
}

/** Mocks `GET /jobs/{id}` only — same "pending N times, then terminal" shape
 * as `cockpit.spec.ts`'s own `mockCurriculaApi` job piece, trimmed to just
 * the poll target since curriculum generation is enqueued through
 * `/chat/.../resolve` here, not `/curricula/generate`. Anchored to
 * `${API_ORIGIN}/jobs/**` the same way `cockpit.spec.ts` does — safe as a
 * plain glob (unlike `/chat`) since this app has no same-origin `/jobs` page
 * for it to collide with. */
async function mockJobsApi(page: Page, { pendingPolls = 1, outcome = "succeeded" as "succeeded" | "failed" } = {}) {
  let polls = 0;
  const calls = { job: 0 };
  const rootId = randomUUID();

  await page.route(`${API_ORIGIN}/jobs/**`, async (route) => {
    const { pathname } = new URL(route.request().url());
    const match = pathname.match(/^\/jobs\/([^/]+)$/);
    if (!match) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
      return;
    }
    calls.job++;
    polls++;
    const base = {
      id: match[1],
      kind: "curriculum",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    if (polls <= pendingPolls) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({ ...base, status: "pending", result_root_id: null, error: null, error_kind: null }),
      });
      return;
    }
    if (outcome === "failed") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          ...base,
          status: "failed",
          result_root_id: null,
          error: "Curriculum generation failed.",
          error_kind: "upstream",
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify({
        ...base,
        status: "succeeded",
        result_root_id: rootId,
        error: null,
        error_kind: null,
      }),
    });
  });

  return { calls, rootId };
}

test.describe("chat cockpit (mocked API)", () => {
  test("approves a proposed mutation and shows the resulting narration", async ({ page }) => {
    const mock = await mockChatApi(page);

    // Reach the page via the nav (not `page.goto`) so this also proves the
    // "add chat to nav" requirement, not just the page's own route. Starts
    // from /en/library (Today is gone from the desktop build).
    await page.goto("/en/library");
    await page.getByTestId("nav-chat").click();
    // `/en/chat` is a redirector now (Stage 5.6): with nothing to resume it
    // creates a session and replaces the URL with the conversation's own.
    await expect(page).toHaveURL(/\/en\/chat\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId("chat-heading")).toBeVisible();
    await expect(page.getByTestId("chat-empty")).toBeVisible();

    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(mock.calls.create).toBe(1);

    const approvalId = randomUUID();
    mock.setNextMessage({
      status: "awaiting_approval",
      approval_id: approvalId,
      tool_name: "create_student",
      tool_args: { name: "Maria Ioannou", level: "beginner" },
      description: "I'll add Maria Ioannou as a new student.",
    });

    await page.getByTestId("chat-input").fill("Add a student named Maria Ioannou, beginner level");
    await page.getByTestId("chat-send").click();

    // The user's own turn is in the transcript, and the proposed mutation
    // renders as an ApprovalCard (tool name + args readable), not a plain
    // assistant bubble.
    await expect(page.getByTestId("chat-message").first()).toContainText("Add a student named Maria Ioannou");
    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(page.getByTestId("approval-tool-name")).toHaveText("create_student");
    await expect(page.getByTestId("approval-description")).toContainText("Maria Ioannou");
    await expect(page.getByTestId("approval-args")).toContainText("Maria Ioannou");
    await expect(page.getByTestId("approval-args")).toContainText("beginner");

    // The composer is disabled for the entire time the approval is open —
    // the client-side mirror of the API's own 409 guard.
    await expect(page.getByTestId("chat-input")).toBeDisabled();
    await expect(page.getByTestId("chat-send")).toBeDisabled();

    mock.setNextResolve({ status: "answer", content: "Done — Maria Ioannou has been added to your roster." });
    await page.getByTestId("approval-approve").click();

    await expect(page.getByTestId("approval-card")).toHaveCount(0);
    await expect(page.getByTestId("chat-message").last()).toContainText("Maria Ioannou has been added");
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    // The composer is usable again. Send stays disabled on an empty draft by
    // design (composerDisabled || !draft.trim()), so type first to confirm it
    // re-enables — proving the pending-approval gate has cleared.
    await page.getByTestId("chat-input").fill("thanks!");
    await expect(page.getByTestId("chat-send")).toBeEnabled();

    expect(mock.calls.message).toBe(1);
    expect(mock.calls.resolve).toBe(1);
    expect(mock.lastBody.message).toEqual({ content: "Add a student named Maria Ioannou, beginner level" });
    expect(mock.lastBody.resolve).toEqual({ decision: "approve", edited_args: null });
    expect(mock.unexpected).toEqual([]);
  });

  test("rejects a proposed mutation and shows the rejection narration", async ({ page }) => {
    const mock = await mockChatApi(page);
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const approvalId = randomUUID();
    mock.setNextMessage({
      status: "awaiting_approval",
      approval_id: approvalId,
      tool_name: "update_student",
      tool_args: { student_id: randomUUID(), level: "advanced" },
      description: "I'll mark this student as advanced.",
    });
    await page.getByTestId("chat-input").fill("Mark Nikos as advanced level");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(page.getByTestId("chat-input")).toBeDisabled();

    mock.setNextResolve({ status: "answer", content: "Understood — I won't make that change." });
    await page.getByTestId("approval-reject").click();

    await expect(page.getByTestId("approval-card")).toHaveCount(0);
    await expect(page.getByTestId("chat-message").last()).toContainText("won't make that change");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    expect(mock.calls.resolve).toBe(1);
    expect(mock.lastBody.resolve).toEqual({ decision: "reject", edited_args: null });
    expect(mock.unexpected).toEqual([]);
  });

  test("an approved async generate_curriculum job polls to completion and links to that curriculum's own page", async ({
    page,
  }) => {
    const mock = await mockChatApi(page);
    // Answers "pending" once, then "succeeded" — proving the panel actually
    // polls `GET /jobs/{id}` rather than trusting the resolve response alone
    // (same non-frozen-loading-state requirement `cockpit.spec.ts`'s own
    // curriculum-generation test holds `generate-dialog.tsx` to).
    const jobs = await mockJobsApi(page, { pendingPolls: 1 });

    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const approvalId = randomUUID();
    mock.setNextMessage({
      status: "awaiting_approval",
      approval_id: approvalId,
      tool_name: "generate_curriculum",
      tool_args: { title: "Tone Shaping Fundamentals", language: "en" },
      description: 'I\'ll generate a curriculum called "Tone Shaping Fundamentals".',
    });
    await page.getByTestId("chat-input").fill("Generate a curriculum on tone shaping");
    await page.getByTestId("chat-send").click();
    await expect(page.getByTestId("approval-card")).toBeVisible();

    const jobId = randomUUID();
    mock.setNextResolve({ status: "job_pending", job_id: jobId });
    await page.getByTestId("approval-approve").click();

    // The approval card is gone and a "generating" status row shows in its
    // place — the composer stays disabled through the whole poll, same as
    // while the approval itself was open.
    await expect(page.getByTestId("approval-card")).toHaveCount(0);
    await expect(page.getByTestId("chat-job-pending")).toBeVisible();
    await expect(page.getByTestId("chat-input")).toBeDisabled();

    await expect(page.getByTestId("chat-job-pending")).toBeHidden({ timeout: 10_000 });
    await expect(page.getByTestId("chat-message-link")).toBeVisible();
    // Unit A: deep-links straight to the materialized curriculum's own detail
    // route, not the plain index — `result_root_id` is known here.
    await expect(page.getByTestId("chat-message-link")).toHaveAttribute("href", `/en/curricula/${jobs.rootId}`);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    expect(jobs.calls.job).toBe(2);
    expect(mock.unexpected).toEqual([]);
  });

  // Plan 11 Task 3 (C4): the answer arrives as real markdown (not a raw
  // asterisk-riddled string in a bubble) and a grounded answer's citations
  // render as chips that deep-link into the Reader at the cited page — the
  // whole payoff of Plans 9-11's forced-retrieval work actually reaching the
  // chat UI. Goes through the default stream-falls-back-to-REST mock (see
  // `mockChatApi`'s own docstring) — this test's concern is RENDERING a
  // `ChatTurnOut` with markdown content + citations, not which transport
  // produced it; the dedicated streaming test below covers the SSE path.
  test("renders assistant markdown and a citation chip linking to the real scanned page", async ({ page }) => {
    const mock = await mockChatApi(page);
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const sourceId = randomUUID();
    mock.setNextMessage({
      status: "answer",
      content:
        "## Pick thickness\n\nA **thicker** pick gives you:\n\n" +
        "- more attack\n- less pick noise\n\n```\nexample\n```",
      citations: [
        {
          source_id: sourceId,
          source_title: "Getting Great Guitar Sounds",
          page_no: 21,
          page_id: randomUUID(),
          snippet: "A thicker pick gives a rounder tone…",
        },
      ],
    });

    await page.getByTestId("chat-input").fill("What does pick thickness do to my tone?");
    await page.getByTestId("chat-send").click();

    const assistantBubble = page.getByTestId("chat-message").last();
    await expect(assistantBubble).toBeVisible();
    // Real markdown elements, not a literal "**thicker**"/"##" wall of text.
    await expect(assistantBubble.locator("h2")).toHaveText("Pick thickness");
    await expect(assistantBubble.locator("strong")).toHaveText("thicker");
    await expect(assistantBubble.locator("li")).toHaveCount(2);
    await expect(assistantBubble.locator("code")).toHaveText("example\n");

    // The citation chip: readable label, and — the actual payoff — a real
    // href into the Reader at the cited page.
    const chip = assistantBubble.getByTestId("citation-chip");
    await expect(chip).toBeVisible();
    await expect(chip).toContainText("Getting Great Guitar Sounds");
    await expect(chip).toContainText("21");
    await expect(chip).toHaveAttribute("href", `/en/library/${sourceId}?page=21`);

    expect(mock.unexpected).toEqual([]);
  });

  // Plan 11 Task 3 (C4): SSE token streaming for the plain-answer path — the
  // fix for "the 15s dead pause is what makes it feel broken even when it
  // works". Scripts a REAL multi-chunk SSE body (several `delta` events then
  // `done`) so this proves the client renders text PROGRESSIVELY (not just a
  // single swap once the whole response function returns), and that a
  // successful stream never falls back to the REST endpoint.
  test("streams tokens progressively over SSE and never falls back to REST", async ({ page }) => {
    const mock = await mockChatApi(page);
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const sourceId = randomUUID();
    mock.setNextStream(
      sseBody([
        { event: "delta", data: { text: "Thicker " } },
        { event: "delta", data: { text: "picks **grip** better " } },
        { event: "delta", data: { text: "and cut through the mix." } },
        {
          event: "done",
          data: {
            citations: [
              {
                source_id: sourceId,
                source_title: "Getting Great Guitar Sounds",
                page_no: 21,
                page_id: randomUUID(),
                snippet: "A thicker pick…",
              },
            ],
          },
        },
      ]),
    );

    // Install a MutationObserver on the transcript BEFORE sending, so it
    // captures every intermediate paint of the streaming bubble's text —
    // the only reliable way to prove "appended progressively" rather than
    // "swapped once at the end" regardless of how the mocked response's
    // bytes happen to be chunked over the wire.
    // Observes `document.body` (not `[data-testid="message-list"]` — that
    // container doesn't exist yet pre-send; `MessageList` renders the
    // `chat-empty` placeholder instead until the first message lands) so the
    // observer is live from before the very first mutation that matters.
    await page.evaluate(() => {
      const w = window as unknown as { __chatSnapshots: string[] };
      w.__chatSnapshots = [];
      const observer = new MutationObserver(() => {
        const bubbles = document.querySelectorAll('[data-testid="chat-message"][data-role="assistant"]');
        const last = bubbles[bubbles.length - 1];
        if (last) w.__chatSnapshots.push(last.textContent ?? "");
      });
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    });

    await page.getByTestId("chat-input").fill("What does pick thickness do to my tone?");
    await page.getByTestId("chat-send").click();

    const assistantBubble = page.getByTestId("chat-message").last();
    await expect(assistantBubble).toContainText("cut through the mix");
    await expect(assistantBubble.getByTestId("citation-chip")).toHaveAttribute(
      "href",
      `/en/library/${sourceId}?page=21`,
    );

    const snapshots = await page.evaluate(() => (window as unknown as { __chatSnapshots: string[] }).__chatSnapshots);
    const distinctNonEmpty = [...new Set(snapshots.filter((s) => s.length > 0))];
    // Several distinct, growing states were rendered — not one final swap.
    expect(distinctNonEmpty.length).toBeGreaterThan(1);
    expect(distinctNonEmpty[0].length).toBeLessThan(distinctNonEmpty[distinctNonEmpty.length - 1].length);

    // A successful stream must never fall back to the REST turn endpoint.
    expect(mock.calls.stream).toBe(1);
    expect(mock.calls.message).toBe(0);
    expect(mock.unexpected).toEqual([]);
  });

  // Plan 11 Task 3 (C4) REGRESSION: streaming must never weaken the HITL
  // gate. When the stream endpoint reports a `fallback` (the honest
  // simplification — a tool/mutation turn always defers to the existing,
  // airtight REST flow), the approval card must still appear and still gate
  // the mutation exactly as it did before streaming existed.
  test("a mutation turn still falls back to REST and the approval card still gates it", async ({ page }) => {
    const mock = await mockChatApi(page);
    // Default stream mock is already `event: fallback` (see `mockChatApi`'s
    // docstring) — asserted explicitly here so this test fails loudly if
    // that default ever changes out from under it.
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    const approvalId = randomUUID();
    mock.setNextMessage({
      status: "awaiting_approval",
      approval_id: approvalId,
      tool_name: "create_student",
      tool_args: { name: "Maria Ioannou", level: "beginner" },
      description: "I'll add Maria Ioannou as a new student.",
    });

    await page.getByTestId("chat-input").fill("Add a student named Maria Ioannou, beginner level");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(page.getByTestId("approval-tool-name")).toHaveText("create_student");
    await expect(page.getByTestId("chat-input")).toBeDisabled();

    expect(mock.calls.stream).toBe(1);
    expect(mock.calls.message).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });
});

// Chat overhaul, Piece B — suggestion chips: a SEPARATE, non-blocking
// `POST /chat/{id}/suggestions` call fired after an assistant answer already
// rendered. These prove the wiring end to end: the chips appear once that
// call resolves, clicking one sends it through the SAME `.../messages` path
// a typed message takes, and no chip ever appears while an approval gates
// the composer (the composer is always primary; chips are an optional
// shortcut, never a second way past its own gate).
test.describe("suggestion chips (mocked API)", () => {
  test("renders chips after an assistant answer, and clicking one sends it as the next turn", async ({ page }) => {
    const mock = await mockChatApi(page);
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    mock.setNextMessage({ status: "answer", content: "A humbucker cancels hum." });
    mock.setNextSuggestions(["Explain single-coil hum", "Compare humbucker brands"]);

    await page.getByTestId("chat-input").fill("What cancels hum?");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("chat-message").last()).toContainText("A humbucker cancels hum.");
    await expect(page.getByTestId("suggestion-chips")).toBeVisible();
    const chips = page.getByTestId("suggestion-chip");
    await expect(chips).toHaveCount(2);
    await expect(chips.first()).toHaveText("Explain single-coil hum");

    // Clicking a chip sends ITS TEXT as the next user turn, through the same
    // `.../messages` REST call a typed message takes (the stream endpoint
    // still attempts first and falls back — `mockChatApi`'s default).
    mock.setNextMessage({ status: "answer", content: "Single-coils pick up hum from AC fields." });
    mock.setNextSuggestions([]);
    await chips.first().click();

    await expect(page.getByTestId("chat-message")).toHaveCount(4);
    await expect(page.getByTestId("chat-message").nth(2)).toContainText("Explain single-coil hum");
    await expect(page.getByTestId("chat-message").last()).toContainText("Single-coils pick up hum");
    // The chips from the FIRST answer are gone the instant the click sent a
    // new turn (cleared, not just replaced once the new fetch resolves), and
    // the second scripted response is an empty list, so none reappear.
    await expect(page.getByTestId("suggestion-chips")).toHaveCount(0);

    expect(mock.calls.message).toBe(2);
    expect(mock.calls.suggestions).toBe(2);
    expect(mock.lastBody.message).toEqual({ content: "Explain single-coil hum" });
    expect(mock.unexpected).toEqual([]);
  });

  test("never shows chips while an approval is pending", async ({ page }) => {
    const mock = await mockChatApi(page);
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    mock.setNextMessage({
      status: "awaiting_approval",
      approval_id: randomUUID(),
      tool_name: "create_student",
      tool_args: { name: "Maria Ioannou" },
      description: "I'll add that student.",
    });
    // Scripted so the test would fail loudly if the panel ever called this
    // endpoint on an awaiting_approval turn — it must not.
    mock.setNextSuggestions(["should never render"]);

    await page.getByTestId("chat-input").fill("add a student named Maria Ioannou");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(page.getByTestId("suggestion-chips")).toHaveCount(0);
    expect(mock.calls.suggestions).toBe(0);
    expect(mock.unexpected).toEqual([]);
  });
});

// Plan 13 Stage 5.6 — chat history. Chris: "the chat needs a history bro."
// The API always persisted every turn; the UI created a session on mount, kept
// the id in React state, and orphaned the whole conversation on refresh. These
// four tests are the proof the fix works where it counts: ACROSS A RELOAD.
test.describe("chat history (mocked API)", () => {
  /** Sends one message through the REST path and returns the conversation's
   * URL — the shared setup for every reload proof below. */
  async function sendFirstMessage(page: Page, mock: Awaited<ReturnType<typeof mockChatApi>>, text: string) {
    await page.goto("/en/chat");
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    await page.getByTestId("chat-input").fill(text);
    await page.getByTestId("chat-send").click();
    return page.url();
  }

  test("the transcript survives a reload", async ({ page }) => {
    const mock = await mockChatApi(page);
    mock.setNextMessage({ status: "answer", content: "A humbucker cancels hum." });

    const url = await sendFirstMessage(page, mock, "What cancels hum?");
    await expect(page.getByTestId("chat-message")).toHaveCount(2);

    // The session is now in the sidebar, named after the first user message —
    // server-derived, so this also proves the panel refreshes the list.
    await expect(page.getByTestId("chat-session-title")).toHaveText("What cancels hum?");

    await page.reload();

    await expect(page).toHaveURL(url);
    await expect(page.getByTestId("chat-message")).toHaveCount(2);
    await expect(page.getByTestId("chat-message").first()).toContainText("What cancels hum?");
    await expect(page.getByTestId("chat-message").last()).toContainText("A humbucker cancels hum.");
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(mock.unexpected).toEqual([]);
  });

  // THE one that matters. A session reloaded mid-approval must come back with
  // the SAME HITL card and a STILL-DISABLED composer — the card's description
  // is not a field on the API's `MessageOut`, it is the trailing assistant
  // row's own content, and the composer being live for even one render would
  // let through a message the API answers with a 409.
  test("a pending approval survives a reload, keeps the composer disabled, and still completes", async ({ page }) => {
    const mock = await mockChatApi(page);
    const approvalId = randomUUID();
    mock.setNextMessage({
      status: "awaiting_approval",
      approval_id: approvalId,
      tool_name: "create_student",
      tool_args: { name: "Maria Ioannou", level: "beginner" },
      description: "I'll add Maria Ioannou as a new student.",
    });

    await sendFirstMessage(page, mock, "Add a student named Maria Ioannou, beginner level");
    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(page.getByTestId("chat-input")).toBeDisabled();

    await page.reload();

    // Same card, same description, same args — rebuilt from `GET .../pending`
    // plus the trailing assistant row of `GET /chat/{id}`.
    await expect(page.getByTestId("approval-card")).toBeVisible();
    await expect(page.getByTestId("approval-description")).toHaveText("I'll add Maria Ioannou as a new student.");
    await expect(page.getByTestId("approval-tool-name")).toHaveText("create_student");
    await expect(page.getByTestId("approval-args")).toContainText("beginner");
    await expect(page.getByTestId("chat-input")).toBeDisabled();
    await expect(page.getByTestId("chat-send")).toBeDisabled();

    // The narration moved INTO the card — it must not also render as a stray
    // assistant bubble above it. Only the user's own turn is in the transcript.
    await expect(page.getByTestId("chat-message")).toHaveCount(1);

    // ...and the resumed turn still completes.
    mock.setNextResolve({ status: "answer", content: "Done — Maria Ioannou has been added to your roster." });
    await page.getByTestId("approval-approve").click();

    await expect(page.getByTestId("approval-card")).toHaveCount(0);
    await expect(page.getByTestId("chat-message").last()).toContainText("has been added");
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(mock.calls.resolve).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });

  test("renaming a conversation sticks across a reload", async ({ page }) => {
    const mock = await mockChatApi(page);
    mock.setNextMessage({ status: "answer", content: "Sure." });
    await sendFirstMessage(page, mock, "Plan for Nikos");
    await expect(page.getByTestId("chat-session-title")).toHaveText("Plan for Nikos");

    await page.getByTestId("chat-rename").click();
    await page.getByTestId("chat-rename-input").fill("Nikos — 12 week plan");
    await page.getByTestId("chat-rename-save").click();

    await expect(page.getByTestId("chat-session-title")).toHaveText("Nikos — 12 week plan");
    expect(mock.lastBody.rename).toEqual({ title: "Nikos — 12 week plan" });

    await page.reload();
    await expect(page.getByTestId("chat-session-title")).toHaveText("Nikos — 12 week plan");
    expect(mock.unexpected).toEqual([]);
  });

  test("deleting the open conversation confirms first, cascades, and redirects", async ({ page }) => {
    const mock = await mockChatApi(page);
    mock.setNextMessage({ status: "answer", content: "Sure." });
    const url = await sendFirstMessage(page, mock, "Delete me");
    await expect(page.getByTestId("chat-session-item")).toHaveCount(1);

    await page.getByTestId("chat-delete").click();

    // Destructive, and behind the confirm dialog — cancelling deletes nothing.
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await expect(page.getByTestId("confirm-body")).toContainText("Delete me");
    await page.getByTestId("confirm-cancel").click();
    expect(mock.calls.remove).toBe(0);
    await expect(page.getByTestId("chat-session-item")).toHaveCount(1);

    await page.getByTestId("chat-delete").click();
    await page.getByTestId("confirm-accept").click();

    expect(mock.summaries()).toEqual([]);
    // The open conversation was the one deleted, so the panel bounces to the
    // index, which — with nothing left to resume — starts a fresh session.
    await expect(page).toHaveURL(/\/en\/chat\/[0-9a-f-]{36}$/);
    await expect(page).not.toHaveURL(url);
    await expect(page.getByTestId("chat-sessions-empty")).toBeVisible();
    await expect(page.getByTestId("chat-empty")).toBeVisible();
    expect(mock.calls.remove).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });
});
