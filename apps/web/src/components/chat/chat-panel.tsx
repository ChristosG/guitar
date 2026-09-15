"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Loader2, Send, Wand2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ApprovalCard } from "@/components/chat/approval-card";
import { RevisionPlanCard } from "@/components/chat/revision-plan-card";
import { MessageList, type ChatDisplayMessage } from "@/components/chat/message-list";
import { SuggestionChips } from "@/components/chat/suggestion-chips";
import { useChatSessions } from "@/components/chat/chat-sessions";
import {
  ApiError,
  getChatHistory,
  getChatSuggestions,
  getCurriculumProgress,
  getJob,
  distillChatInstruction,
  getPendingApproval,
  resolveApproval,
  sendChatMessage,
  sendChatMessageAsync,
  streamChatMessage,
  type ChatCitation,
  type ChatMessageOut,
  type ChatTurnOut,
  type JobOut,
  type RevisionPlan,
} from "@/lib/api";
import { jobErrorText } from "@/lib/job-errors";

/** Poll cadence + cap while a `generate_curriculum` job is in flight after an
 * approval — same convention (and same constants) as `curriculum/
 * generate-dialog.tsx`'s own poll loop: reuses Plan 8's `getJob`, ~2s between
 * polls, capping at ~5 minutes before this component gives up on its OWN
 * wait (the job keeps running server-side regardless of the cap — see that
 * component's docstring for the full reasoning, which applies unchanged
 * here). Duplicated rather than extracted into a shared hook — small
 * deliberate duplication, matching this codebase's own stated precedent
 * (e.g. `routers/chat.py`'s `_stringify` mirroring `loop.py`'s exactly)
 * rather than introducing a new shared module for one 10-line loop used in
 * exactly two places. */
const POLL_INTERVAL_MS = 2000;
/** ~40 minutes. Was 150 (~5) when the only thing polled here was a
 * `generate_curriculum` job, then 300 (~10) once the drawer's own turn became
 * a job too. 1200 because 10 was still short of the real thing: on the tutor's
 * first live afternoon (2026-09-12) a single `claude -p` turn under bridge
 * contention ran past 8 minutes, and a curriculum job behind it ran 19 — the
 * cap has to sit above the bridge's own 1200s ceiling, not under it. The job
 * keeps running server-side past the cap regardless; the cap only ends THIS
 * component's wait. */
const MAX_POLLS = 1200;
/** Consecutive unanswered polls before `waitForJob` gives up. Three, not one:
 * see its own comment — a single blink is not a failed job, and three in a row
 * (~6s of nothing) is a connection that is actually gone. */
const MAX_POLL_MISSES = 3;

interface PendingApprovalState {
  approvalId: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  description: string;
}

interface ChatPanelProps {
  /** The conversation to show, from the URL (`/{locale}/chat/{sessionId}`).
   * The page keys this component on it, so a change here is a fresh mount,
   * never a stale-transcript re-render. */
  sessionId: string;
  /** Set when this panel is scoped to one curriculum — the revise drawer on
   * `curricula/[rootId]` (Unit D, Task D2b) is the only caller that passes
   * it. Not read for its value, only as a SIGNAL, in two places: `pollJob`
   * hands an async mutation's success to `onJobDone` (refresh the board the
   * tutor is already looking at) instead of narrating a "view curriculum"
   * link away from it, and the composer shows a "Planning the revision…"
   * status instead of a bare spinner while a turn is in flight — this
   * drawer's whole reason to exist, `propose_curriculum_revision`, is a
   * synchronous 20-60s call over the whole library (resolved design call
   * #2, `docs/superpowers/plans/2026-07-18-unit-d-revise-chat.md`). */
  rootId?: string;
  /** id -> title, built from the curriculum's own tree by the revise drawer
   * (its only caller). `RevisionPlanCard` uses it to label a `modify_lesson`/
   * `move_lesson`/`remove_lesson` op with the block's real name — those ops
   * carry only a bare id (`curriculum/revise.py`'s flat op schema). */
  blockTitles?: Record<string, string>;
  /** Called INSTEAD of appending the "view curriculum" link once an async
   * mutation job succeeds, when `rootId` is set. */
  onJobDone?: () => void;
  /** Text to drop into the composer when this panel mounts, or when the value
   * changes. The module ⋯ menu's "Restructure with AI" uses it to open the
   * drawer with the module already named — the tutor then finishes the
   * sentence in his own words rather than having to describe which module he
   * means to a model that cannot see what he clicked.
   *
   * SEEDED, NOT SENT. It lands in the composer and waits: the whole point is
   * that he says what he actually wants, and a message that sent itself would
   * spend a 20-60s planner call on a half-written instruction. */
  seedDraft?: string;
}

