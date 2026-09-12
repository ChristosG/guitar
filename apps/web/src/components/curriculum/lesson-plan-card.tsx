"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { LessonPlan } from "@/lib/api";

export interface LessonPlanCardProps {
  plan: LessonPlan;
  /** «Δεν μου αρέσει, ξανακάνε το» — replans with an extra note appended to
   * the original instruction. The panel keeps THIS card on screen under the
   * status line until the new plan lands. */
  onReplan: (note: string) => void;
  /** The ticked sections, plus whatever he added in the card's note box. */
  onApply: (picks: { section: string; brief: string }[], note: string) => void;
  /** True while a plan or an apply job is in flight. Both buttons go dead —
   * the card itself stays editable, because a failed apply comes back HERE and
   * his ticks, briefs and note must still be where he left them. */
  busy: boolean;
}

/** THE PLAN AS A PROPOSAL, NOT A VERDICT.
 *
 * The planner's answer arrives as one row per section with `keep`/`rewrite` and
 * a one-line reason. This card turns that into something he edits: every row is
 * a tick box he can undo, every ticked row carries a brief he can rewrite, and
 * nothing leaves the browser until «Εφαρμογή». The gate matters because the
 * thing on the other side is destructive — an apply replaces the ticked
 * sections' text outright (with a `prev_segments` snapshot behind it, but still
 * a rewrite of prose he has read and curated).
 *
 * WHAT THE APPLY SENDS IS WHAT IS TICKED *NOW*, never the planner's original
 * verdicts. That is the whole reason `checked` is state here rather than read
 * off `plan.sections` at submit time.
 *
 * TUTOR-EDITED SECTIONS START UNTICKED even if the planner said `rewrite`.
 * A section carrying `tutor_edited` is prose HE wrote; the default must never
 * be "throw it away". It stays tickable — he is allowed to say "yes, redo my
 * own paragraph too" — but he has to say it.
 *
 * NO `useEffect` RESET. The tutor's choices are initialised from `plan` once,
 * and a NEW plan (a re-plan) arrives as a REMOUNT: the panel bumps a counter
 * into this component's `key`. That is React's own answer to "reset all state
 * when a prop changes", and it cannot drift the way a reset effect can — an
 * effect would briefly paint the new plan's rows under the old plan's ticks.
 */
export function LessonPlanCard({ plan, onReplan, onApply, busy }: LessonPlanCardProps) {
  const t = useTranslations("curricula.lessonAi.card");

  const [checked, setChecked] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      plan.sections.map((s) => [s.section, s.action === "rewrite" && !s.tutor_edited]),
    ),
  );
  const [briefs, setBriefs] = useState<Record<string, string>>(() =>
    Object.fromEntries(plan.sections.map((s) => [s.section, s.brief])),
  );
  const [note, setNote] = useState("");

  // Built in the plan's own order, so the POST body reads like the lesson.
  const picks = plan.sections
    .filter((s) => checked[s.section])
    .map((s) => ({ section: s.section, brief: briefs[s.section] ?? "" }));

  return (
    <div data-testid="lesson-plan-card" className="flex flex-col gap-3 rounded-lg border border-border p-3">
      <p className="text-sm">{plan.summary}</p>

      {/* The planner talking back — "I left the exercises alone, as you asked".
          Worth its own colour: it is the one line that explains a plan that
          looks smaller than he expected. */}
      {plan.note_to_tutor && (
        <p className="text-sm text-amber-700 dark:text-amber-400">{plan.note_to_tutor}</p>
      )}

      {/* What the safety net threw away — sections the planner asked for that
          this lesson does not have. A silently shrunken plan is worse than one
          that admits what it lost. */}
      {plan.dropped.length > 0 && (
        <p data-testid="lesson-plan-dropped" className="text-xs text-amber-700 dark:text-amber-400">
          {t("dropped", { count: plan.dropped.length })}
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {plan.sections.map((s) => (
          <li key={s.section} data-testid={`lesson-plan-row-${s.section}`} className="flex flex-col gap-1">
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                data-testid={`lesson-plan-check-${s.section}`}
                checked={!!checked[s.section]}
                onChange={(e) => {
                  const next = e.target.checked;
                  setChecked((c) => ({ ...c, [s.section]: next }));
                }}
                className="mt-1 shrink-0"
              />
              <span className="flex min-w-0 flex-col">
                <span className="font-medium">
                  {s.title}
                  {s.tutor_edited && (
                    <Badge variant="outline" className="ml-2">
                      {t("yours")}
                    </Badge>
                  )}
                </span>
                {s.reason && <span className="text-xs text-muted-foreground">{s.reason}</span>}
              </span>
            </label>
            {/* The brief belongs to a section he is actually rewriting. Shown
                only when ticked, so an untouched section has no box to argue
                with — and so the ticked ones read as a to-do list. */}
            {checked[s.section] && (
              <Input
                data-testid={`lesson-plan-brief-${s.section}`}
                value={briefs[s.section] ?? ""}
                placeholder={t("briefPlaceholder")}
                onChange={(e) => {
                  const next = e.target.value;
                  setBriefs((b) => ({ ...b, [s.section]: next }));
                }}
                className="ml-6 text-xs"
              />
            )}
          </li>
        ))}
      </ul>

      {/* Counts HIS ticks, not the planner's `impact.rewrite_count` — the
          number has to move the moment he unticks something, or it is a
          promise about a different plan than the one he is about to send. */}
      <p data-testid="lesson-plan-impact" className="text-xs text-muted-foreground">
        {t("impact", { count: picks.length })}
      </p>

      <Textarea
        data-testid="lesson-plan-note"
        rows={2}
        value={note}
        placeholder={t("notePlaceholder")}
        onChange={(e) => setNote(e.target.value)}
      />

      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          data-testid="lesson-ai-replan"
          disabled={busy}
          onClick={() => onReplan(note)}
        >
          {t("replan")}
        </Button>
        <Button
          type="button"
          data-testid="lesson-ai-apply"
          disabled={busy || picks.length === 0}
          onClick={() => onApply(picks, note)}
        >
          {t("apply")}
        </Button>
      </div>

      {/* Why «Εφαρμογή» is dead. A disabled button with no sentence next to it
          reads as a broken page. */}
      {picks.length === 0 && <p className="text-xs text-muted-foreground">{t("nothingTicked")}</p>}
    </div>
  );
}
