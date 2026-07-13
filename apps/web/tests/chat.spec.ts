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
// `/chat/**`) — same convention `cockpit.spec.ts` settled on for `/students`,
// and for the same reason both `cockpit.spec.ts` and `artifacts.spec.ts`
// document explicitly: a bare suffix glob like `**/chat/**` would ALSO match
// this app's own same-origin `/en/chat` page URL, and (unlike `/students`)
// `**/chat/**` specifically would never even match the bare `POST /chat`
// session-create call in the first place — that URL has no "/chat/"
// substring for the glob's trailing `/**` to anchor on. Anchoring on origin
// instead sidesteps both problems.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

/** Encodes a list of `{event, data}` pairs as an SSE response body — the
 * exact wire shape `app/routers/chat.py`'s `POST .../messages/stream`
 * produces (see that module's docstring): one `event: <name>\ndata:
 * <json>\n\n` block per server-sent event, in order. */
function sseBody(events: Array<{ event: string; data: unknown }>): string {
  return events.map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`).join("");
}

/** Wires up an in-memory mock of the Chat API (`POST /chat`, `POST
 * /chat/{id}/messages`, `POST /chat/{id}/messages/stream`, `POST
 * /chat/{id}/approvals/{id}/resolve`, plus the history/pending GETs for
 * completeness even though this page's current flow never calls them itself
 * — see `lib/api.ts`'s own docstring on `getChatHistory`/
 * `getPendingApproval`). Each write endpoint's NEXT response is queued
 * explicitly by the test right before the action that triggers it
 * (`setNextMessage`/`setNextResolve`/`setNextStream`) — simpler than
 * modeling the real agent loop, and every test here only ever sends/resolves
 * once.
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
async function mockChatApi(page: Page) {
  const calls = { create: 0, message: 0, resolve: 0, history: 0, pending: 0, stream: 0 };
  const lastBody: { message?: unknown; resolve?: unknown; stream?: unknown } = {};
  const unexpected: string[] = [];
  let nextMessage: unknown = { status: "answer", content: "OK." };
  let nextResolve: unknown = { status: "answer", content: "OK." };
  let nextStream: string = sseBody([{ event: "fallback", data: { reason: "tool_call" } }]);

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/chat" && method === "POST") {
      calls.create++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({ session_id: randomUUID() }),
      });
      return;
    }

    const streamMatch = pathname.match(/^\/chat\/([^/]+)\/messages\/stream$/);
    if (streamMatch && method === "POST") {
      calls.stream++;
      lastBody.stream = req.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: CORS_HEADERS,
        body: nextStream,
      });
      return;
    }

    const messagesMatch = pathname.match(/^\/chat\/([^/]+)\/messages$/);
    if (messagesMatch && method === "POST") {
      calls.message++;
      lastBody.message = req.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(nextMessage),
      });
      return;
    }

    const resolveMatch = pathname.match(/^\/chat\/([^/]+)\/approvals\/([^/]+)\/resolve$/);
    if (resolveMatch && method === "POST") {
      calls.resolve++;
      lastBody.resolve = req.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(nextResolve),
      });
      return;
    }

    const pendingMatch = pathname.match(/^\/chat\/([^/]+)\/pending$/);
    if (pendingMatch && method === "GET") {
      calls.pending++;
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: "null" });
      return;
    }

    const historyMatch = pathname.match(/^\/chat\/([^/]+)$/);
    if (historyMatch && method === "GET") {
      calls.history++;
      await route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: "[]" });
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
    setNextMessage(value: unknown) {
      nextMessage = value;
    },
    setNextResolve(value: unknown) {
      nextResolve = value;
    },
    /** Queues the raw SSE body the NEXT `.../messages/stream` call answers
     * with — build it with `sseBody` (exported alongside this helper isn't
     * needed; tests in this file call it directly). Overrides the
     * fallback-by-default body documented on `mockChatApi` above. */
    setNextStream(value: string) {
      nextStream = value;
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
        result_root_id: randomUUID(),
        error: null,
        error_kind: null,
      }),
    });
  });

  return { calls };
}

test.describe("chat cockpit (mocked API)", () => {
  test("approves a proposed mutation and shows the resulting narration", async ({ page }) => {
    const mock = await mockChatApi(page);

    // Reach the page via the nav (not `page.goto`) so this also proves the
    // "add chat to nav" requirement, not just the page's own route.
    await page.goto("/en/today");
    await page.getByTestId("nav-chat").click();
    await expect(page).toHaveURL(/\/en\/chat$/);
    await expect(page.getByTestId("chat-heading")).toBeVisible();
    await expect(page.getByTestId("chat-empty")).toBeVisible();

    // A session is created up front, on mount.
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

  test("an approved async generate_curriculum job polls to completion and links to the curricula page", async ({
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
    await expect(page.getByTestId("chat-message-link")).toHaveAttribute("href", "/en/curricula");
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