/** A persisted transcript row becomes a bubble. Rows with no `content` are
 * DROPPED, not rendered empty: a `content: null` assistant row is a
 * tool-calls-only turn (the model proposed a mutation and narrated nothing),
 * and there is nothing to show for it. */
function toDisplayMessage(row: ChatMessageOut): ChatDisplayMessage | null {
  if (row.role !== "user" && row.role !== "assistant") return null;
  if (!row.content) return null;
  return { id: row.id, role: row.role, content: row.content, citations: row.citations };
}

/**
 * The chat cockpit's one stateful surface (Plan 5 Task 5): a transcript
 * (`MessageList`) + composer, plus the HITL approval gate every mutation
 * tool call goes through. Owns the whole turn-by-turn state machine driven
 * by a `ChatTurnOut.status`:
 *  - "answer" -> append the assistant's `content` to the transcript.
 *  - "awaiting_approval" -> render `ApprovalCard` for it and DISABLE the
 *    composer — the client half of the API's own 409 guard (`routers/
 *    chat.py`'s `post_message`): the server refuses a new user turn while an
 *    approval is open, so this UI must never let one through in the first
 *    place rather than reacting to that 409 after the fact.
 *  - "job_pending" (resolve only, `generate_curriculum`) -> show a
 *    "generating…" status row and poll `getJob` (see `POLL_INTERVAL_MS`/
 *    `MAX_POLLS` above) until it's terminal, then append a success message
 *    linking to `/{locale}/curricula/{result_root_id}` (Unit A's detail
 *    route) when the job returned one, falling back to the plain
 *    `/{locale}/curricula` index in the defensive case where it didn't (or
 *    surface the failure/still-generating case as a composer-area error) and
 *    re-enable the composer either way.
 *
 * The session comes in as a PROP from the URL (`/{locale}/chat/{sessionId}`)
 * and this component HYDRATES from it on mount (Plan 13 Stage 5.6) — it used
 * to `createChatSession()` on mount and keep the id in React state only,
 * which orphaned every conversation on refresh. Hydration is both halves of
 * the persisted state, and the second one is the one that matters:
 *
 *  - `getChatHistory` -> the transcript.
 *  - `getPendingApproval` -> an approval left OPEN when the tab was closed.
 *    Its `ApprovalCard` must come back with the SAME description it had, and
 *    the composer must stay disabled (the API 409s a new message while an
 *    approval is open — the disabled composer is the client half of that
 *    guard, and it has to survive a reload, not just a lucky render). The
 *    description is not a field on the API's `MessageOut` — it is the
 *    TRAILING ASSISTANT ROW's own content, the narration the model wrote when
 *    it proposed the mutation (`agent/loop.py` persists it). So on hydration
 *    that row is popped off the transcript and handed to the card instead of
 *    being rendered as a stray bubble above it — exactly where it sits in the
 *    live (never-reloaded) flow.
 */
