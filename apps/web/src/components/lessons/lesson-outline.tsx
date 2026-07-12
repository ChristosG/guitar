"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SessionCard } from "@/components/lessons/session-card";
import { ApiError, addSession, type BlockNode } from "@/lib/api";

interface LessonOutlineProps {
  lessonId: string;
  /** The lesson's own `children` — already ordered by `order`
   * (`block_to_tree`'s own sort, server-side), so this renders them
   * straight through with no client-side re-sort. */
  sessions: BlockNode[];
  applyTree: (tree: BlockNode) => void;
  refreshTree: () => Promise<void>;
}

/** The outline itself: sessions in order, each an obvious `SessionCard`
 * (title, length, items, split/merge/delete), plus one "Add a session"
 * affordance at the end — the ONLY thing on this page that creates
 * anything. Deliberately thin: every actual mutation lives in `SessionCard`
 * (structural: split/merge/delete) or is delegated straight to the API
 * here (add) — this component's own job is just "lay the sessions out in
 * order and offer the one way to add another." */
export function LessonOutline({ lessonId, sessions, applyTree, refreshTree }: LessonOutlineProps) {
  const t = useTranslations("lessons.editor");

  const [addingSession, setAddingSession] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  async function handleAddSession() {
    setAddingSession(true);
    setAddError(null);
    try {
      const tree = await addSession(lessonId, {
        title: t("newSessionTitle"),
        after: sessions.at(-1)?.id ?? null,
      });
      applyTree(tree);
    } catch (err) {
      setAddError(err instanceof ApiError ? err.detail : t("addSessionError"));
    } finally {
      setAddingSession(false);
    }
  }

  return (
    <div className="flex flex-col gap-4" data-testid="lesson-outline">
      {sessions.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="lesson-outline-empty">
          {t("noSessions")}
        </p>
      ) : (
        sessions.map((session, i) => (
          <SessionCard
            key={session.id}
            session={session}
            lessonId={lessonId}
            index={i}
            nextSession={sessions[i + 1] ?? null}
            applyTree={applyTree}
            refreshTree={refreshTree}
          />
        ))
      )}

      {addError && (
        <p role="alert" data-testid="add-session-error" className="text-sm text-destructive">
          {addError}
        </p>
      )}

      <Button
        type="button"
        variant="outline"
        disabled={addingSession}
        onClick={handleAddSession}
        data-testid="add-session"
        className="w-fit self-start border-dashed"
      >
        <Plus />
        {addingSession ? t("addingSession") : t("addSession")}
      </Button>
    </div>
  );
}
