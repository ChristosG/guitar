"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { InterviewOption, InterviewShape } from "@/lib/api";
import { cn } from "@/lib/utils";

interface InterviewSourcesStepProps {
  options: InterviewOption[];
  /** Arithmetic, not a suggestion in a prompt: 20 weeks x 50 min really does
   * produce 5 modules of 4 lessons, and he agrees to that size before we spend
   * anything. (It used to produce 4 modules for 20 weeks, because the count was
   * a sentence in a prompt the model was free to ignore.) */
  shape?: InterviewShape;
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

/** Third step (`STEP_ORDER[2]`) — THE step Chris's brief calls load-bearing:
 * every library source, shown with type + char_count so the tutor can tell
 * real material from a synthetic filler source at a glance and deselect it
 * in one click (his library's real "Guitar Tone & Gear — Course Spine"
 * out-scores his real 77-page book in retrieval today — see
 * `app.curriculum.interview`'s own module docstring). `default_selected`
 * (sources at/above `LEN_FLOOR`) only seeds the initial checkbox state —
 * nothing is silently excluded, and the tutor's own choice always wins;
 * `[]` (deselect everything) is a valid, deliberate answer the API itself
 * accepts (`_answer_sources`'s own docstring). */
export function InterviewSourcesStep({ options, shape, submitting, error, onSubmit }: InterviewSourcesStepProps) {
  const t = useTranslations("curricula.interview");
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(options.filter((o) => o.default_selected).map((o) => o.value)),
  );

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    onSubmit({ source_ids: Array.from(selected) });
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-medium">{t("steps.sources.heading")}</p>
        <p className="text-xs text-muted-foreground">{t("steps.sources.hint")}</p>
      </div>

      {shape && (
        <p
          data-testid="interview-shape-echo"
          className="rounded-xl border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          {t("steps.sources.shape", {
            lessons: shape.lessons_total,
            modules: shape.modules,
            perModule: shape.lessons_per_module.join("+"),
            words: shape.target_words_per_lesson,
            taught: shape.teaching_minutes,
            qa: shape.qa_minutes,
          })}
        </p>
      )}

      {/* `min-w-0` on the fieldset is load-bearing and was missing: a <fieldset>
          is a flex ITEM here, and a flex item's default `min-width: auto`
          refuses to shrink below its widest child — a source titled with a
          120-character URL therefore pushed this whole step wider than the
          dialog and painted outside the card. With `min-w-0` the fieldset can
          shrink, which is what lets the `line-clamp-2` title below actually
          clamp instead of merely being asked to. */}
      <fieldset disabled={submitting} className="flex max-h-72 min-w-0 flex-col gap-1.5 overflow-y-auto pr-1">
        {options.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="interview-sources-empty">
            {t("steps.sources.empty")}
          </p>
        )}
        {options.map((opt) => (
          <label
            key={opt.value}
            data-testid={`interview-source-row-${opt.value}`}
            className={cn(
              "flex cursor-pointer items-center gap-3 rounded-xl border border-border bg-card px-3 py-2 text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/40",
              selected.has(opt.value) && "border-primary/50 ring-primary/30",
            )}
          >
            <input
              type="checkbox"
              data-testid={`interview-source-${opt.value}`}
              checked={selected.has(opt.value)}
              onChange={() => toggle(opt.value)}
              className="size-4 shrink-0 accent-primary"
            />
            {/* Two clamped lines + a tooltip with the FULL title. Chris,
                verbatim: "some long links are clipped so we need a mouseover
                tooltip to get the full title." A tutor deciding whether to
                ground a curriculum in a source cannot make that call from
                "Guitar Tone & Gear — Course Spi…". */}
            <Tooltip>
              <TooltipTrigger
                render={<span className="min-w-0 flex-1 line-clamp-2 font-medium break-words" />}
                data-testid={`interview-source-label-${opt.value}`}
              >
                {opt.label}
              </TooltipTrigger>
              <TooltipContent>{opt.label}</TooltipContent>
            </Tooltip>
            {opt.type && (
              <Badge variant="outline" className="shrink-0">
                {opt.type}
              </Badge>
            )}
            <span
              className="shrink-0 text-xs text-muted-foreground"
              data-testid={`interview-source-charcount-${opt.value}`}
            >
              {t("steps.sources.charCount", { count: opt.char_count ?? 0 })}
            </span>
          </label>
        ))}
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
