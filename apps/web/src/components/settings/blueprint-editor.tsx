"use client";

import { ChevronDown, ChevronUp, Lock, Plus, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { BlueprintSection, BlueprintShape } from "@/lib/api";
import { cn } from "@/lib/utils";

const AUDIENCES: BlueprintSection["audience"][] = ["teacher", "student", "both"];

interface BlueprintEditorProps {
  value: BlueprintShape;
  onChange: (next: BlueprintShape) => void;
}

/** THE SHARED BLUEPRINT EDITOR (Plan C, Task 6) — the lesson skeleton every
 * curriculum drafts from, editable. Mounted twice: as the Settings default
 * (`blueprint-default-card.tsx`, against `/blueprint/default`) and inside the
 * wizard's optional "structure" step (`interview-structure-step.tsx`, seeded
 * from `findings.blueprint` and folded into `interview.answers.structure` — see
 * that component's own docstring). One component, one set of rules, because a
 * tutor who learns the shape once in Settings must find the exact same controls
 * when he reaches for it mid-wizard.
 *
 * PURELY CONTROLLED: no fetch, no save button, no confirm dialog lives here —
 * every keystroke calls `onChange` with a brand-new `BlueprintShape` and the two
 * call sites own loading/saving/persisting. That split is what let the wizard
 * step and the settings card share this file without either one importing the
 * other's plumbing.
 *
 * THE LOCK IS THE WHOLE POINT (spec invariant #4). `exercises` (kind
 * `"exercises"`) and `qa_prompts` (kind `"qa"`) are STRUCTURED: the model's
 * guided-json schema for them is a fixed nested shape the app builds in code
 * (`depth._exercises_section`/`_qa_section`), keyed by their `kind`. Renaming one
 * or deleting it would silently break every future lesson draft with no error
 * until the model tries to fill a schema that no longer exists. So their `key`
 * and both label inputs are `disabled`, and there is no Remove button for them —
 * everything else (description, weight, audience, enabled, reorder) stays live,
 * because reweighting or disabling a structured section is a normal thing to
 * want (a tutor who never quizzes at the end can turn Q&A off).
 *
 * Only `prose` sections may be added, removed or renamed — `addSection` always
 * appends a fresh `kind: "prose"` row, and `removeSection` is only ever wired to
 * one.
 */
export function BlueprintEditor({ value, onChange }: BlueprintEditorProps) {
  const t = useTranslations("blueprint");

  function patchSection(i: number, patch: Partial<BlueprintSection>) {
    const sections = [...value.sections];
    sections[i] = { ...sections[i], ...patch };
    onChange({ ...value, sections });
  }

  function patchLabel(i: number, lang: "el" | "en", text: string) {
    const sections = [...value.sections];
    sections[i] = { ...sections[i], label: { ...sections[i].label, [lang]: text } };
    onChange({ ...value, sections });
  }

  function move(i: number, dir: -1 | 1) {
    const j = i + dir;
    if (j < 0 || j >= value.sections.length) return;
    const sections = [...value.sections];
    [sections[i], sections[j]] = [sections[j], sections[i]];
    onChange({ ...value, sections });
  }

  function removeSection(i: number) {
    onChange({ ...value, sections: value.sections.filter((_, idx) => idx !== i) });
  }

  function addSection() {
    onChange({
      ...value,
      sections: [
        ...value.sections,
        {
          key: "",
          label: { el: "", en: "" },
          description: "",
          weight: 0.1,
          kind: "prose",
          audience: "teacher",
          enabled: true,
        },
      ],
    });
  }

  return (
    <div className="flex flex-col gap-3" data-testid="blueprint-editor">
      <div className="flex flex-col gap-2.5">
        {value.sections.map((section, i) => {
          const locked = section.kind !== "prose";
          const rowId = section.key || `blank-${i}`;
          return (
            <article
              key={i}
              data-testid={`blueprint-section-${rowId}`}
              className={cn(
                "flex min-w-0 flex-col gap-2.5 rounded-2xl border border-border bg-card p-3 ring-1 ring-foreground/5",
                !section.enabled && "opacity-60",
              )}
            >
              <div className="flex min-w-0 items-start gap-2">
                <div className="flex shrink-0 flex-col">
                  <Button
                    type="button" variant="ghost" size="icon-sm"
                    data-testid={`blueprint-up-${rowId}`}
                    aria-label={t("moveUp")}
                    disabled={i === 0}
                    onClick={() => move(i, -1)}
                  >
                    <ChevronUp />
                  </Button>
                  <Button
                    type="button" variant="ghost" size="icon-sm"
                    data-testid={`blueprint-down-${rowId}`}
                    aria-label={t("moveDown")}
                    disabled={i === value.sections.length - 1}
                    onClick={() => move(i, 1)}
                  >
                    <ChevronDown />
                  </Button>
                </div>

                <div className="flex min-w-0 flex-1 flex-col gap-2">
                  {locked && (
                    <span
                      data-testid={`blueprint-locked-${rowId}`}
                      title={t("lockedWhy")}
                      className="flex w-fit items-center gap-1.5 rounded-md bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                    >
                      <Lock className="size-3 shrink-0" />
                      {t("lockedTitle")}
                    </span>
                  )}

                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("keyLabel")}</Label>
                      <Input
                        value={section.key}
                        disabled={locked}
                        data-testid={`blueprint-key-${rowId}`}
                        onChange={(e) => patchSection(i, { key: e.target.value })}
                        className="h-8 font-mono text-xs"
                      />
                    </div>
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("labelElLabel")}</Label>
                      <Input
                        value={section.label.el}
                        disabled={locked}
                        data-testid={`blueprint-label-el-${rowId}`}
                        onChange={(e) => patchLabel(i, "el", e.target.value)}
                        className="h-8"
                      />
                    </div>
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("labelEnLabel")}</Label>
                      <Input
                        value={section.label.en}
                        disabled={locked}
                        data-testid={`blueprint-label-en-${rowId}`}
                        onChange={(e) => patchLabel(i, "en", e.target.value)}
                        className="h-8"
                      />
                    </div>
                  </div>

                  <div className="flex flex-col gap-1">
                    <Label className="text-xs">{t("descriptionLabel")}</Label>
                    <Textarea
                      value={section.description}
                      data-testid={`blueprint-description-${rowId}`}
                      onChange={(e) => patchSection(i, { description: e.target.value })}
                      rows={2}
                      className="bg-background text-xs"
                    />
                  </div>

                  <div className="flex flex-wrap items-end gap-3">
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("weightLabel")}</Label>
                      <Input
                        type="number"
                        min={0}
                        max={1}
                        step={0.01}
                        value={section.weight}
                        data-testid={`blueprint-weight-${rowId}`}
                        onChange={(e) => patchSection(i, { weight: Number(e.target.value) })}
                        className="h-8 w-24"
                      />
                    </div>

                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("audienceLabel")}</Label>
                      {/* A plain <select>: this app has no Select component (see
                          `interview-outline-step.tsx`'s tier picker for the same
                          call), and three fixed options don't warrant one. */}
                      <select
                        value={section.audience}
                        data-testid={`blueprint-audience-${rowId}`}
                        onChange={(e) =>
                          patchSection(i, { audience: e.target.value as BlueprintSection["audience"] })
                        }
                        className="h-8 rounded-lg border border-border bg-background px-2 text-xs"
                      >
                        {AUDIENCES.map((a) => (
                          <option key={a} value={a}>
                            {t(`audience.${a}`)}
                          </option>
                        ))}
                      </select>
                    </div>

                    <label className="flex items-center gap-1.5 pb-1.5 text-xs">
                      <input
                        type="checkbox"
                        checked={section.enabled}
                        data-testid={`blueprint-enabled-${rowId}`}
                        onChange={(e) => patchSection(i, { enabled: e.target.checked })}
                        className="size-4 accent-primary"
                      />
                      {t("enabledLabel")}
                    </label>
                  </div>
                </div>

                {!locked && (
                  <Button
                    type="button" variant="ghost" size="icon-sm"
                    data-testid={`blueprint-remove-${rowId}`}
                    aria-label={t("remove")}
                    onClick={() => removeSection(i)}
                    className="shrink-0"
                  >
                    <Trash2 />
                  </Button>
                )}
              </div>
            </article>
          );
        })}
      </div>

      <Button
        type="button"
        variant="outline"
        onClick={addSection}
        data-testid="blueprint-add-section"
        className="self-start"
      >
        <Plus />
        {t("add")}
      </Button>
    </div>
  );
}
