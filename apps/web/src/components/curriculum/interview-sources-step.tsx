"use client";

import { useMemo, useState, type FormEvent } from "react";
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
  /** `InterviewStateOut.prior` — set when navigating back; his earlier
   * selection wins over `default_selected` seeding, so returning to this step
   * never silently re-adds a source he deselected (or vice versa). */
  prior?: unknown;
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
export function InterviewSourcesStep({ options, shape, prior, submitting, error, onSubmit }: InterviewSourcesStepProps) {
  const t = useTranslations("curricula.interview");
  const p = (prior ?? null) as { source_ids?: string[] } | null;
  const [selected, setSelected] = useState<Set<string>>(
    () =>
      new Set(
        Array.isArray(p?.source_ids)
          ? p.source_ids
          : options.filter((o) => o.default_selected).map((o) => o.value),
      ),
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

  // Live "what will the model actually do with this selection" hint. The rough
  // chars/4 estimate is client-side only; the API re-counts server-side and
  // routes in corpus.build_mixed_context: raw ≤ full_context_budget (600K,
  // app/config.py) is read WHOLE — the 300K canon_threshold picks a
  // representation server-side, not a different promise — and only ABOVE 600K
  // does the canon carry the compiled books (uncompiled ride verbatim), with
  // retrieval strictly the last rung, when there is no canon to lean on.
  // 2026-07-22: this line used to re-derive the ladder wrong (>600K claimed
  // "retrieval") — the tutor picked 603K of books, read the warning, and the
  // job then built an 87K canon context and read it whole. The web must not
  // out-guess corpus.py; it mirrors the two facts it can know: does the raw
  // sum fit, and is there any canon among the selection.
  const estTokens = useMemo(
    () =>
      Math.round(
        options.reduce((sum, o) => (selected.has(o.value) ? sum + (o.char_count ?? 0) : sum), 0) / 4,
      ),
    [options, selected],
  );
  const anyCompiledSelected = useMemo(
    () => options.some((o) => selected.has(o.value) && o.compiled === true),
    [options, selected],
  );
  const regime = estTokens <= 600_000 ? "whole" : anyCompiledSelected ? "canon" : "search";

  // Above the whole-read threshold the canon carries the compiled sources and
  // anything uncompiled rides along verbatim (or falls to retrieval) — worth a
  // heads-up naming them, where the old flow failed the job after the fact.
  const uncompiledSelected = useMemo(
    () => options.filter((o) => selected.has(o.value) && o.compiled === false),
    [options, selected],
  );

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div>
        <p className="text-sm font-medium">{t("steps.sources.heading")}</p>
        <p className="text-xs text-muted-foreground">{t("steps.sources.hint")}</p>
      </div>

      {selected.size > 0 && (
        <p
          data-testid="sources-regime"
          className="rounded-xl border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
        >
          {regime === "whole" && t("steps.sources.regimeWhole", { tokens: estTokens })}
          {regime === "canon" && t("steps.sources.regimeCanon", { tokens: estTokens })}
          {regime === "search" && t("steps.sources.regimeSearch", { tokens: estTokens })}
        </p>
      )}

      {regime !== "whole" && uncompiledSelected.length > 0 && (
        <p
          data-testid="sources-uncompiled-hint"
          className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400"
        >
          {t("steps.sources.uncompiledHint", {
            count: uncompiledSelected.length,
            titles: uncompiledSelected.map((o) => o.label).join(", "),
          })}
        </p>
      )}

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
            {opt.compiled !== undefined && (
              <Badge
                variant="outline"
                data-testid={`interview-source-canon-${opt.value}`}
                className={cn(
                  "shrink-0",
                  opt.compiled
                    ? "border-emerald-500/40 text-emerald-600 dark:text-emerald-400"
                    : "border-amber-500/40 text-amber-600 dark:text-amber-400",
                )}
              >
                {opt.compiled ? t("steps.sources.canonReady") : t("steps.sources.canonMissing")}
              </Badge>
            )}
            <span
              // Hidden on phones: three shrink-0 chips on one card row left the
              // title a word-per-line sliver. The type and canon badges are the
              // ones a grounding decision needs; the char count is desktop detail.
              className="hidden shrink-0 text-xs text-muted-foreground sm:block"
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
