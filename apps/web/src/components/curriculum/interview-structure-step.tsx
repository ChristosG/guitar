"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { BlueprintEditor } from "@/components/settings/blueprint-editor";
import type { BlueprintShape } from "@/lib/api";

interface InterviewStructureStepProps {
  /** `findings.blueprint` — `resolve_default_blueprint(db)`, the tutor's saved
   * SETTINGS default if he has one, else the code default. Always present in
   * practice (`describe_step`'s "structure" branch always sends it), but typed
   * optional because `InterviewFindings` is a shared, all-optional shape. */
  blueprint: BlueprintShape | undefined;
  /** `InterviewStateOut.prior` — set when navigating back. His EDITED blueprint
   * (if he pressed Continue rather than Skip) wins over the settings default,
   * so returning to this step never silently discards his section edits. */
  prior?: unknown;
  /** `findings.minutes_per_lesson` — the lesson length booked at the duration
   * step, so the editor previews each section's share in real minutes. */
  minutesPerLesson?: number | null;
  submitting: boolean;
  error: string | null | undefined;
  onSubmit: (answer: unknown) => void;
}

const EMPTY_BLUEPRINT: BlueprintShape = { version: 1, sections: [] };

/** THE OPTIONAL, SKIPPABLE "LESSON STRUCTURE" STEP (Plan C, Task 5). A thin
 * wrapper around the shared `BlueprintEditor` — the exact same editor Settings
 * mounts against `/blueprint/default` (`blueprint-default-card.tsx`) — plus two
 * buttons instead of one.
 *
 * SKIP IS A FIRST-CLASS ANSWER, not a lesser one (spec invariant #7): the editor
 * is PRE-FILLED with the settings default the moment this step renders, so a
 * tutor who has never touched a blueprint in his life sees a working structure
 * immediately and can just press Continue-as-is — Skip exists for "I looked, I
 * have nothing to add", and sends `{skip: true}` rather than re-submitting the
 * very thing that was already going to be used. `_answer_confirm` (Task 3) reads
 * BOTH as "no custom blueprint" identically: either way the settings default
 * ends up on `course.meta["blueprint"]`. Only "Use this structure" records
 * anything in `interview.answers.structure`.
 *
 * Placed between "scope" and "sources" in the wizard (Resolved design call #1) —
 * it shapes lesson DRAFTING, not the outline, so it must not sit between
 * "sources" and "outline" where it would disturb that step's existing
 * auto-regenerate handoff.
 */
export function InterviewStructureStep({
  blueprint,
  prior,
  minutesPerLesson,
  submitting,
  error,
  onSubmit,
}: InterviewStructureStepProps) {
  const t = useTranslations("curricula.interview");

  // A local, deep-copied draft — his edits are his own until he presses
  // Continue, same posture as `InterviewOutlineStep`'s `draft`. A blueprint he
  // already SUBMITTED (navigating back) wins over the settings default.
  const p = (prior ?? null) as { skip?: boolean; blueprint?: BlueprintShape } | null;
  const [draft, setDraft] = useState<BlueprintShape>(() =>
    structuredClone(p?.blueprint ?? blueprint ?? EMPTY_BLUEPRINT),
  );

  return (
    <div className="flex flex-col gap-3" data-testid="interview-structure-step">
      <div>
        <p className="text-sm font-medium">{t("steps.structure.heading")}</p>
        <p className="text-xs text-muted-foreground">{t("steps.structure.hint")}</p>
      </div>

      <fieldset disabled={submitting} className="flex min-w-0 flex-col gap-3">
        <BlueprintEditor value={draft} onChange={setDraft} minutesPerLesson={minutesPerLesson} />
      </fieldset>

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
          data-testid="interview-structure-skip"
          onClick={() => onSubmit({ skip: true })}
        >
          {t("steps.structure.skip")}
        </Button>
        <Button
          type="button"
          disabled={submitting}
          data-testid="interview-answer-submit"
          onClick={() => onSubmit({ blueprint: draft })}
        >
          {t("steps.structure.continue")}
        </Button>
      </div>
    </div>
  );
}
