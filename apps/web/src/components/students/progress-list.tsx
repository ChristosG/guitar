"use client";

import { useTranslations } from "next-intl";
import { ProgressRow } from "@/components/students/progress-row";
import type { ProgressOut } from "@/lib/api";

interface ProgressListProps {
  studentId: string;
  progress: ProgressOut[];
  onChanged: (updated: ProgressOut) => void;
}

/** Thin wrapper around `ProgressRow`, same "wrapper owns empty state, item
 * owns its own mutation" split as `curriculum/tree-board.tsx` + `block-
 * card.tsx`. */
export function ProgressList({ studentId, progress, onChanged }: ProgressListProps) {
  const t = useTranslations("students.detail.progress");

  if (progress.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="student-progress-empty">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="progress-list">
      {progress.map((row) => (
        <ProgressRow key={row.id} studentId={studentId} progress={row} onChanged={onChanged} />
      ))}
    </div>
  );
}
