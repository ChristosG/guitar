import { randomUUID } from "node:crypto";
import { test, expect, type Page, type Route } from "@playwright/test";

// Task 0.6 — the revise drawer's turn is a JOB now, and this spec is the
// proof that the indirection actually completes: the composer POSTs
// `?async=1`, gets a 202 back, polls `GET /jobs/{id}` while the turn runs off
// the request path, and only then re-hydrates history — which is where the
// answer comes from, because the job persisted it server-side.
//
// Why the drawer and not the cockpit chat: `propose_curriculum_revision` runs
// the planner over the whole library inside the turn, measured at 6-8 minutes
// under `claude -p`. Inside an HTTP request that answer never arrives — the
// Cloudflare edge cuts at ~100s while the server keeps working (verified live
// 2026-07-21, and again 2026-09-11). Nothing in front of a job can cut it.
//
// Same route-interception convention as `curricula-revise.spec.ts` next door
// (and every other cockpit spec): anchored to `API_ORIGIN` rather than a bare
// suffix glob, explicit CORS headers plus OPTIONS handling because these are
// cross-origin credentialed calls even under interception, and an "unexpected
// request -> 500" catch-all so a routing mistake fails loudly instead of
// quietly hanging.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

/** The one curriculum this spec revises — a course -> one module -> one
 * drafted lesson. Deliberately thinner than `curricula-revise.spec.ts`'s
 * fixture: nothing here asserts on the board, only on the transport. */
