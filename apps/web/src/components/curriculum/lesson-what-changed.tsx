"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { GitCompare, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { WhatChanged } from "@/components/curriculum/what-changed";
import { ApiError, restoreLessonSegments, type BlockNode } from "@/lib/api";

interface LessonWhatChangedProps {
  lessonId: string;
  prevSegments: { title: string; body: string | null; section?: string | null }[];
  liveSegments: BlockNode[];
  instruction?: string | null;
  onRestored: (block: BlockNode) => void;
}

/** «Τι άλλαξε;» FOR A WHOLE LESSON, plus the only restore in the app.
 *
 * Separate from the segment-level chip in `ExtendWithChat` because the two
 * answer different questions from different data. A segment was EDITED, so its
 * before/after is one block's `prev_body` and its way back is Undo. A lesson was
 * REGENERATED — `modify_lesson` requeues it and a worker rewrites every segment
 * from scratch — so its before/after is the whole segment SET, and Undo cannot
 * reach it at all.
 *
 * Both sides are flattened to one string with the segment titles as headings,
 * because that is what the tutor reads: a lesson is prose, not a data
 * structure, and a diff that made him compare two lists of segments
 * side-by-side would be exactly the homework this feature exists to avoid. The
 * paragraph aligner then does the rest — a segment that survived unchanged
 * collapses, a segment that was rewritten shows as its own block.
 */
export function LessonWhatChanged({
  lessonId,
  prevSegments,
  liveSegments,
  instruction,
  onRestored,
}: LessonWhatChangedProps) {
  const t = useTranslations("curricula.extend");
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const before = flatten(prevSegments.map((s) => ({ title: s.title, body: s.body })));
  const after = flatten(liveSegments.map((s) => ({ title: s.title, body: s.body })));

  async function handleRestore() {
    const ok = await confirm({
      title: t("restoreLessonConfirmTitle"),
      // The copy promises the current version is kept, and the SERVER is what
      // makes that true: `restore_lesson_segments` stashes what it replaces
      // before replacing it. If that ever stops being a toggle, this sentence
      // becomes a lie and must change with it.
      body: t("restoreLessonConfirmBody"),
      confirmLabel: t("restoreLessonConfirm"),
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      onRestored(await restoreLessonSegments(lessonId));
      setOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("restoreLessonError"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          data-testid="lesson-what-changed-trigger"
          disabled={busy}
          onClick={() => setOpen(true)}
        >
          {busy ? <Loader2 className="animate-spin" /> : <GitCompare />}
          {t("whatChanged")}
        </Button>
      </div>

      {error && (
        <p role="alert" data-testid="lesson-restore-error" className="text-xs text-destructive">
          {error}
        </p>
      )}

      <WhatChanged
        open={open}
        onOpenChange={setOpen}
        before={before}
        after={after}
        instruction={instruction}
        onRestore={handleRestore}
        restoreLabel={t("restoreLesson")}
      />
    </div>
  );
}

/** Segments -> one document. The title becomes a heading line so a segment that
 * was renamed shows up as a change rather than silently shifting every
 * paragraph beneath it. */
function flatten(segments: { title: string; body: string | null }[]): string {
  return segments
    .map((s) => [s.title, s.body ?? ""].filter(Boolean).join("\n\n"))
    .filter(Boolean)
    .join("\n\n");
}
