"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApprovalCard } from "@/components/chat/approval-card";
import { RevisionPlanCard } from "@/components/chat/revision-plan-card";
import { MessageList, type ChatDisplayMessage } from "@/components/chat/message-list";
import { useChatSessions } from "@/components/chat/chat-sessions";
import {
  ApiError,
  getChatHistory,
  getCurriculumProgress,
  getJob,
  getPendingApproval,
  resolveApproval,
  sendChatMessage,
  streamChatMessage,
  type ChatCitation,
  type ChatMessageOut,
  type ChatTurnOut,
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
const MAX_POLLS = 150;

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
export function ChatPanel({ sessionId, rootId, blockTitles, onJobDone }: ChatPanelProps) {
  const t = useTranslations("chat");
  const tJobErrors = useTranslations("jobErrors");
  const locale = useLocale();
  const { refresh: refreshSessions } = useChatSessions();

  const [hydrating, setHydrating] = useState(true);
  const [sessionError, setSessionError] = useState<string | null>(null);

  const [messages, setMessages] = useState<ChatDisplayMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [composerError, setComposerError] = useState<string | null>(null);

  const [pendingApproval, setPendingApproval] = useState<PendingApprovalState | null>(null);
  const [resolving, setResolving] = useState(false);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  const [jobPending, setJobPending] = useState(false);

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
  }

  // Poll one job to a terminal status (or the MAX_POLLS cap), returning it.
  // Extracted so the revise flow can wait on TWO jobs in sequence: the
  // curriculum_revise row (does the apply happen?) and then its chained
  // curriculum_draft row (did the new lessons actually get written?).
  async function waitForJob(jobId: string) {
    let job = await getJob(jobId);
    let polls = 1;
    while (job.status !== "succeeded" && job.status !== "failed" && polls < MAX_POLLS) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      job = await getJob(jobId);
      polls++;
    }
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
          // Deep-link straight to the materialized curriculum's own board
          // (Unit A's `/curricula/[rootId]` route) when the job says which
          // one it is; fall back to the plain Curricula index in the
          // defensive edge case where `result_root_id` came back unset.
          appendMessage("assistant", t("job.succeeded"), {
            label: t("job.viewCurriculum"),
            href: job.result_root_id
              ? `/${locale}/curricula/${job.result_root_id}`
              : `/${locale}/curricula`,
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
  async function handleSend(e: FormEvent) {
    e.preventDefault();
    const content = draft.trim();
    if (!content || composerDisabled) return;

    setDraft("");
    appendMessage("user", content);
    setSending(true);
    setComposerError(null);

    const streamId = crypto.randomUUID();
    setMessages((prev) => [...prev, { id: streamId, role: "assistant", content: "" }]);
    let streamedAnything = false;

    try {
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
      setComposerError(err instanceof ApiError ? err.detail : t("error"));
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

      {jobPending && (
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

        <form onSubmit={handleSend} className="flex gap-2">
          <Input
            data-testid="chat-input"
            className="h-9"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={t("placeholder")}
            disabled={composerDisabled}
          />
          <Button type="submit" size="lg" disabled={composerDisabled || !draft.trim()} data-testid="chat-send">
            {sending ? <Loader2 className="animate-spin" /> : <Send />}
            {t("send")}
          </Button>
        </form>
      </div>
    </div>
  );
}