function tree(rootId: string, moduleId: string, lessonId: string) {
  return {
    id: rootId, kind: "course", title: "Tone Shaping Fundamentals", body: null,
    est_minutes: null, order: 0, language: "en", plane: "content", student_id: null,
    meta: { brief: "A usable live tone.", source_ids: null },
    children: [{
      id: moduleId, kind: "module", title: "Signal Chain Basics",
      body: "Understand the guitar signal path.", est_minutes: null, order: 0,
      language: "en", plane: "content", student_id: null,
      meta: { objective: "Understand the guitar signal path.", tier: "library" },
      children: [{
        id: lessonId, kind: "lesson", title: "Pickups and Tone",
        body: "Pickup types and tone shaping.", est_minutes: 30, order: 0,
        language: "en", plane: "content", student_id: null,
        meta: { draft_status: "ready", objective: "Understand pickup types." },
        children: [],
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
      route.fulfill({ status: 200, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

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

type TurnJob = {
  /** `null` until the composer's `?async=1` POST mints one. */
  id: string | null;
  /** Polls answered as `running` before the terminal one — the whole point:
   * the panel must keep polling, not read the first response as the answer. */
  runningPolls: number;
};

/** What the job reports when it goes terminal. `turn` is the `ChatTurnOut`
 * that rides home on `progress.turn`; `answer` is the assistant row the
 * server persisted for it (or `null` — a turn can die having written
 * nothing); `fails`, when set, makes the job `failed` with that
 * `error_kind` instead, which is what the panel must translate. */
type Scripted = {
  turn: Record<string, unknown> | null;
  answer: string | null;
  fails?: { error: string; kind: string };
};

/** The Chat + Jobs half. Same shape as `curricula-revise.spec.ts`'s
 * `mockChatApi`, cut to this spec's subject: the async messages door, the
 * `chat_turn` job it hands back, and the history that door writes to — which
 * is where the answer reaches the screen from, since the async path trusts
 * the server's transcript over its own optimistic state. */
async function mockChatApi(page: Page, sessionId: string) {
  const calls = { message: 0, poll: 0, history: 0, pending: 0, stream: 0, suggestions: 0 };
  const unexpected: string[] = [];
  const asyncFlags: (string | null)[] = [];
  const rows: Array<{ id: string; role: string; content: string | null; created_at: string; citations: null }> = [];

  const job: TurnJob = { id: null, runningPolls: 1 };
  // The SECOND job a turn can hand back: `progress.turn` came home as
  // `job_pending`, so the panel must go on polling THIS one — with the
  // spinner and the disabled composer intact across the hand-off.
  const chained = { id: randomUUID(), polls: 0, runningPolls: 1 };
  /** The turn body the job reports on success, and the assistant row the
   * server would have persisted for it — set per test. `answer: null` means
   * the job dies instead (nothing gets persisted). */
  let scripted: Scripted = { turn: { status: "answer", content: "OK." }, answer: "OK." };
  /** When set, the `?async=1` POST is refused exactly the way the API refuses
   * a second turn while the first one's job is still running. */
  let refuseWith409 = false;

  function record(role: "user" | "assistant", content: string | null) {
    rows.push({ id: randomUUID(), role, content, created_at: new Date().toISOString(), citations: null });
  }

  async function handler(route: Route) {
    const req = route.request();
    const method = req.method();
    const url = new URL(req.url());
    const { pathname } = url;

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: "application/json", headers: CORS_HEADERS, body: JSON.stringify(body) });

    if (pathname === "/chat" && method === "GET") return json([]);

    // A drawer turn must never reach the SSE door — it is tool-heavy by
    // definition and the server declines every one of them. Recorded (not
    // fulfilled with a fallback) so the test can assert it stayed untouched.
    if (pathname === `/chat/${sessionId}/messages/stream` && method === "POST") {
      calls.stream++;
      return json({ detail: "the drawer must not stream" }, 500);
    }

    if (pathname === `/chat/${sessionId}/messages` && method === "POST") {
      calls.message++;
      asyncFlags.push(url.searchParams.get("async"));
      if (refuseWith409) {
        return json({ detail: { code: "turn_running", message: "the previous message is still being answered" } }, 409);
      }
      const body = req.postDataJSON() as { content: string };
      record("user", body.content);
      job.id = randomUUID();
      return json({ job_id: job.id, status: "pending" }, 202);
    }

    if (pathname === `/chat/${sessionId}/suggestions` && method === "POST") {
      calls.suggestions++;
      return json({ suggestions: [] });
    }

    if (pathname === `/chat/${sessionId}/pending` && method === "GET") {
      calls.pending++;
      return json(null);
    }

    if (pathname === `/chat/${sessionId}` && method === "GET") {
      calls.history++;
      return json(rows);
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  }

  await page.route(`${API_ORIGIN}/chat`, handler);
  await page.route(`${API_ORIGIN}/chat/**`, handler);

  await page.route(`${API_ORIGIN}/jobs/**`, async (route: Route) => {
    const { pathname } = new URL(route.request().url());
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const now0 = new Date().toISOString();
    if (pathname === `/jobs/${chained.id}`) {
      chained.polls++;
      const chainedBase = {
        id: chained.id, kind: "curriculum_revise", result_root_id: null,
        error: null, error_kind: null, created_at: now0, updated_at: now0,
      };
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(
          chained.polls <= chained.runningPolls
            ? { ...chainedBase, status: "pending", progress: null }
            : { ...chainedBase, status: "succeeded", progress: null },
        ),
      });
      return;
    }
    if (pathname !== `/jobs/${job.id}`) {
      unexpected.push(`GET ${pathname}`);
      await route.fulfill({
        status: 500, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ detail: "unmocked request in test" }),
      });
      return;
    }
    calls.poll++;
    const now = new Date().toISOString();
    const base = { id: job.id, kind: "chat_turn", result_root_id: null, created_at: now, updated_at: now };
    if (calls.poll <= job.runningPolls) {
      await route.fulfill({
        status: 200, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify({ ...base, status: "running", error: null, error_kind: null, progress: { phase: "answering" } }),
      });
      return;
    }
    // Terminal. The server persisted the assistant row BEFORE the job went
    // terminal, so the transcript is already complete by the time the panel
    // re-hydrates — mirror that ordering here.
    if (scripted.answer !== null) record("assistant", scripted.answer);
    await route.fulfill({
      status: 200, contentType: "application/json", headers: CORS_HEADERS,
      body: JSON.stringify(
        scripted.fails
          ? { ...base, status: "failed", error: scripted.fails.error, error_kind: scripted.fails.kind, progress: null }
          : { ...base, status: "succeeded", error: null, error_kind: null, progress: { phase: "done", turn: scripted.turn } },
      ),
    });
  });

  return {
    calls, unexpected, asyncFlags,
    chainedJobId: chained.id,
    chainedPolls: () => chained.polls,
    setRunningPolls(n: number) { job.runningPolls = n; },
    setNextTurn(value: Scripted) { scripted = value; },
    refuseNextSend() { refuseWith409 = true; },
  };
}

test.describe("revise drawer — the turn runs as a job (mocked API)", () => {
  test("a drawer turn goes async, polls the job, then shows the persisted answer", async ({ page }) => {
    const rootId = randomUUID();
    const sessionId = randomUUID();
    const curricula = await mockCurriculaApi(page, rootId, randomUUID(), randomUUID(), sessionId);
    const chat = await mockChatApi(page, sessionId);
    chat.setRunningPolls(1);
    chat.setNextTurn({
      turn: { status: "answer", content: "A DS-1 sits well after the Tube Screamer." },
      answer: "A DS-1 sits well after the Tube Screamer.",
    });

    await page.goto(`/en/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    await page.getByTestId("chat-input").fill("Where should a DS-1 go?");
    await page.getByTestId("chat-send").click();

    // While the job runs the drawer keeps its own honest status row — and
    // only that one (the generic job spinner is suppressed underneath it).
    await expect(page.getByTestId("chat-planning-revision")).toBeVisible();
    await expect(page.getByTestId("chat-job-pending")).toHaveCount(0);

    await expect(
      page.getByTestId("chat-message").filter({ hasText: "A DS-1 sits well after the Tube Screamer." }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("chat-planning-revision")).toHaveCount(0);
    await expect(page.getByTestId("chat-error")).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    // The door it went through: `?async=1`, exactly once, and never the
    // streaming endpoint.
    expect(chat.calls.message).toBe(1);
    expect(chat.asyncFlags).toEqual(["1"]);
    expect(chat.calls.stream).toBe(0);
    // It genuinely POLLED — a `running` answer was not mistaken for the turn.
    expect(chat.calls.poll).toBeGreaterThanOrEqual(2);

    expect(curricula.unexpected).toEqual([]);
    expect(chat.unexpected).toEqual([]);
  });

  test("a failed turn job says why, in Greek, and still re-hydrates the transcript", async ({ page }) => {
    const rootId = randomUUID();
    const sessionId = randomUUID();
    await mockCurriculaApi(page, rootId, randomUUID(), randomUUID(), sessionId);
    const chat = await mockChatApi(page, sessionId);
    // The job dies, but the server had already persisted a partial turn —
    // which is exactly why the panel hydrates on a failure too.
    chat.setNextTurn({
      turn: null, answer: "Ξεκίνησα να απαντώ…",
      fails: { error: "the model took too long to answer", kind: "timeout" },
    });

    await page.goto(`/el/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    await page.getByTestId("chat-input").fill("Πρόσθεσε ένα μάθημα για το DS-1.");
    await page.getByTestId("chat-send").click();

    const error = page.getByTestId("chat-error");
    await expect(error).toBeVisible({ timeout: 15_000 });
    // The tutor's own language, never the provider's English string —
    // `jobErrorText` is what turns `error_kind` into this sentence.
    await expect(error).toHaveText("Το μοντέλο άργησε πολύ να απαντήσει. Δοκιμάστε ξανά.");

    // Hydrated anyway: the half-written turn is on screen, not thrown away.
    await expect(page.getByTestId("chat-message").filter({ hasText: "Ξεκίνησα να απαντώ…" })).toBeVisible();
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(chat.unexpected).toEqual([]);
  });

  test("a turn that hands back a second job keeps the spinner until THAT job lands", async ({ page }) => {
    const rootId = randomUUID();
    const sessionId = randomUUID();
    await mockCurriculaApi(page, rootId, randomUUID(), randomUUID(), sessionId);
    const chat = await mockChatApi(page, sessionId);
    chat.setNextTurn({
      turn: { status: "job_pending", job_id: chat.chainedJobId },
      answer: "Ξεκινάω την αναθεώρηση.",
    });

    await page.goto(`/el/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    await page.getByTestId("chat-input").fill("Εφάρμοσε την αναθεώρηση.");
    await page.getByTestId("chat-send").click();

    // The hand-off must not drop the flag between the two waits: this is the
    // window in which the composer would otherwise come back to life with a
    // revision still being applied underneath it.
    await expect(page.getByTestId("chat-job-pending")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("chat-input")).toBeDisabled();

    await expect(
      page.getByTestId("chat-message").filter({
        hasText: "Έτοιμο — το πρόγραμμα παρακάτω αντικατοπτρίζει πλέον αυτή την αναθεώρηση.",
      }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId("chat-job-pending")).toHaveCount(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(chat.chainedPolls()).toBeGreaterThanOrEqual(2);
    expect(chat.unexpected).toEqual([]);
  });

  test("a 409 'turn_running' is told plainly, not waited out as a slow turn", async ({ page }) => {
    const rootId = randomUUID();
    const sessionId = randomUUID();
    await mockCurriculaApi(page, rootId, randomUUID(), randomUUID(), sessionId);
    const chat = await mockChatApi(page, sessionId);
    chat.refuseNextSend();

    await page.goto(`/el/curricula/${rootId}`);
    await page.getByTestId("revise-open").click();
    await expect(page.getByTestId("chat-input")).toBeEnabled();

    await page.getByTestId("chat-input").fill("Και άλλο ένα μάθημα.");
    await page.getByTestId("chat-send").click();

    await expect(page.getByTestId("chat-error")).toContainText(
      "Η προηγούμενη απάντηση γράφεται ακόμα",
      { timeout: 15_000 },
    );
    // Refused before anything was persisted, so there is nothing to poll and
    // nothing to wait for — the composer comes straight back.
    expect(chat.calls.poll).toBe(0);
    await expect(page.getByTestId("chat-input")).toBeEnabled();
    expect(chat.unexpected).toEqual([]);
  });
});
