"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ChatSessionsProvider } from "@/components/chat/chat-sessions";
import {
  ApiError, distillInterviewBrief, getOrCreateInterviewChatSession, putInterviewPlanningBrief,
} from "@/lib/api";

interface PlanningChatProps {
  interviewId: string;
  onDone: () => void; // proceed to the interview steps (with or without a saved brief)
}

/** Part 5 — «Συζήτησέ το πρώτα»: chat -> distill -> EDIT -> save -> proceed.
 * The brief is an artifact BETWEEN the chat and the generator: the tutor
 * audits what the machine understood before generation spends time and money
 * (the input-side mirror of the revise engine's approve-before-apply). */
export function PlanningChat({ interviewId, onDone }: PlanningChatProps) {
  const t = useTranslations("curricula.planning");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [phase, setPhase] = useState<"chat" | "edit">("chat");
  const [brief, setBrief] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guard against React Strict Mode's dev double-invoke of mount effects.
  // Harmless for two GETs against `getOrCreateInterviewChatSession` (it's
  // idempotent-resume either way), but there is no reason to fire it twice —
  // the ref keeps it to one call, mirroring `chat-panel.tsx`'s own hydration
  // effect guard for the identical caveat.
  const sessionRequestedRef = useRef(false);
  useEffect(() => {
    if (sessionRequestedRef.current) return;
    sessionRequestedRef.current = true;
    getOrCreateInterviewChatSession(interviewId)
      .then((r) => setSessionId(r.session_id))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("sessionError")));
  }, [interviewId, t]);

  const handleDistill = async () => {
    setBusy(true); setError(null);
    try {
      const r = await distillInterviewBrief(interviewId);
      setBrief(r.brief);
      setPhase("edit");
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 409
          ? t("distillEmpty")
          : err instanceof ApiError ? err.detail : t("distillError"),
      );
    } finally {
      setBusy(false);
    }
  };

  const handleSaveAndContinue = async () => {
    setBusy(true); setError(null);
    try {
      await putInterviewPlanningBrief(interviewId, brief);
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("saveError"));
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-3" data-testid="planning-chat">
      {phase === "chat" && (
        <>
          <p className="text-sm text-muted-foreground">{t("chatHint")}</p>
          {/* This is its own scroll region, distinct from `DialogBody`'s
              (`dialog.tsx`'s docstring calls that one "the one part allowed
              to scroll" — true everywhere else). Without `overflow-y-auto`
              here, a transcript taller than this box's flex-computed height
              didn't get clipped OR scroll — it just painted past its own
              edges (`overflow: visible`'s default), overlapping the
              Skip/Use-this-plan buttons below instead of letting the tutor
              scroll up through it. `min-h-0` is what lets `flex-1` shrink
              this box below its content size at all; `overflow-y-auto` is
              what turns that shrunk box into an actual scroll container. Both
              only produce a BOUNDED box because `InterviewDialog` gives
              `DialogContent` a DEFINITE height for this phase (`h-[85dvh]`,
              not just the shared `max-h`) — a flex item's `flex-1`/`min-h-0`
              needs a definite ancestor size to shrink-and-scroll against. */}
          <div className="min-h-0 flex-1 overflow-y-auto" data-testid="planning-transcript">
            {sessionId ? (
              <ChatSessionsProvider>
                <ChatPanel key={sessionId} sessionId={sessionId} />
              </ChatSessionsProvider>
            ) : (
              !error && <Loader2 className="animate-spin" aria-label={t("loading")} />
            )}
          </div>
          <div className="flex items-center justify-between gap-2">
            <Button type="button" variant="ghost" disabled={busy}
                    data-testid="planning-skip" onClick={onDone}>
              {t("skip")}
            </Button>
            <Button type="button" disabled={busy || !sessionId}
                    data-testid="planning-distill" onClick={handleDistill}>
              {busy ? <Loader2 className="animate-spin" /> : <MessageCircle />}
              {t("usePlan")}
            </Button>
          </div>
        </>
      )}
      {phase === "edit" && (
        <>
          <p className="text-sm text-muted-foreground">{t("editHint")}</p>
          <Textarea value={brief} onChange={(e) => setBrief(e.target.value)}
                    rows={14} disabled={busy} data-testid="planning-brief-editor" />
          <div className="flex items-center justify-between gap-2">
            <Button type="button" variant="outline" disabled={busy} onClick={() => setPhase("chat")}>
              {t("backToChat")}
            </Button>
            <Button type="button" disabled={busy || !brief.trim()}
                    data-testid="planning-continue" onClick={handleSaveAndContinue}>
              {busy && <Loader2 className="animate-spin" />}
              {t("continueToWizard")}
            </Button>
          </div>
        </>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
