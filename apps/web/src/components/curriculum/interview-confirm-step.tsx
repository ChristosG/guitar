"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import type { InterviewPreview } from "@/lib/api";

interface InterviewConfirmStepProps {
  findings: InterviewPreview | null;
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** Final step (`STEP_ORDER[4]`) — approve, and decide whether a gap module
 * may be filled from labelled general knowledge (`allow_general`, only
 * offered when there's actually a gap to fill — a control with nothing to
 * control would just be noise). Approving posts straight to
 * `answerInterview`, whose response on this exact step is a 202
 * `JobAccepted`, not another `InterviewStateOut` — `InterviewDialog` is what
 * discriminates that (`isJobAccepted`) and starts the job-poll phase. */
export function InterviewConfirmStep({ findings, submitting, error, onSubmit }: InterviewConfirmStepProps) {
  const t = useTranslations("curricula.interview");
  const [allowGeneral, setAllowGeneral] = useState(false);

  const gapCount = findings?.gap_count ?? 0;

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm font-medium" data-testid="interview-confirm-heading">
        {t("steps.confirm.heading", { title: findings?.course_title ?? "" })}
      </p>

      {gapCount > 0 && (
        <label className="flex items-start gap-2 rounded-xl border border-border bg-card p-3 text-sm ring-1 ring-foreground/10">
          <input
            type="checkbox"
            data-testid="interview-allow-general"
            checked={allowGeneral}
            onChange={(e) => setAllowGeneral(e.target.checked)}
            className="mt-0.5 size-4 shrink-0 accent-primary"
          />
          <span>{t("steps.confirm.allowGeneralLabel")}</span>
        </label>
      )}

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
        onClick={() => onSubmit({ approved: true, allow_general: allowGeneral })}
      >
        {t("steps.confirm.approve")}
      </Button>
    </div>
  );
}
