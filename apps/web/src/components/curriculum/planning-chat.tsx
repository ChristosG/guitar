"use client";

import { useEffect, useState } from "react";
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

  useEffect(() => {
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
          <div className="min-h-0 flex-1">
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
