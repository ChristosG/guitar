"use client";

import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TierBadge } from "@/components/curriculum/tier-badge";
import { outlineTotals } from "@/components/curriculum/outline-cost";
import type { InterviewFindings } from "@/lib/api";

interface InterviewConfirmStepProps {
  /** The outline HE edited — the API echoes back exactly what it stored, so this
   * is the last chance to notice that what is about to be built is not what he
   * meant. */
  findings: InterviewFindings | null;
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** The last click before the money. It says what it will cost and what will
 * happen, and then it gets out of the way — the board opens IMMEDIATELY on a real
 * tree of queued lessons (materialization happens at confirm, not at the end of
 * drafting), so this dialog is not a waiting room. He watches the lessons arrive
 * on the board, and reads module 1 while module 5 is still being written.
 *
 * `allow_general` is gone from this step. It used to be a checkbox HERE, after the
 * outline had already been planned — which meant the gap policy arrived too late
 * to influence the plan it was supposed to govern. It is the "scope" step's
 * question now, asked before the model reads anything.
 */
export function InterviewConfirmStep({ findings, submitting, error, onSubmit }: InterviewConfirmStepProps) {
  const t = useTranslations("curricula.interview");

  const modules = findings?.modules ?? [];
  const totals = outlineTotals(modules);

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-medium" data-testid="interview-confirm-heading">
          {t("steps.confirm.heading", { title: findings?.title ?? "" })}
        </p>
        <p className="text-xs text-muted-foreground" data-testid="interview-confirm-summary">
          {t("steps.confirm.summary", {
            modules: totals.modules,
            lessons: totals.lessons,
            words: totals.words,
            cost: totals.costUsd.toFixed(2),
          })}
        </p>
      </div>

      <ul className="flex max-h-56 flex-col gap-1.5 overflow-y-auto pr-1">
        {modules.map((module, i) => (
          <li
            key={`${module.title}-${i}`}
            data-testid="interview-confirm-module"
            className="flex min-w-0 items-center gap-2 rounded-xl border border-border bg-card px-3 py-2 text-sm ring-1 ring-foreground/10"
          >
            <span className="min-w-0 flex-1 truncate">{module.title}</span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {t("steps.confirm.lessonCount", { count: module.lessons.length })}
            </span>
            <TierBadge tier={module.tier} coverageNote={module.coverage_note} />
          </li>
        ))}
      </ul>

      <p className="text-xs text-muted-foreground">{t("steps.confirm.backgroundHint")}</p>

      {error && (
        <p role="alert" data-testid="interview-step-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <Button
        type="button"
        disabled={submitting}
        data-testid="interview-confirm-submit"
        className="self-end"
        onClick={() => onSubmit({ approved: true })}
      >
        {submitting && <Loader2 className="animate-spin" />}
        {t("steps.confirm.approve")}
      </Button>
    </div>
  );
}
