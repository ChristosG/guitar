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

/** Second step (`STEP_ORDER[1]`): weeks + minutes/session — the two numbers
 * `_answer_duration` validates strictly (both required, both > 0, a `bool`
 * rejected explicitly since Python's `bool` is an `int` subclass — see that
 * function's own docstring on the API side). */
export function InterviewDurationStep({ submitting, error, onSubmit }: InterviewDurationStepProps) {
  const t = useTranslations("curricula.interview");
  const [weeks, setWeeks] = useState("");
  const [minutes, setMinutes] = useState("");

  const weeksNum = Number(weeks);
  const minutesNum = Number(minutes);
  const canSubmit = weeks !== "" && minutes !== "" && weeksNum > 0 && minutesNum > 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmit({ weeks: Math.round(weeksNum), minutes_per_session: Math.round(minutesNum) });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <p className="text-sm font-medium">{t("steps.duration.heading")}</p>

      <fieldset disabled={submitting} className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
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
        <div className="flex flex-col gap-1.5">
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
