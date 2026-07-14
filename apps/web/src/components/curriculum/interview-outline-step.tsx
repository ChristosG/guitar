"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  ChevronDown,
  ChevronUp,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { TierBadge } from "@/components/curriculum/tier-badge";
import { outlineTotals, targetWords } from "@/components/curriculum/outline-cost";
import type { Outline, OutlineModule, Tier } from "@/lib/api";
import { cn } from "@/lib/utils";

/** The tiers the tutor may CHOOSE. `gap` is not among them — a gap is something
 * the model reports, never something he asks for; if a module already is one
 * (his gap policy is library-only and his sources genuinely do not cover it) the
 * select shows it, so he can move it OFF the gap, but he cannot put a module on
 * one. */
const CHOOSABLE_TIERS: Tier[] = ["library", "general_knowledge", "web"];

interface InterviewOutlineStepProps {
  outline: Outline;
  submitting: boolean;
  error: string | null | undefined;
  /** `{outline}` -> accept the EDITED outline and move to confirm.
   * `{regenerate: true}` -> throw this one away and ask the model again. */
  onSubmit: (answer: unknown) => void;
}

/** THE OUTLINE EDITOR. The whole point of this stage.
 *
 * Chris, verbatim: "i need more user engagement here. the man might want to change
 * something. might need to extend a module, delete one or add one more. we have
 * NOTHING of those bro."
 *
 * It sits AFTER the plan and BEFORE the expensive draft, and that position is the
 * entire argument for it: right here, deleting a module costs nothing and adding
 * one costs nothing. Twenty minutes and $2.72 later, both cost a regeneration. So
 * this is where he gets to work on his course instead of accepting or rejecting
 * somebody else's.
 *
 * WHATEVER HE SENDS BACK IS WHAT GETS BUILT. `_answer_outline` stores this exact
 * dict on the interview and `materialize_outline` persists THAT — the model's
 * original is not kept anywhere, and is not supposed to be.
 *
 * Reorder is up/down buttons, not drag-and-drop. He is reordering five modules,
 * not a thousand rows; two buttons and a swap beat a drag library, its touch
 * targets and its autoscroll, and they work on a keyboard for free.
 *
 * The footer is live and it is in dollars, because the next click spends them.
 */
