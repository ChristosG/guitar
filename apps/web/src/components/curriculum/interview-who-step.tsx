"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface InterviewWhoStepProps {
  options: { value: string; label: string }[];
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** The interview's first step (`app.curriculum.interview.STEP_ORDER[0]`):
 * pick a real student off the roster as a one-tap option, or name someone
 * not yet on it. Mirrors `students/progress-row.tsx`'s "button-row
 * radiogroup" pattern for the roster picks (this app has no dedicated
 * radio-input component — see that file's own docstring for why). */
export function InterviewWhoStep({ options, submitting, error, onSubmit }: InterviewWhoStepProps) {
  const t = useTranslations("curricula.interview");

  const [mode, setMode] = useState<"existing" | "new">(options.length > 0 ? "existing" : "new");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [level, setLevel] = useState("");
  const [language, setLanguage] = useState("");

  const canSubmit = mode === "existing" ? Boolean(selectedId) : name.trim().length > 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    if (mode === "existing") {
      onSubmit({ student_id: selectedId });
    } else {
      onSubmit({
        name: name.trim(),
        level: level.trim() || undefined,
        language: language.trim() || undefined,
      });
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <p className="text-sm font-medium">{t("steps.who.heading")}</p>

      <fieldset disabled={submitting} className="flex flex-col gap-3">
        {/* The "someone new" toggle is ALWAYS rendered, even with an empty
            roster (`options.length === 0` — a brand-new tutor with no
            students yet) — it's the only way to reach the name/level/
            language form once the tutor edits those fields and this step
            re-renders with `mode` still "new" but a non-empty draft. */}
        <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label={t("steps.who.heading")}>
          {options.map((opt) => (
            <Button
              key={opt.value}
              type="button"
              size="sm"
              variant={mode === "existing" && selectedId === opt.value ? "default" : "outline"}
              data-testid={`interview-who-option-${opt.value}`}
              aria-pressed={mode === "existing" && selectedId === opt.value}
              onClick={() => {
                setMode("existing");
                setSelectedId(opt.value);
              }}
            >
              {opt.label}
            </Button>
          ))}
          <Button
            type="button"
            size="sm"
            variant={mode === "new" ? "default" : "outline"}
            data-testid="interview-who-new-toggle"
            aria-pressed={mode === "new"}
            onClick={() => setMode("new")}
          >
            {t("steps.who.newStudentToggle")}
          </Button>
        </div>

        {mode === "new" && (
          <div className="flex flex-col gap-2 rounded-xl border border-border bg-card p-3 ring-1 ring-foreground/10">
            <Input
              data-testid="interview-who-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("steps.who.namePlaceholder")}
              required
            />
            <div className="grid grid-cols-2 gap-2">
              <Input
                data-testid="interview-who-level"
                value={level}
                onChange={(e) => setLevel(e.target.value)}
                placeholder={t("steps.who.levelPlaceholder")}
              />
              <Input
                data-testid="interview-who-language"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                placeholder={t("steps.who.languagePlaceholder")}
              />
            </div>
          </div>
        )}
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
