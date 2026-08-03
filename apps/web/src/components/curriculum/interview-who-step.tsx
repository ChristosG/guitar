"use client";

import { useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";

interface InterviewWhoStepProps {
  /** `findings.levels` — the levels the API will accept. Not hardcoded here: a
   * level this client invented would just be re-asked by the validator. */
  levels: string[];
  /** `findings.languages` — the course languages the API accepts ("el"/"en").
   * Same contract as `levels`: the server owns the list. */
  languages: string[];
  /** `InterviewStateOut.prior` — the answer this step already has, present when
   * the tutor navigated BACK. Seeds the initial selection so Continue
   * re-submits his earlier choice instead of the blank defaults. */
  prior?: unknown;
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** The interview's first step — LEVEL and LANGUAGE. The step key is still
 * "who" (renaming it would strand crash-resumable interviews persisted at
 * `step="who"`), but the student roster it used to offer left the product.
 *
 * What survives is the two questions the generation actually consumes:
 * how advanced the course is pitched (`target_profile.level`) and which
 * language it is written in (`Block.language`). The language is asked
 * EXPLICITLY, defaulting to the cockpit locale but never bound to it —
 * before this, picking the one English-speaking student was the only way
 * the wizard could produce an English course, and 5 of 6 real courses are
 * English. A Greek-UI tutor writing English courses is the normal case.
 *
 * `student_id: null` still rides on the answer: the server keeps the
 * historical `answers["who"]` shape so older in-flight interviews resume
 * cleanly.
 */
export function InterviewWhoStep({ levels, languages, prior, submitting, error, onSubmit }: InterviewWhoStepProps) {
  const t = useTranslations("curricula.interview");
  const uiLocale = useLocale();

  // A legacy `prior` (from an interview answered before the roster was
  // removed) may carry `student_id`/no `level` — both fall back safely.
  const p = (prior ?? null) as { level?: string | null; language?: string | null } | null;
  const [level, setLevel] = useState<string>(p?.level ?? levels[0] ?? "all_levels");
  const [language, setLanguage] = useState<string>(
    p?.language ?? (languages.includes(uiLocale) ? uiLocale : (languages[0] ?? "el")),
  );

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    onSubmit({ student_id: null, level, language });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <p className="text-sm font-medium">{t("steps.who.heading")}</p>

      <fieldset disabled={submitting} className="flex flex-col gap-3">
        <div className="flex flex-col gap-1.5" data-testid="interview-who-levels">
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("steps.who.heading")}>
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

        <div className="flex flex-col gap-1.5" data-testid="interview-who-languages">
          <span className="text-xs text-muted-foreground">{t("steps.who.languageHint")}</span>
          <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("steps.who.languageHint")}>
            {languages.map((lang) => (
              <Button
                key={lang}
                type="button"
                size="sm"
                variant={language === lang ? "default" : "outline"}
                data-testid={`interview-who-language-${lang}`}
                aria-pressed={language === lang}
                onClick={() => setLanguage(lang)}
              >
                {t.has(`steps.who.languages.${lang}`) ? t(`steps.who.languages.${lang}`) : lang}
              </Button>
            ))}
          </div>
        </div>
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