export function InterviewOutlineStep({
  outline,
  submitting,
  error,
  onSubmit,
}: InterviewOutlineStepProps) {
  const t = useTranslations("curricula.outline");
  const confirm = useConfirm();

  // A local deep copy. The tutor's edits are HIS until he presses Continue —
  // nothing is sent per keystroke, and a regenerate throws this whole thing away.
  const [draft, setDraft] = useState<Outline>(() => structuredClone(outline));

  const totals = outlineTotals(draft.modules);

  function editModule(mi: number, patch: Partial<OutlineModule>) {
    setDraft((prev) => {
      const modules = [...prev.modules];
      modules[mi] = { ...modules[mi], ...patch };
      return { ...prev, modules };
    });
  }

  function editLesson(mi: number, li: number, patch: Partial<Outline["modules"][0]["lessons"][0]>) {
    setDraft((prev) => {
      const modules = [...prev.modules];
      const lessons = [...modules[mi].lessons];
      lessons[li] = { ...lessons[li], ...patch };
      modules[mi] = { ...modules[mi], lessons };
      return { ...prev, modules };
    });
  }

  function move<T>(list: T[], i: number, dir: -1 | 1): T[] {
    const j = i + dir;
    if (j < 0 || j >= list.length) return list; // at the end: a no-op, not an error
    const next = [...list];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  }

  function moveModule(mi: number, dir: -1 | 1) {
    setDraft((prev) => ({ ...prev, modules: move(prev.modules, mi, dir) }));
  }

  function moveLesson(mi: number, li: number, dir: -1 | 1) {
    setDraft((prev) => {
      const modules = [...prev.modules];
      modules[mi] = { ...modules[mi], lessons: move(modules[mi].lessons, li, dir) };
      return { ...prev, modules };
    });
  }

  async function deleteModule(mi: number) {
    // `mod`, not `module`: `module` is a reserved-ish identifier in a Next client
    // bundle (@next/next/no-assign-module-variable) and shadowing it is an error.
    const mod = draft.modules[mi];
    const ok = await confirm({
      title: t("confirmDeleteModule.title", { title: mod.title }),
      body: t("confirmDeleteModule.body", { count: mod.lessons.length }),
      confirmLabel: t("confirmDeleteModule.confirm"),
      destructive: true,
    });
    if (!ok) return;
    setDraft((prev) => ({ ...prev, modules: prev.modules.filter((_, i) => i !== mi) }));
  }

  async function deleteLesson(mi: number, li: number) {
    const lesson = draft.modules[mi].lessons[li];
    const ok = await confirm({
      title: t("confirmDeleteLesson.title", { title: lesson.title }),
      confirmLabel: t("confirmDeleteLesson.confirm"),
      destructive: true,
    });
    if (!ok) return;
    setDraft((prev) => {
      const modules = [...prev.modules];
      modules[mi] = { ...modules[mi], lessons: modules[mi].lessons.filter((_, i) => i !== li) };
      return { ...prev, modules };
    });
  }

  function addModule() {
    setDraft((prev) => ({
      ...prev,
      modules: [
        ...prev.modules,
        {
          title: t("newModuleTitle"),
          objective: "",
          // A module HE invented is not in his library until the model says it is,
          // and it has not read this one. `general_knowledge` is the honest floor;
          // he can move it to `library` or `web` in the select right there.
          tier: "general_knowledge" as Tier,
          coverage_note: "",
          lessons: [],
        },
      ],
    }));
  }

  function addLesson(mi: number) {
    setDraft((prev) => {
      const modules = [...prev.modules];
      // Inherit the minutes of the lesson next to it — a course of 50-minute
      // lessons plus one 40-minute one, because a constant was easier, is not a
      // thing he asked for.
      const minutes = modules[mi].lessons.at(-1)?.est_minutes ?? 50;
      modules[mi] = {
        ...modules[mi],
        lessons: [
          ...modules[mi].lessons,
          { title: t("newLessonTitle"), objective: "", est_minutes: minutes },
        ],
      };
      return { ...prev, modules };
    });
  }

  const canSubmit = draft.modules.length > 0 && !submitting;

  return (
    <div className="flex flex-col gap-3" data-testid="outline-editor">
      <div>
        <p className="text-sm font-medium">{t("heading")}</p>
        <p className="text-xs text-muted-foreground">{t("hint")}</p>
      </div>

      <div className="flex max-h-[46vh] flex-col gap-3 overflow-y-auto pr-1">
        {/* `mod`, not `module` — see `deleteModule` above. */}
        {draft.modules.map((mod, mi) => (
          <article
            key={mi}
            data-testid="outline-module"
            data-title={mod.title}
            className="flex min-w-0 flex-col gap-2.5 rounded-2xl border border-border bg-card p-3 ring-1 ring-foreground/5"
          >
            <div className="flex min-w-0 items-start gap-2">
              <div className="flex shrink-0 flex-col">
                <Button
                  type="button" variant="ghost" size="icon-sm"
                  data-testid={`outline-module-up-${mi}`}
                  aria-label={t("moveUp")}
                  disabled={mi === 0}
                  onClick={() => moveModule(mi, -1)}
                >
                  <ChevronUp />
                </Button>
                <Button
                  type="button" variant="ghost" size="icon-sm"
                  data-testid={`outline-module-down-${mi}`}
                  aria-label={t("moveDown")}
                  disabled={mi === draft.modules.length - 1}
                  onClick={() => moveModule(mi, 1)}
                >
                  <ChevronDown />
                </Button>
              </div>

              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <Input
                  value={mod.title}
                  data-testid={`outline-module-title-${mi}`}
                  aria-label={t("moduleTitleLabel")}
                  onChange={(e) => editModule(mi, { title: e.target.value })}
                  className="h-8 font-medium"
                />
                <Input
                  value={mod.objective}
                  data-testid={`outline-module-objective-${mi}`}
                  aria-label={t("objectiveLabel")}
                  placeholder={t("objectivePlaceholder")}
                  onChange={(e) => editModule(mi, { objective: e.target.value })}
                  className="h-7 text-xs"
                />

                <div className="flex flex-wrap items-center gap-2">
                  {/* THE PER-MODULE TIER SELECT. "for THIS one, go search the web."
                      A plain <select>: this app has no Select component, and one
                      built for four options that must also render inside a scrolling
                      dialog is a popup-positioning bug waiting to happen. */}
                  <select
                    value={mod.tier}
                    data-testid={`outline-module-tier-${mi}`}
                    aria-label={t("tierLabel")}
                    onChange={(e) => editModule(mi, { tier: e.target.value as Tier })}
                    className="h-7 rounded-lg border border-border bg-background px-2 text-xs"
                  >
                    {CHOOSABLE_TIERS.map((tier) => (
                      <option key={tier} value={tier}>
                        {t(`tiers.${tier}`)}
                      </option>
                    ))}
                    {mod.tier === "gap" && <option value="gap">{t("tiers.gap")}</option>}
                  </select>
                  <TierBadge tier={mod.tier} />
                </div>

                {/* The GROUNDING PANEL: what the model found in his library for this
                    module, in its own words, having read the whole thing — or the
                    honest absence of it. Not a similarity score. */}
                {mod.coverage_note ? (
                  <p
                    data-testid={`outline-module-coverage-${mi}`}
                    className="rounded-lg bg-muted/60 px-2.5 py-1.5 text-xs text-muted-foreground"
                  >
                    {mod.coverage_note}
                  </p>
                ) : null}
                {mod.tier === "gap" && (
                  <p
                    data-testid={`outline-module-gap-${mi}`}
                    className="text-xs font-medium text-amber-600 dark:text-amber-400"
                  >
                    {t("gapWarning")}
                  </p>
                )}
              </div>

              <Button
                type="button" variant="ghost" size="icon-sm"
                data-testid={`outline-module-delete-${mi}`}
                aria-label={t("deleteModule")}
                onClick={() => deleteModule(mi)}
                className="shrink-0"
              >
                <Trash2 />
              </Button>
            </div>

            <ul className="flex flex-col gap-1.5 border-l border-border pl-3">
              {mod.lessons.map((lesson, li) => (
                <li key={li} data-testid="outline-lesson" className="flex min-w-0 items-center gap-1.5">
                  <div className="flex shrink-0 items-center">
                    <Button
                      type="button" variant="ghost" size="icon-sm"
                      data-testid={`outline-lesson-up-${mi}-${li}`}
                      aria-label={t("moveUp")}
                      disabled={li === 0}
                      onClick={() => moveLesson(mi, li, -1)}
                    >
                      <ChevronUp />
                    </Button>
                    <Button
                      type="button" variant="ghost" size="icon-sm"
                      data-testid={`outline-lesson-down-${mi}-${li}`}
                      aria-label={t("moveDown")}
                      disabled={li === mod.lessons.length - 1}
                      onClick={() => moveLesson(mi, li, 1)}
                    >
                      <ChevronDown />
                    </Button>
                  </div>

                  <Input
                    value={lesson.title}
                    data-testid={`outline-lesson-title-${mi}-${li}`}
                    aria-label={t("lessonTitleLabel")}
                    onChange={(e) => editLesson(mi, li, { title: e.target.value })}
                    className="h-7 min-w-0 flex-1 text-sm"
                  />

                  <div className="flex shrink-0 items-center gap-1">
                    <Input
                      type="number"
                      min={5}
                      value={lesson.est_minutes}
                      data-testid={`outline-lesson-minutes-${mi}-${li}`}
                      aria-label={t("minutesLabel")}
                      onChange={(e) =>
                        editLesson(mi, li, { est_minutes: Math.max(0, Number(e.target.value) || 0) })
                      }
                      className="h-7 w-16 text-xs"
                    />
                    <span className="w-20 text-right text-xs text-muted-foreground tabular-nums">
                      {t("lessonWords", { words: targetWords(lesson.est_minutes) })}
                    </span>
                  </div>

                  <Button
                    type="button" variant="ghost" size="icon-sm"
                    data-testid={`outline-lesson-delete-${mi}-${li}`}
                    aria-label={t("deleteLesson")}
                    onClick={() => deleteLesson(mi, li)}
                    className="shrink-0"
                  >
                    <Trash2 />
                  </Button>
                </li>
              ))}

              <li>
                <Button
                  type="button" variant="ghost" size="sm"
                  data-testid={`outline-add-lesson-${mi}`}
                  onClick={() => addLesson(mi)}
                >
                  <Plus />
                  {t("addLesson")}
                </Button>
              </li>
            </ul>
          </article>
        ))}

        <Button type="button" variant="outline" size="sm" data-testid="outline-add-module" onClick={addModule}>
          <Plus />
          {t("addModule")}
        </Button>
      </div>

      {/* THE LIVE FOOTER. It moves while he types, and the last number is money. */}
      <div
        data-testid="outline-footer"
        className={cn(
          "flex flex-wrap items-center gap-x-2 gap-y-1 rounded-xl border border-border bg-muted/40 px-3 py-2",
          "text-xs text-muted-foreground tabular-nums",
        )}
      >
        <span data-testid="outline-footer-summary">
          {t("footer", {
            modules: totals.modules,
            lessons: totals.lessons,
            words: totals.words,
            cost: totals.costUsd.toFixed(2),
          })}
        </span>
      </div>

      {error && (
        <p role="alert" data-testid="interview-step-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button
          type="button"
          variant="outline"
          disabled={submitting}
          data-testid="outline-regenerate"
          onClick={() => onSubmit({ regenerate: true })}
        >
          {submitting ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          {t("regenerate")}
        </Button>
        <Button
          type="button"
          disabled={!canSubmit}
          data-testid="interview-answer-submit"
          onClick={() => onSubmit({ outline: draft })}
        >
          {submitting && <Loader2 className="animate-spin" />}
          {t("accept")}
        </Button>
      </div>
    </div>
  );
}
