"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { Settings } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { InterviewOption } from "@/lib/api";
import { cn } from "@/lib/utils";

interface InterviewScopeStepProps {
  /** The gap policies the API accepts (`library_only` / `general_knowledge`, and
   * `web` once Tier 3 ships) — from `describe_step`, never invented here. */
  options: InterviewOption[];
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** THE COURSE BRIEF — in his own words — AND THE GAP POLICY. This step is what
 * `domain` was pretending to be.
 *
 * Chris asked: "is domain playing any role? is it used somewhere or only for
 * tagging?" The honest answer was that a 30-character tag was one line in one
 * prompt and a retrieval filter that could no longer filter anything. It never
 * tagged a curriculum and never rendered anywhere. So it is gone, and in its place
 * he says what the course is FOR, in a sentence or two — and unlike `domain`, this
 * reaches the outline call AND every one of the twenty lesson-draft prompts.
 *
 * The gap policy is the OTHER half of the question he actually asked ("what happens
 * with the ones saying nothing in your library?"): he decides, up front, whether a
 * topic his library does not cover gets filled from Claude's general knowledge —
 * labelled — or left as an honest, visible gap.
 */
export function InterviewScopeStep({ options, submitting, error, onSubmit }: InterviewScopeStepProps) {
  const t = useTranslations("curricula.interview");
  const locale = useLocale();

  const [brief, setBrief] = useState("");
  const [policy, setPolicy] = useState(options[0]?.value ?? "general_knowledge");

  const canSubmit = brief.trim().length > 0;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    onSubmit({ brief: brief.trim(), gap_policy: policy });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-medium">{t("steps.scope.heading")}</p>
        <p className="text-xs text-muted-foreground">{t("steps.scope.hint")}</p>
      </div>

      <fieldset disabled={submitting} className="flex min-w-0 flex-col gap-3">
        <Textarea
          rows={4}
          value={brief}
          onChange={(e) => setBrief(e.target.value)}
          placeholder={t("steps.scope.briefPlaceholder")}
          data-testid="interview-scope-brief"
          required
        />

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium">{t("steps.scope.policyHeading")}</span>
          {options.map((opt) => (
            <label
              key={opt.value}
              data-testid={`interview-policy-row-${opt.value}`}
              className={cn(
                "flex min-w-0 cursor-pointer items-start gap-2.5 rounded-xl border border-border bg-card px-3 py-2 text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/40",
                policy === opt.value && "border-primary/50 ring-primary/30",
              )}
            >
              <input
                type="radio"
                name="gap-policy"
                value={opt.value}
                checked={policy === opt.value}
                onChange={() => setPolicy(opt.value)}
                data-testid={`interview-policy-${opt.value}`}
                className="mt-0.5 size-4 shrink-0 accent-primary"
              />
              <span className="min-w-0">
                {t.has(`steps.scope.policies.${opt.value}`)
                  ? t(`steps.scope.policies.${opt.value}`)
                  : opt.label}
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      {error && (
        <p role="alert" data-testid="interview-step-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* THE STEP-3 DEEP-LINK (Plan C, Task 7). A plain navigation, not a form
            control — it must never submit this step's answer, it just opens
            Settings on the "Curriculum" prompt group so he can see (or change)
            what will actually write this course, before he commits to it. */}
        <Link
          href={`/${locale}/settings?promptGroup=curriculum`}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="interview-scope-prompts-link"
          className="flex items-center gap-1.5 text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          <Settings className="size-3.5" />
          {t("steps.scope.promptsLink")}
        </Link>

        <Button
          type="submit"
          disabled={submitting || !canSubmit}
          data-testid="interview-answer-submit"
        >
          {t("continue")}
        </Button>
      </div>
    </form>
  );
}
