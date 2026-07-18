"use client";

import { useState } from "react";
import { ChevronDown, ChevronUp, Info, Plus, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { getBlueprintCodeDefault, type BlueprintSection, type BlueprintShape } from "@/lib/api";
import { cn } from "@/lib/utils";

const AUDIENCES: BlueprintSection["audience"][] = ["teacher", "student", "both"];

type StructuredKind = "exercises" | "qa";

interface BlueprintEditorProps {
  value: BlueprintShape;
  onChange: (next: BlueprintShape) => void;
}

/** THE SHARED BLUEPRINT EDITOR (Plan C, Task 6; full delete/re-add follow-up
 * 2026-07-19) — the lesson skeleton every curriculum drafts from, editable.
 * Mounted twice: as the Settings default (`blueprint-default-card.tsx`, against
 * `/blueprint/default`) and inside the wizard's optional "structure" step
 * (`interview-structure-step.tsx`, seeded from `findings.blueprint` and folded
 * into `interview.answers.structure` — see that component's own docstring). One
 * component, one set of rules, because a tutor who learns the shape once in
 * Settings must find the exact same controls when he reaches for it mid-wizard.
 *
 * MOSTLY CONTROLLED: every keystroke and reorder calls `onChange` with a
 * brand-new `BlueprintShape`, and the two call sites still own loading/saving/
 * persisting — no save button, no confirm dialog lives here. The ONE thing this
 * component fetches itself is the canonical STRUCTURED section template when the
 * tutor re-adds one from the Add menu (`getBlueprintCodeDefault()`) — sourced
 * live from the server rather than retyped here, so a backend wording change can
 * never silently drift out of sync with what "Add Exercises"/"Add Q&A" restores.
 *
 * `exercises` (kind `"exercises"`) and `qa_prompts` (kind `"qa"`) are STRUCTURED:
 * the model's guided-json schema for them is a fixed nested shape the app builds
 * in code (`depth._exercises_section`/`_qa_section`), keyed by their `kind`, not
 * their `key` string. FULL TUTOR CONTROL (2026-07-19): a structured section can
 * now be turned off, REMOVED entirely, or re-added from the Add menu — the only
 * things that stay fixed are its `kind` (so the model schema builder still knows
 * which shape to build) and, while it exists, its canonical `key` (so
 * `_section_minutes`/`persist_lesson` keep finding it by name). So only the KEY
 * input stays `disabled` for a structured row; its labels, description, weight,
 * audience, and order are exactly as live as any prose section's, and its Remove
 * button works like any other row's — an informational "Structured" badge
 * explains the one thing that is still true about it.
 *
 * The Add control is a small menu: "Add a text piece" is always offered (a fresh
 * `kind: "prose"` row); "Add Exercises" / "Add Q&A" are offered only while no
 * section of that kind currently exists — offering to add a second one would
 * just be rejected server-side (`structured_section_duplicate`), so the menu
 * simply does not present that dead end.
 */
export function BlueprintEditor({ value, onChange }: BlueprintEditorProps) {
  const t = useTranslations("blueprint");
  const [addError, setAddError] = useState<string | null>(null);

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

  function addProseSection() {
    setAddError(null);
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

  /** Re-add a deleted STRUCTURED section. Fetches the canonical section from the
   * live code default rather than keeping a second, hand-typed copy of its
   * label/description here — see this component's docstring. */
  async function addStructuredSection(kind: StructuredKind) {
    setAddError(null);
    try {
      const { blueprint: codeDefault } = await getBlueprintCodeDefault();
      const template = codeDefault.sections.find((s) => s.kind === kind);
      if (!template) return;
      onChange({
        ...value,
        sections: [...value.sections, { ...template, label: { ...template.label } }],
      });
    } catch {
      setAddError(t("addStructuredFailed"));
    }
  }

  const hasStructured = (kind: StructuredKind) => value.sections.some((s) => s.kind === kind);

  return (
    <div className="flex flex-col gap-3" data-testid="blueprint-editor">
      <div className="flex flex-col gap-2.5">
        {value.sections.map((section, i) => {
          const structured = section.kind !== "prose";
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
                  {structured && (
                    <Tooltip>
                      <TooltipTrigger render={<span className="inline-flex w-fit" />}>
                        <span
                          data-testid={`blueprint-structured-${rowId}`}
                          className="flex items-center gap-1.5 rounded-md bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                        >
                          <Info className="size-3 shrink-0" />
                          {t("structuredBadge")}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-xs">
                        {t(section.kind === "exercises"
                          ? "structuredTooltipExercises"
                          : "structuredTooltipQa")}
                      </TooltipContent>
                    </Tooltip>
                  )}

                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("keyLabel")}</Label>
                      <Input
                        value={section.key}
                        disabled={structured}
                        data-testid={`blueprint-key-${rowId}`}
                        onChange={(e) => patchSection(i, { key: e.target.value })}
                        className="h-8 font-mono text-xs"
                      />
                    </div>
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("labelElLabel")}</Label>
                      <Input
                        value={section.label.el}
                        data-testid={`blueprint-label-el-${rowId}`}
                        onChange={(e) => patchLabel(i, "el", e.target.value)}
                        className="h-8"
                      />
                    </div>
                    <div className="flex min-w-0 flex-col gap-1">
                      <Label className="text-xs">{t("labelEnLabel")}</Label>
                      <Input
                        value={section.label.en}
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

                <Button
                  type="button" variant="ghost" size="icon-sm"
                  data-testid={`blueprint-remove-${rowId}`}
                  aria-label={t("remove")}
                  onClick={() => removeSection(i)}
                  className="shrink-0"
                >
                  <Trash2 />
                </Button>
              </div>
            </article>
          );
        })}
      </div>

      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              type="button"
              variant="outline"
              data-testid="blueprint-add-section"
              className="self-start"
            />
          }
        >
          <Plus />
          {t("add")}
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem data-testid="blueprint-add-prose" onClick={addProseSection}>
            <Plus />
            {t("addProse")}
          </DropdownMenuItem>
          {!hasStructured("exercises") && (
            <DropdownMenuItem
              data-testid="blueprint-add-exercises"
              onClick={() => void addStructuredSection("exercises")}
            >
              <Plus />
              {t("addExercises")}
            </DropdownMenuItem>
          )}
          {!hasStructured("qa") && (
            <DropdownMenuItem
              data-testid="blueprint-add-qa"
              onClick={() => void addStructuredSection("qa")}
            >
              <Plus />
              {t("addQa")}
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      {addError && (
        <p role="alert" data-testid="blueprint-add-error" className="text-xs text-destructive">
          {addError}
        </p>
      )}
    </div>
  );
}
