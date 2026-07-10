"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BlockTitle } from "@/components/students/block-title";
import { ApiError, upsertProgress, type ProgressOut } from "@/lib/api";

/** The 4 statuses `app.models.curriculum.Progress.status`'s own comment
 * documents (`not_started | introduced | practicing | mastered`) — kept as
 * a plain array here (not exported/shared elsewhere) since, unlike
 * `components/artifacts/kinds.ts`'s `ARTIFACT_KINDS`, this list is only
 * ever used in this one component. */
const PROGRESS_STATUSES = ["not_started", "introduced", "practicing", "mastered"] as const;

interface ProgressRowProps {
  studentId: string;
  progress: ProgressOut;
  onChanged: (updated: ProgressOut) => void;
}

/** One Progress row: the block's title (resolved lazily via `BlockTitle`),
 * its existing notes (read-only here — the brief's "editable status
 * control" covers `status` only), and a small button-group to change status
 * — mirrors `curriculum/attach-artifact-dialog.tsx`'s kind-picker "row of
 * toggle buttons" pattern (this app has no Select primitive for a short,
 * fixed list — see `notes/student-select.tsx`'s docstring for the one place
 * a native `<select>` was used instead, for a much longer, open-ended list;
 * 4 known statuses fits the button-row treatment fine).
 *
 * Resending `progress.notes` unchanged on every status click is NOT
 * optional: `POST /students/{id}/progress` OVERWRITES `notes` wholesale
 * (see `lib/api.ts`'s `ProgressInput` docstring / `upsert_progress`'s own
 * docstring on the API side) — omitting it would silently blank out any
 * existing note. Reads `progress.notes` straight from this component's own
 * prop each click (never a separate local copy), so it's always whatever
 * the parent's array currently holds — no chance of resending stale notes.
 */
export function ProgressRow({ studentId, progress, onChanged }: ProgressRowProps) {
  const t = useTranslations("students.detail.progress");

  const [submittingStatus, setSubmittingStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleStatusChange(status: string) {
    if (status === progress.status) return;
    setSubmittingStatus(status);
    setError(null);
    try {
      const updated = await upsertProgress(studentId, {
        blockId: progress.block_id,
        status,
        notes: progress.notes, // preserve — see this component's own docstring above
      });
      onChanged(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("updateError"));
    } finally {
      setSubmittingStatus(null);
    }
  }

  return (
    <div
      data-testid="progress-item"
      data-progress-id={progress.id}
      className="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 text-sm ring-1 ring-foreground/10"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium" data-testid="progress-block-title">
          <BlockTitle blockId={progress.block_id} />
        </span>
        <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("statusLabel")}>
          {PROGRESS_STATUSES.map((status) => (
            <Button
              key={status}
              type="button"
              size="xs"
              variant={progress.status === status ? "default" : "outline"}
              aria-pressed={progress.status === status}
              disabled={submittingStatus != null}
              data-testid={`progress-status-${status}`}
              onClick={() => handleStatusChange(status)}
            >
              {submittingStatus === status && <Loader2 className="animate-spin" />}
              {t(`status.${status}`)}
            </Button>
          ))}
        </div>
      </div>
      {progress.notes && (
        <p className="text-xs text-muted-foreground" data-testid="progress-notes">
          {progress.notes}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="progress-update-error" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
