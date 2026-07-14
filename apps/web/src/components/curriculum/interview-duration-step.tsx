"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface InterviewDurationStepProps {
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** Weeks x sessions/week x minutes — the three numbers the ENFORCED shape is
 * derived from (`app.curriculum.shape.plan_shape`), which is why there are three
 * of them now and not two.
 *
 * This is where *"20 weeks, 4 modules"* stops being possible. The old generator
 * asked the model for "about 2-6 modules" and got whatever it felt like; the shape
 * is now arithmetic, and he is agreeing to a SIZE here before anyone spends his
 * money on it. The derived shape is echoed back at him on the very next step
 * ("20 sessions -> 5 modules x 4 lessons -> ~2,200 words each") — computed by the
 * API, not guessed at here.
 */
export function InterviewDurationStep({ submitting, error, onSubmit }: InterviewDurationStepProps) {
  const t = useTranslations("curricula.interview");
  const [weeks, setWeeks] = useState("");
  const [perWeek, setPerWeek] = useState("1");
  const [minutes, setMinutes] = useState("");

  const weeksNum = Number(weeks);
  const perWeekNum = Number(perWeek);
  const minutesNum = Number(minutes);
  const canSubmit =
    weeks !== "" && minutes !== "" && weeksNum > 0 && perWeekNum > 0 && minutesNum > 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmit({
      weeks: Math.round(weeksNum),
      sessions_per_week: Math.round(perWeekNum),
      minutes_per_session: Math.round(minutesNum),
    });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <p className="text-sm font-medium">{t("steps.duration.heading")}</p>

      <fieldset disabled={submitting} className="grid grid-cols-3 gap-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor="interview-duration-weeks">{t("steps.duration.weeksLabel")}</Label>
          <Input
            id="interview-duration-weeks"
            data-testid="interview-duration-weeks"
            type="number"
            min={1}
            value={weeks}
            onChange={(e) => setWeeks(e.target.value)}
            required
          />
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor="interview-duration-per-week">{t("steps.duration.perWeekLabel")}</Label>
          <Input
            id="interview-duration-per-week"
            data-testid="interview-duration-per-week"
            type="number"
            min={1}
            value={perWeek}
            onChange={(e) => setPerWeek(e.target.value)}
            required
          />
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor="interview-duration-minutes">{t("steps.duration.minutesLabel")}</Label>
          <Input
            id="interview-duration-minutes"
            data-testid="interview-duration-minutes"
            type="number"
            min={1}
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
            required
          />
        </div>
      </fieldset>

      {error && (
        <p role="alert" data-testid="interview-step-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <Button
        type="submit"
        disabled={submitting || !canSubmit}
        data-testid="interview-answer-submit"
        className="self-end"
      >
        {t("continue")}
      </Button>
    </form>
  );
}