export function ChatPanel({ sessionId, rootId, blockTitles, onJobDone, seedDraft }: ChatPanelProps) {
  const t = useTranslations("chat");
  const tJobErrors = useTranslations("jobErrors");
  const locale = useLocale();
  const { refresh: refreshSessions } = useChatSessions();

  const [hydrating, setHydrating] = useState(true);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const [messages, setMessages] = useState<ChatDisplayMessage[]>([]);
  const [draft, setDraft] = useState(seedDraft ?? "");
  const [sending, setSending] = useState(false);
  const [composerError, setComposerError] = useState<string | null>(null);

  // A NEW seed replaces the composer; the SAME seed never re-fires. Keyed on
  // the seed's own value rather than on mount, because the drawer stays mounted
  // between openings: without this, clicking "Restructure with AI" on a second
  // module would leave the first module's sentence sitting there. Guarded on
  // truthiness so an unscoped open (no seed) never wipes something he typed.
  useEffect(() => {
    if (seedDraft) setDraft(seedDraft);
  }, [seedDraft]);

  const [pendingApproval, setPendingApproval] = useState<PendingApprovalState | null>(null);
  const [resolving, setResolving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  const [jobPending, setJobPending] = useState(false);
  const [distilling, setDistilling] = useState(false);

  // "Next move" suggestion chips (chat overhaul, Piece B). Fetched
  // NON-BLOCKING, after a genuine assistant answer has already rendered —
  // never awaited before showing that answer. `suggestionsRequestId` guards
  // against the one race this invites: the tutor sends a new turn before an
  // in-flight suggestions fetch from the PREVIOUS turn resolves, which would
  // otherwise overwrite freshly-cleared chips with stale ones a moment later.
  // Every clear bumps the id; a resolving fetch only applies its result if
  // the id it captured is still current.
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const suggestionsRequestId = useRef(0);

  function clearSuggestions() {
    suggestionsRequestId.current += 1;
    setSuggestions([]);
  }

  const fetchSuggestions = useCallback(() => {
    const requestId = suggestionsRequestId.current;
    getChatSuggestions(sessionId)
      .then((result) => {
        if (suggestionsRequestId.current === requestId) setSuggestions(result.suggestions);
      })
      .catch(() => {
        if (suggestionsRequestId.current === requestId) setSuggestions([]);
      });
  }, [sessionId]);

  // Same .then/.catch/.finally shape as e.g. knowledge/page.tsx's
  // fetchSources, for the same reason: every setState call stays lexically
  // inside a callback rather than a bare statement in the function body
  // (what react-hooks/set-state-in-effect actually checks for).
  const hydrate = useCallback(() => {
    return Promise.all([getChatHistory(sessionId), getPendingApproval(sessionId)])
      .then(([rows, pending]) => {
        const display = rows
          .map(toDisplayMessage)
          .filter((m): m is ChatDisplayMessage => m !== null);

        if (pending) {
          // The trailing assistant row IS the card's description (see this
          // component's docstring), so it moves OUT of the transcript and
          // into the card. Fallback for the case the API itself documents:
          // the model can propose a mutation having narrated nothing, and
          // that row is then persisted with no content at all.
          let description = t("approval.proposedAction");
          if (display.at(-1)?.role === "assistant") {
            description = display.pop()!.content;
          }
          setPendingApproval({
            approvalId: pending.id,
            toolName: pending.tool_name,
            toolArgs: pending.tool_args ?? {},
            description,
          });
        }
        setMessages(display);
      })
      .catch((err) => setSessionError(err instanceof ApiError ? err.detail : t("sessionError")))
      .finally(() => setHydrating(false));
  }, [sessionId, t]);

  // Guard against React Strict Mode's dev double-invoke of mount effects.
  // Harmless for two GETs, but it would double-run the `display.pop()` above
  // against two independent responses — the ref keeps hydration to one pass.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    void hydrate();
  }, [hydrate]);

  function appendMessage(
    role: "user" | "assistant",
    content: string,
    link?: ChatDisplayMessage["link"],
    citations?: ChatCitation[] | null,
  ) {
    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role, content, link, citations }]);
  }

  // Shared reaction to a `ChatTurnOut`, whatever produced it (the initial
  // send, or a resolve) — mirrors the API's own `_respond_to_turn` doing the
  // same dual duty server-side (see `routers/chat.py`), including the same
  // edge case it documents: a RESUMED turn can itself immediately propose
  // ANOTHER mutation, which lands right back in the `awaiting_approval`
  // branch below exactly like the first proposal would.
  function applyTurn(turn: ChatTurnOut) {
    // Stale chips from whatever prompted THIS turn never belong to what
    // comes next — cleared unconditionally, before branching on `status`, so
    // an approval card or a job-pending row never renders alongside them.
    clearSuggestions();

    if (turn.status === "awaiting_approval" && turn.approval_id && turn.tool_name) {
      setPendingApproval({
        approvalId: turn.approval_id,
        toolName: turn.tool_name,
        toolArgs: turn.tool_args ?? {},
        description: turn.description ?? "",
      });
      return;
    }
    if (turn.status === "job_pending" && turn.job_id) {
      void pollJob(turn.job_id);
      return;
    }
    // "answer" (or, defensively, anything else): narrate if there's content.
    if (turn.content) appendMessage("assistant", turn.content, undefined, turn.citations);
    // Chips only ever follow a genuine plain answer — never an approval/plan
    // turn (the HITL card IS the next move in that case) and never a bare
    // job-pending hand-off. Fired here, non-blocking: the answer above has
    // already rendered by the time this call resolves.
    if (turn.status === "answer" && turn.content) fetchSuggestions();
  }

  // Poll one job to a terminal status (or the MAX_POLLS cap), returning it.
  // Extracted so the revise flow can wait on TWO jobs in sequence: the
  // curriculum_revise row (does the apply happen?) and then its chained
  // curriculum_draft row (did the new lessons actually get written?).
  //
  // ONE FAILED POLL IS NOT A FAILED JOB. These turns run for minutes, and over
  // minutes a single `GET /jobs/{id}` will occasionally not answer: the laptop
  // slept, the wifi blinked, the tunnel in front of the API recycled a
  // connection, the dev server restarted. The job itself never noticed — it is
  // running in the API process, and the very next poll would have found it
  // `succeeded`. Throwing on the first rejection turned that blink into "this
  // failed, try again" and threw away a turn that was about to land. So a
  // rejection is a MISS: wait the same interval and ask again, and only give up
  // after MAX_POLL_MISSES in a row — at which point the connection is genuinely
  // gone, not blinking, and the existing error path is the right one. The
  // counter resets on every answer, so a flaky hour of one-off misses never
  // accumulates into a false failure.
  async function waitForJob(jobId: string): Promise<JobOut> {
    let misses = 0;
    let job: JobOut | undefined;
    for (let polls = 0; polls < MAX_POLLS; polls++) {
      if (polls > 0) await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      try {
        job = await getJob(jobId);
        misses = 0;
      } catch (err) {
        if (++misses >= MAX_POLL_MISSES) throw err;
        continue;
      }
      if (job.status === "succeeded" || job.status === "failed") return job;
    }
    // The cap, reported with the last status a poll actually returned. `job` is
    // always set here: reaching the cap means polls were answering, since
    // MAX_POLL_MISSES unanswered ones in a row throw above.
    if (!job) throw new Error("no poll ever answered");
    return job;
  }

  // Review-fix (chat overhaul Task 5): a chained `curriculum_draft` job can
  // report `status="succeeded"` while still leaving lessons `queued` — the
  // 429 RULE (`jobs/curriculum_draft.py`'s own module docstring): a
  // rate-limited lesson goes back to `queued`, NOT `failed`, so the job as a
  // whole finishes "successfully" the moment it runs out of lessons it CAN
  // draft right now, not when every lesson is actually done. The `waitForJob`
  // above only sees that job-level "succeeded" and would otherwise let a flat
  // "applied" narration paper over lessons that still need writing. This asks
  // the board's own `/progress` endpoint (the same one `TreeBoard`'s progress
  // bar already polls) for the ACTUAL lesson counts, once, right after the
  // draft job settles — cheap by that endpoint's own design (a live GROUP BY,
  // meant to be polled every 2s), and best-effort: a failed check here must
  // never block the "applied" narration, so it degrades to "false" rather
  // than throwing — the board's own progress bar (already refreshed via
  // `onJobDone()`) is the fallback source of truth regardless.
  async function boardStillNeedsDrafting(): Promise<boolean> {
    if (!rootId) return false;
    try {
      const progress = await getCurriculumProgress(rootId);
      return progress.queued > 0 || progress.failed > 0;
    } catch {
      return false;
    }
  }

  async function pollJob(jobId: string) {
    setJobPending(true);
    try {
      const job = await waitForJob(jobId);

      if (job.status === "succeeded") {
        if (rootId && onJobDone && job.kind === "curriculum_revise") {
          // The revise drawer (Unit D, Task D2b): a curriculum_revise job just
          // applied to THIS curriculum, and the tutor is already looking at its
          // board — refresh IT instead of narrating a link away. Gated on the
          // job KIND, not just rootId: a different async tool called from the
          // drawer (e.g. generate_curriculum) must fall through to the deep-link
          // below rather than misreport a revision and refresh the wrong board.
          //
          // Refresh the board NOW (the new lessons appear and its own progress bar
          // starts polling them live), THEN wait on the chained draft job the revise
          // row hands us via `progress.draft_job_id`: a revise SUCCEEDS the moment
          // the tree is right, so a drafting failure would otherwise be a silent
          // "queued". If it failed, say so here too — not just on the board.
          onJobDone();
          const draftJobId = job.progress?.draft_job_id;
          if (draftJobId) {
            const draft = await waitForJob(draftJobId);
            if (draft.status === "failed") {
              appendMessage("assistant", t("revise.appliedDraftFailed", {
                reason: jobErrorText(draft, tJobErrors),
              }));
              return;
            }
            // The draft job itself reports "succeeded" the moment it runs out
            // of lessons it CAN draft right now — the 429 RULE means a
            // rate-limited lesson goes back to `queued`, NOT `failed`, so
            // "succeeded" alone does not mean every lesson actually got
            // written. Checked ONLY here, once the draft job is genuinely
            // terminal-successful (never on a bare `MAX_POLLS` timeout, which
            // leaves `draft.status` at whatever non-terminal value it last
            // polled — that case falls through to `job.stillGenerating`-style
            // silence being wrong for a DIFFERENT reason, not this one).
            if (draft.status === "succeeded" && (await boardStillNeedsDrafting())) {
              appendMessage("assistant", t("revise.appliedNeedsResume"));
              return;
            }
          }
          appendMessage("assistant", t("revise.applied"));
        } else {
          // Deep-link straight to what the job actually produced, BRANCHED
          // ON THE JOB'S KIND: a `draft_lesson_from_selection` job (kind
          // "lesson") reports the drafted LESSON's root Block id in
          // `result_root_id` (see `lib/api.ts`'s job docs), so sending it to
          // `/curricula/{id}` — as this used to unconditionally — 404-ed the
          // one link the narration offered. Lessons open in the outline
          // editor (`/lessons/[lessonId]`), curricula on their own board
          // (Unit A's `/curricula/[rootId]`); each falls back to its own
          // index page in the defensive edge case where `result_root_id`
          // came back unset.
          const isLesson = job.kind === "lesson";
          const section = isLesson ? "lessons" : "curricula";
          appendMessage("assistant", t("job.succeeded"), {
            label: isLesson ? t("job.viewLesson") : t("job.viewCurriculum"),
            href: job.result_root_id
              ? `/${locale}/${section}/${job.result_root_id}`
              : `/${locale}/${section}`,
          });
        }
      } else if (job.status === "failed") {
        setComposerError(jobErrorText(job, tJobErrors));
      } else {
        // Cap exceeded — the job keeps running server-side; give up waiting
        // and tell the tutor to check the Curricula page later instead of
        // polling forever (mirrors `generate-dialog.tsx`'s own
        // `stillGenerating` handling).
        setComposerError(t("job.stillGenerating"));
      }
    } catch (err) {
      setComposerError(err instanceof ApiError ? err.detail : t("job.pollError"));
    } finally {
      setJobPending(false);
    }
  }

  // `hydrating` is in here for the reload-with-a-pending-approval case: until
  // `getPendingApproval` has answered we do not yet KNOW whether this session
  // is blocked, and an enabled composer in that window is a message the API
  // would 409 anyway.
  const composerDisabled = hydrating || sending || pendingApproval != null || jobPending;

  // Plan 11 Task 3 (C4): streams the plain-answer path token-by-token via
  // `streamChatMessage`, with the existing REST `sendChatMessage` as an
  // HONEST FALLBACK for everything that stream endpoint doesn't handle (a
  // tool/mutation call, a post-turn guard trip, a mid-stream error — see
  // `lib/api.ts`'s `ChatStreamOutcome` docstring). A placeholder assistant
  // bubble (`streamId`) is appended up front and grown in place as `onDelta`
  // fires; on a "fallback" outcome that placeholder is REMOVED (never shown
  // half-formed) and this falls through to the exact same `sendChatMessage`
  // + `applyTurn` call the pre-streaming version of this function always
  // made — so the HITL approval-card flow is reached through an UNCHANGED
  // code path no matter which branch got it there.
  //
  // Factored out of `handleSend` (chat overhaul, Piece B) so a suggestion
  // chip can send its own text the SAME way a typed message does — a chip is
  // a shortcut INTO this exact path, never a second one beside it. `handleSend`
  // below is now just the form's own `content` extraction + guard.
  async function sendContent(content: string) {
    if (!content || composerDisabled) return;

    setDraft("");
    clearSuggestions();
    appendMessage("user", content);
    setSending(true);
    setComposerError(null);

    const streamId = crypto.randomUUID();
    setMessages((prev) => [...prev, { id: streamId, role: "assistant", content: "" }]);
    let streamedAnything = false;

    try {
      if (rootId) {
        // THE DRAWER NEVER STREAMS. Its turns are tool-heavy by definition
        // (`propose_curriculum_revision` runs the planner over the whole
        // library) so the SSE endpoint declines every one of them anyway,
        // and the REST resend that follows is exactly the multi-minute
        // request the Cloudflare edge cuts at ~100s. Send it as a
        // `chat_turn` job instead and poll it home: nothing in front of a
        // job can cut it. The empty placeholder bubble goes — the drawer
        // renders its own "planning the revision…" status for this window.
        setMessages((prev) => prev.filter((m) => m.id !== streamId));
        setJobPending(true);
        // Set when this turn ends by handing a SECOND job to `pollJob`, which
        // owns `jobPending` for its own window — clearing the flag here would
        // yank the spinner (and re-enable the composer) out from under it.
        let handedOff = false;
        try {
          const accepted = await sendChatMessageAsync(sessionId, content);
          const job = await waitForJob(accepted.job_id);
          if (job.status === "failed") {
            setComposerError(jobErrorText(job, tJobErrors));
          } else if (job.status !== "succeeded") {
            setComposerError(t("job.stillGenerating"));
          }
          // Hydrate in EVERY case, terminal or not: the turn is persisted
          // server-side, so history (and any approval the turn opened) is
          // the truth here — and a job that failed or outran the cap may
          // still have written part of it.
          await hydrate();
          const turn = job.progress?.turn as ChatTurnOut | undefined;
          // `awaiting_approval` needs nothing: `hydrate` above already read
          // the open approval off `/pending` and moved the model's narration
          // into the card. The other two statuses have no server-side trace
          // to hydrate FROM, so they are driven from the turn itself.
          if (turn?.status === "job_pending" && turn.job_id) {
            handedOff = true;
            void pollJob(turn.job_id);
          }
          if (turn?.status === "answer") fetchSuggestions();
        } finally {
          if (!handedOff) setJobPending(false);
        }
        return;
      }

      const outcome = await streamChatMessage(sessionId, content, (text) => {
        streamedAnything = true;
        setMessages((prev) =>
          prev.map((m) => (m.id === streamId ? { ...m, content: m.content + text } : m)),
        );
      });

      if (outcome.status === "done") {
        if (!streamedAnything) {
          // "done" with zero rendered deltas (an empty answer, or every delta
          // block failed to parse): an empty grey bubble is not a transcript
          // entry. Drop it and re-hydrate — the persisted turn is the truth.
          setMessages((prev) => prev.filter((m) => m.id !== streamId));
          await hydrate();
          return;
        }
        setMessages((prev) =>
          prev.map((m) => (m.id === streamId ? { ...m, citations: outcome.citations } : m)),
        );
        // A genuine streamed answer just rendered — the same "chips follow a
        // real plain answer" moment `applyTurn` reacts to on the REST path.
        fetchSuggestions();
        return;
      }

      if (outcome.status === "error") {
        // TRANSPORT death mid-turn. The server may have finished and
        // persisted the whole billed answer with only the response lost —
        // an automatic REST resend here used to re-run the full
        // retrieval+generation on the tutor's own API key (~2x cost per
        // network blip) and could duplicate the turn. Re-sync from history
        // instead; if the turn didn't land, the composer gets the text back
        // for a one-click manual retry.
        setMessages((prev) => prev.filter((m) => m.id !== streamId));
        await hydrate();
        setDraft((current) => current || content);
        setComposerError(t("streamInterrupted"));
        return;
      }

      // "fallback": the SERVER declined to stream this turn (tool call,
      // guard trip) and guarantees it persisted nothing — the REST resend is
      // safe and is the designed path. Drop the placeholder and resolve the
      // turn the exact same way this component always did, pre-streaming.
      setMessages((prev) => prev.filter((m) => m.id !== streamId));
      const turn = await sendChatMessage(sessionId, content);
      applyTurn(turn);
    } catch (err) {
      if (!streamedAnything) setMessages((prev) => prev.filter((m) => m.id !== streamId));
      if (err instanceof ApiError && err.code === "conversation_too_long") {
        setComposerError(t("conversationTooLong"));
        return;
      }
      if (err instanceof ApiError && err.code === "turn_running") {
        // The PREVIOUS turn is still being answered off the request path (a
        // `chat_turn` job) and the API refused this one before persisting a
        // thing. Nothing is in flight for THIS message, so the slow-turn
        // history poll below would be waiting on an answer that belongs to
        // someone else's question — say so plainly instead.
        setComposerError(t("turnRunning"));
        return;
      }
      if (err instanceof ApiError) {
        setComposerError(err.detail || t("error"));
      } else {
        // TRANSPORT death on the REST resend — in practice the Cloudflare
        // edge cutting a slow turn at ~100s while the server keeps working
        // (verified live 2026-07-21: the reply persisted 8 minutes later,
        // with a revision proposal attached). The turn is NOT lost — poll
        // history until it lands instead of showing a dead error over an
        // answer that is still being written.
        setComposerError(t("slowTurn"));
        const gaveUpAt = Date.now() + 10 * 60 * 1000;
        while (Date.now() < gaveUpAt) {
          await new Promise((r) => setTimeout(r, 10_000));
          try {
            const rows = await getChatHistory(sessionId);
            const last = rows.at(-1);
            const pending = await getPendingApproval(sessionId);
            if (pending || (last && last.role !== "user")) {
              await hydrate();
              setComposerError(null);
              break;
            }
          } catch {
            // a poll that fails is just the next poll's problem
          }
        }
      }
    } finally {
      setSending(false);
      // The sidebar's row for this session is SERVER-derived — the first user
      // message is what names it (and every message moves its preview and its
      // position in the last-activity ordering). A brand-new session isn't in
      // the list at ALL until this turn lands, so without this refresh the
      // conversation you are currently having has no row to click back to.
      void refreshSessions();
    }
  }

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    await sendContent(draft.trim());
  }

  /** A suggestion chip's click handler — sends its text as the next user
   * turn through the exact same `sendContent` the composer's Send button
   * uses (chat overhaul, Piece B: "clicking one sends it as the next user
   * turn; reuse the existing send path"). */
  function handleSuggestionClick(suggestion: string) {
    void sendContent(suggestion);
  }

  async function resolvePending(decision: "approve" | "reject", editedArgs?: Record<string, unknown>) {
    if (!pendingApproval) return;
    setResolving(true);
    setApprovalError(null);
    try {
      const turn = await resolveApproval(sessionId, pendingApproval.approvalId, { decision, editedArgs });
      setPendingApproval(null);
      applyTurn(turn);
    } catch (err) {
      setApprovalError(err instanceof ApiError ? err.detail : t("approval.error"));
    } finally {
      setResolving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {sessionError && (
        <p role="alert" data-testid="chat-session-error" className="text-sm text-destructive">
          {sessionError}
        </p>
      )}

      {hydrating ? (
        <div
          role="status"
          data-testid="chat-hydrating"
          className="flex items-center gap-2 text-sm text-muted-foreground"
        >
          <Loader2 className="size-4 animate-spin" />
        </div>
      ) : (
        <MessageList messages={messages} sessionId={sessionId} />
      )}

      {/* Suggestion chips (chat overhaul, Piece B) — rendered right below the
          transcript, i.e. below the latest assistant message. `disabled`
          mirrors the composer's own gate; in practice `suggestions` is
          already empty whenever an approval/job is pending (`applyTurn`
          clears it before ever setting either), so this guard is defense in
          depth, not the only thing keeping a chip from rendering stale. */}
      {!hydrating && (
        <SuggestionChips
          suggestions={suggestions}
          disabled={composerDisabled}
          onSelect={handleSuggestionClick}
        />
      )}

      {pendingApproval &&
        (pendingApproval.toolName === "apply_curriculum_revision" ? (
          // The Unit D `RevisionPlanCard` variant: same Approve/Reject shell,
          // but rendered from the VALIDATED plan itself (`tool_args.plan`)
          // rather than a raw `tool_args` dump — and applied VERBATIM, so
          // `onApprove` carries no `editedArgs` (there is no edit affordance
          // on this card at all; see its own docstring for why).
          <RevisionPlanCard
            key={pendingApproval.approvalId}
            plan={(pendingApproval.toolArgs.plan as RevisionPlan | undefined) ?? { summary: "", ops: [] }}
            blockTitles={blockTitles ?? {}}
            resolving={resolving}
            error={approvalError}
            onApprove={() => resolvePending("approve")}
            onReject={() => resolvePending("reject")}
          />
        ) : (
          <ApprovalCard
            key={pendingApproval.approvalId}
            description={pendingApproval.description}
            toolName={pendingApproval.toolName}
            toolArgs={pendingApproval.toolArgs}
            resolving={resolving}
            error={approvalError}
            onApprove={(editedArgs) => resolvePending("approve", editedArgs)}
            onReject={() => resolvePending("reject")}
          />
        ))}

      {sending && rootId && (
        // Resolved design call #2: a clear "Planning the revision…" state
        // while `propose_curriculum_revision` runs inline in this turn
        // (20-60s over the whole course + library) — this drawer's whole
        // reason to exist, so any turn sent from it gets this label rather
        // than a bare composer spinner.
        <div
          role="status"
          data-testid="chat-planning-revision"
          className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground"
        >
          <Loader2 className="size-4 shrink-0 animate-spin" />
          {t("revise.planning")}
        </div>
      )}

      {/* Suppressed while the drawer's own "planning the revision…" row is
          up (a drawer turn is now a job, so both conditions hold at once) —
          one honest status row, not two spinners saying different things. */}
      {jobPending && !(sending && rootId) && (
        <div
          role="status"
          data-testid="chat-job-pending"
          className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground"
        >
          <Loader2 className="size-4 shrink-0 animate-spin" />
          {t("job.pending")}
        </div>
      )}

      {/* The composer, visually separated from the transcript above it by its
          own top border — a clean, deliberate seam rather than the input just
          trailing off the last bubble. */}
      <div className="flex flex-col gap-2 border-t border-border pt-3">
        {composerError && (
          <p role="alert" data-testid="chat-error" className="text-sm text-destructive">
            {composerError}
          </p>
        )}

        {/* A BOX HE CAN SEE HIS OWN WORDS IN. This composer was a single-line
            `h-9` Input, and the tutor's instructions to the reviser are not
            one line — they are a paragraph about one lesson, typed in Greek,
            which scrolled out of sight character by character as he wrote it
            ("cannot see what he is writing", 2026-09-15). Three rows that
            auto-grow (field-sizing) up to 40vh, at a flat 16px on EVERY
            breakpoint — the shared Textarea drops to `md:text-sm` on a laptop,
            which is the one place he reads this glasses-off. Enter still
            sends, because that is the muscle memory a chat box owes you;
            Shift+Enter is how you get a second paragraph.

            THE BUTTONS GO UNDERNEATH, NOT BESIDE. One flex row was fine on the
            /chat page's full width and a disaster in the revise drawer, which
            is a `max-w-lg` side panel: the two buttons took their natural width
            and left the textarea about 160px wide — a tall thin column, five
            words to a line, the exact complaint this round set out to fix. The
            box now owns the full width of wherever it is mounted and the
            controls sit on their own row beneath it. */}
        <form onSubmit={handleSend} className="flex flex-col gap-2">
          <Textarea
            data-testid="chat-input"
            rows={3}
            className="max-h-[40vh] min-h-20 overflow-y-auto text-base leading-relaxed md:text-base"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter" || e.shiftKey) return;
              // A Greek/IME candidate window is open: Enter is COMMITTING the
              // word he is typing, not sending the turn. Submitting here would
              // eat the composition and send a half-word.
              if (e.nativeEvent.isComposing) return;
              e.preventDefault();
              // `requestSubmit` first, because it runs the form's own
              // `onSubmit` — Enter and the Send button stay literally one path.
              // But the .deb runs against whatever WebKitGTK the host ships,
              // and older WebKit has no `requestSubmit` at all: calling it
              // there throws and Enter does nothing, on the one build we cannot
              // pin the browser for. Feature-detected, with the same
              // `sendContent(draft.trim())` `handleSend` itself calls as the
              // fallback.
              const form = e.currentTarget.form;
              if (form && typeof form.requestSubmit === "function") {
                form.requestSubmit();
              } else {
                void sendContent(draft.trim());
              }
            }}
            placeholder={t("placeholder")}
            disabled={composerDisabled}
          />
          {/* Send on the right, where a send button belongs; the optional
              "help me word it" button pushed to the far left by `mr-auto`, so
              the thing that COMMITS is never adjacent to the thing that
              rewrites your draft. With no distill button (the /chat page) Send
              simply sits alone at the right. */}
          <div className="flex items-center justify-end gap-2">
            {/* "Talk it through first" exit — curriculum-bound chats only: one
                cheap call distills the conversation into the instruction the
                tutor MEANT and drops it in the composer for him to edit and
                send. Nothing is planned or applied until he presses Send. */}
            {rootId && (
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="mr-auto"
                data-testid="chat-distill"
                disabled={composerDisabled || distilling || messages.length === 0}
                onClick={async () => {
                  setDistilling(true);
                  setComposerError(null);
                  try {
                    const { instruction } = await distillChatInstruction(sessionId);
                    setDraft(instruction);
                  } catch (e) {
                    setComposerError(e instanceof ApiError && e.detail ? e.detail : t("distillFailed"));
                  } finally {
                    setDistilling(false);
                  }
                }}
              >
                {distilling ? <Loader2 className="animate-spin" /> : <Wand2 />}
                {t("distill")}
              </Button>
            )}
            <Button type="submit" size="lg" disabled={composerDisabled || !draft.trim()} data-testid="chat-send">
              {sending ? <Loader2 className="animate-spin" /> : <Send />}
              {t("send")}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
