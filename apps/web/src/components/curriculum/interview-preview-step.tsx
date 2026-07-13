"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { AlertTriangle, BookOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { InterviewOption, InterviewPreview } from "@/lib/api";

interface InterviewPreviewStepProps {
  findings: InterviewPreview;
  sourceCatalog: Map<string, InterviewOption>;
  locale: string;
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** Fourth step (`STEP_ORDER[3]`) — THE step Chris asked for ("i think it
 * would be beneficial here to select things from our library"): what the
 * planned outline actually found in the tutor's chosen sources, module by
 * module, with an honest gap flag rather than a silent fallback to the
 * model's general knowledge. `sourceCatalog` (source title -> the "sources"
 * step's own option, built by `InterviewDialog` from that earlier step's
 * response) is what lets a citation deep-link into the Reader at the real
 * page: `InterviewPassage` only carries `source_title` on the wire, never
 * `source_id` (`_compute_preview` in `app.curriculum.interview` never
 * serializes it) — see that type's own docstring in `lib/api.ts`. A passage
 * whose source can't be resolved (title collision, or a page-less passage)
 * still renders as plain text rather than a dead link. */
export function InterviewPreviewStep({
  findings,
  sourceCatalog,
  locale,
  submitting,
  error,
  onSubmit,
}: InterviewPreviewStepProps) {
  const t = useTranslations("curricula.interview");

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-medium">{t("steps.preview.heading")}</p>
        <p className="text-xs text-muted-foreground" data-testid="interview-preview-summary">
          {findings.gap_count === 0
            ? t("steps.preview.gapCountZero")
            : t("steps.preview.gapSummary", { count: findings.gap_count, total: findings.modules.length })}
        </p>
      </div>

      <div className="flex max-h-80 flex-col gap-2 overflow-y-auto pr-1">
        {findings.modules.map((module, i) => (
          <div
            key={`${module.title}-${i}`}
            data-testid="interview-module"
            className="flex flex-col gap-1.5 rounded-xl border border-border bg-card p-3 text-sm ring-1 ring-foreground/10"
          >
            <span className="font-medium">{module.title}</span>
            {module.objective && <span className="text-xs text-muted-foreground">{module.objective}</span>}

            {module.gap ? (
              <div
                data-testid={`interview-gap-${i}`}
                className="flex items-center gap-1.5 text-xs font-medium text-amber-600 dark:text-amber-400"
              >
                <AlertTriangle className="size-3.5 shrink-0" />
                {t("steps.preview.gapLabel")}
              </div>
            ) : (
              <div className="flex flex-col gap-1">
                {module.passages.map((passage, pi) => {
                  const source = sourceCatalog.get(passage.source_title);
                  const label = t("steps.preview.citation", {
                    source: passage.source_title,
                    page: passage.page_no ?? "?",
                  });
                  return source && passage.page_no != null ? (
                    <Link
                      key={pi}
                      href={`/${locale}/library/${source.value}?page=${passage.page_no}`}
                      data-testid={`interview-passage-${i}-${pi}`}
                      className="inline-flex w-fit items-center gap-1.5 rounded-full border border-primary/30 bg-primary/5 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:bg-primary/10 hover:text-foreground"
                    >
                      <BookOpen className="size-3.5 shrink-0 text-primary" />
                      {label}
                    </Link>
                  ) : (
                    <span
                      key={pi}
                      data-testid={`interview-passage-${i}-${pi}`}
                      className="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground"
                    >
                      <BookOpen className="size-3.5 shrink-0 text-primary" />
                      {label}
                    </span>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </div>

      {error && (
        <p role="alert" data-testid="interview-step-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <Button
        type="button"
        disabled={submitting}
        data-testid="interview-answer-submit"
        className="self-end"
        onClick={() => onSubmit({ proceed: true })}
      >
        {t("steps.preview.proceed")}
      </Button>
    </div>
  );
}
