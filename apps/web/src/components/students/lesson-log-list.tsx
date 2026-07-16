"use client";

import { useLocale, useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { BlockTitle } from "@/components/students/block-title";
import type { LessonLogOut } from "@/lib/api";

/** ISO "YYYY-MM-DD" → the UI locale's own format ("16/07/2026" for el).
 * Rendered raw, a machine date was the one non-Greek thing on the page.
 * Parsed manually (not `new Date(iso)`) to stay timezone-proof: an ISO date
 * string is parsed as UTC midnight, which formats as the PREVIOUS day in any
 * negative-offset zone. */
function formatDate(iso: string, locale: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  const date = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(date);
}

interface LessonLogListProps {
  lessons: LessonLogOut[];
}

/** Read-only "recent lessons" list — no per-row mutation (a LessonLog is
 * always a fresh insert, never edited from this UI — see `LessonLogInput`'s
 * docstring in `lib/api.ts`), so unlike Progress this stays one component
 * instead of a list+row pair. */
export function LessonLogList({ lessons }: LessonLogListProps) {
  const t = useTranslations("students.detail.lessons");
  const locale = useLocale();

  if (lessons.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="student-lessons-empty">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="lesson-list">
      {lessons.map((log) => (
        <div
          key={log.id}
          data-testid="lesson-item"
          data-lesson-id={log.id}
          className="flex flex-col gap-1.5 rounded-xl border border-border bg-card p-3 text-sm ring-1 ring-foreground/10"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium" data-testid="lesson-block-title">
              <BlockTitle blockId={log.session_block_id} />
            </span>
            <div className="flex items-center gap-1.5">
              {log.date && (
                <Badge variant="outline" data-testid="lesson-date">
                  {formatDate(log.date, locale)}
                </Badge>
              )}
              <Badge variant={log.taught ? "default" : "secondary"} data-testid="lesson-taught">
                {log.taught ? t("taught") : t("notTaught")}
              </Badge>
            </div>
          </div>
          {log.notes && (
            <p className="text-xs text-muted-foreground" data-testid="lesson-notes">
              {log.notes}
            </p>
          )}
          {log.homework && (
            <p className="text-xs text-muted-foreground" data-testid="lesson-homework">
              <span className="font-medium">{t("homeworkLabel")}: </span>
              {log.homework}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
