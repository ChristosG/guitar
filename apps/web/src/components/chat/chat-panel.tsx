"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApprovalCard } from "@/components/chat/approval-card";
import { MessageList, type ChatDisplayMessage } from "@/components/chat/message-list";
import {
  ApiError,
  createChatSession,
  getJob,
  resolveApproval,
  sendChatMessage,
  streamChatMessage,
  type ChatCitation,
  type ChatTurnOut,
} from "@/lib/api";

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
 *    linking to `/{locale}/curricula` (or surface the failure/still-
 *    generating case as a composer-area error) and re-enable the composer
 *    either way.
 *
 * A session is created once, on mount (`createChatSession`, no `student_id`
 * — this page isn't scoped to one student) — this page never resumes a
 * previous session (no session id kept in the URL/storage yet), so
 * `lib/api.ts`'s `getChatHistory`/`getPendingApproval` have nothing to
 * hydrate here and aren't called from this component (see their own
 * docstrings in `lib/api.ts` for when they would be).
 */
export function ChatPanel() {
  const t = useTranslations("chat");
  const locale = useLocale();

  const [sessionId, setSessionId] = useState<string | null>(null);
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
  const startSession = useCallback(() => {
    return createChatSession()
      .then((res) => setSessionId(res.session_id))
      .catch((err) => setSessionError(err instanceof ApiError ? err.detail : t("sessionError")));
  }, [t]);

  // Guard against React Strict Mode's dev double-invoke of mount effects,
  // which would otherwise POST /chat twice and create two sessions. The ref
  // persists across the strict-mode unmount/remount of the same instance, so
  // the session is created exactly once.
  const startedRef = useRef(false);
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    startSession();
  }, [startSession]);

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

  async function pollJob(jobId: string) {
    setJobPending(true);
    try {
      let job = await getJob(jobId);
      let polls = 1;
      while (job.status !== "succeeded" && job.status !== "failed" && polls < MAX_POLLS) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        job = await getJob(jobId);
        polls++;
      }

      if (job.status === "succeeded") {
        // A generic link to the Curricula list, not a `getCurriculum
        // (result_root_id)` deep link — the brief only asks for a hand-off
        // back to that page, and a list link degrades gracefully even in
        // the defensive edge case where `result_root_id` came back unset.
        appendMessage("assistant", t("job.succeeded"), {
          label: t("job.viewCurriculum"),
          href: `/${locale}/curricula`,
        });
      } else if (job.status === "failed") {
        setComposerError(job.error ?? t("job.failed"));
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

  const composerDisabled = !sessionId || sending || pendingApproval != null || jobPending;

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
    if (!content || !sessionId || composerDisabled) return;

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
        setMessages((prev) =>
          prev.map((m) => (m.id === streamId ? { ...m, citations: outcome.citations } : m)),
        );
        return;
      }

      // "fallback": drop the (possibly partial) streamed placeholder — it
      // was never persisted server-side either (see `streamChatMessage`'s
      // docstring) — and resolve this turn the exact same way this
      // component always did, pre-streaming.
      setMessages((prev) => prev.filter((m) => m.id !== streamId));
      const turn = await sendChatMessage(sessionId, content);
      applyTurn(turn);
    } catch (err) {
      if (!streamedAnything) setMessages((prev) => prev.filter((m) => m.id !== streamId));
      setComposerError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSending(false);
    }
  }

  async function resolvePending(decision: "approve" | "reject", editedArgs?: Record<string, unknown>) {
    if (!sessionId || !pendingApproval) return;
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

      <MessageList messages={messages} />

      {pendingApproval && (
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

      {composerError && (
        <p role="alert" data-testid="chat-error" className="text-sm text-destructive">
          {composerError}
        </p>
      )}

      <form onSubmit={handleSend} className="flex gap-2">
        <Input
          data-testid="chat-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={t("placeholder")}
          disabled={composerDisabled}
        />
        <Button type="submit" disabled={composerDisabled || !draft.trim()} data-testid="chat-send">
          {sending ? <Loader2 className="animate-spin" /> : <Send />}
          {t("send")}
        </Button>
      </form>
    </div>
  );
}
