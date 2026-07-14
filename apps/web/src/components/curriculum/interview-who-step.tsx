"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import type { InterviewOption } from "@/lib/api";

interface InterviewWhoStepProps {
  /** The roster, plus a first option with `value: "none"` — the API builds that
   * one itself (`describe_step`); it is not a client-side affordance bolted on. */
  options: InterviewOption[];
  /** `findings.levels` — the levels the API will accept. Not hardcoded here: a
   * level this client invented would just be re-asked by the validator. */
  levels: string[];
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

const NO_STUDENT = "none";

/** The interview's first step — and THE STUDENT IS OPTIONAL.
 *
 * Chris, verbatim: "this has to be optional dude.. the student part here has to be
 * TOTALLY optional". So "no particular student" is a first-class option with its
 * own button, not a field left blank. And when there is no student there is an
 * explicit LEVEL selector — "who is this for" and "how advanced are they" are two
 * questions, and only one of them needs a person.
 *
 * The level selector disappears once a real student is picked, because his own
 * level (on file) wins over it anyway (`_answer_who`), and a control that cannot
 * change the outcome is a lie about what the app is doing.
 */
export function InterviewWhoStep({ options, levels, submitting, error, onSubmit }: InterviewWhoStepProps) {
  const t = useTranslations("curricula.interview");

  const [selected, setSelected] = useState<string>(NO_STUDENT);
  const [level, setLevel] = useState<string>(levels[0] ?? "all_levels");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    onSubmit({ student_id: selected === NO_STUDENT ? null : selected, level });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <p className="text-sm font-medium">{t("steps.who.heading")}</p>

      <fieldset disabled={submitting} className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("steps.who.heading")}>
          {options.map((opt) => (
            <Button
              key={opt.value}
              type="button"
              size="sm"
              variant={selected === opt.value ? "default" : "outline"}
              data-testid={`interview-who-option-${opt.value}`}
              aria-pressed={selected === opt.value}
              onClick={() => setSelected(opt.value)}
            >
              {opt.value === NO_STUDENT ? t("steps.who.noStudent") : opt.label}
            </Button>
          ))}
        </div>

        {selected === NO_STUDENT && (
          <div className="flex flex-col gap-1.5" data-testid="interview-who-levels">
            <span className="text-xs text-muted-foreground">{t("steps.who.levelHint")}</span>
            <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("steps.who.levelHint")}>
              {levels.map((lvl) => (
                <Button
                  key={lvl}
                  type="button"
                  size="sm"
                  variant={level === lvl ? "default" : "outline"}
                  data-testid={`interview-who-level-${lvl}`}
                  aria-pressed={level === lvl}
                  onClick={() => setLevel(lvl)}
                >
                  {t.has(`steps.who.levels.${lvl}`) ? t(`steps.who.levels.${lvl}`) : lvl}
                </Button>
              ))}
            </div>
          </div>
        )}
      </fieldset>

      {error && (
        <p role="alert" data-testid="interview-step-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <Button type="submit" disabled={submitting} data-testid="interview-answer-submit" className="self-end">
        {t("continue")}
      </Button>
    </form>
  );
}
