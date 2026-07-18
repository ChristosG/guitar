"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Loader2, TriangleAlert } from "lucide-react";

import {
  ApiError,
  getBlueprintDefault,
  resetBlueprintDefault,
  saveBlueprintDefault,
  type BlueprintShape,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm";
import { BlueprintEditor } from "@/components/settings/blueprint-editor";

/** THE SETTINGS DEFAULT — what every NEW curriculum is seeded with (Plan C,
 * Task 6). This card owns load/save/restore against `/blueprint/default`; the
 * actual editing surface is the shared `BlueprintEditor`, reused unchanged by
 * the wizard's optional "structure" step.
 *
 * EDITING THIS NEVER TOUCHES A COURSE THAT ALREADY EXISTS (spec invariant #3).
 * That guarantee lives entirely on the API side (`blueprint_from_course_meta`'s
 * fallback is the CODE default, never this table) — this card only has to be
 * honest that it is the FUTURE default, which `help` below says out loud.
 *
 * RESTORE IS CONFIRM-GATED, same posture as `prompt-list.tsx`'s Reset: once he
 * has spent an evening reshaping this, the button that throws it all away sits
 * right next to Save, and there is no history table to get it back from
 * (Resolved design call #4 — Reset = delete the row, the git-backed code default
 * is the restore target).
 */
export function BlueprintDefaultCard() {
  const t = useTranslations("blueprint");
  const confirm = useConfirm();

  const [blueprint, setBlueprint] = useState<BlueprintShape | null>(null);
  const [isOverride, setIsOverride] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const out = await getBlueprintDefault();
      setBlueprint(out.blueprint);
      setIsOverride(out.is_override);
    } catch (e) {
      setErrorCode(codeOf(e, "load_failed"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Any interaction invalidates a previous verdict — same rule as
   * `prompt-list.tsx`'s `SliceEditor.clearVerdict`. A green "Saved." beside a
   * structure he has since edited again is a lie with a tick on it. */
  function clearVerdict() {
    setSaved(false);
    setErrorCode(null);
  }

  function handleChange(next: BlueprintShape) {
    setBlueprint(next);
    clearVerdict();
  }

  async function onSave() {
    if (!blueprint) return;
    setSaving(true);
    clearVerdict();
    try {
      const out = await saveBlueprintDefault(blueprint);
      setBlueprint(out.blueprint);
      setIsOverride(out.is_override);
      setSaved(true);
    } catch (e) {
      setErrorCode(codeOf(e, "save_failed"));
    } finally {
      setSaving(false);
    }
  }

  async function onRestore() {
    const ok = await confirm({
      title: t("restoreConfirmTitle"),
      body: t("restoreConfirmBody"),
      confirmLabel: t("restore"),
      destructive: true,
    });
    if (!ok) return;
    clearVerdict();
    try {
      const out = await resetBlueprintDefault();
      setBlueprint(out.blueprint);
      setIsOverride(out.is_override);
    } catch (e) {
      setErrorCode(codeOf(e, "save_failed"));
    }
  }

  return (
    <Card data-testid="blueprint-default-card">
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">{t("help")}</p>

        {!blueprint && !errorCode && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            {t("loading")}
          </p>
        )}

        {blueprint && <BlueprintEditor value={blueprint} onChange={handleChange} />}

        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={onSave} disabled={saving || !blueprint} data-testid="blueprint-save">
            {saving && <Loader2 className="size-4 animate-spin" />}
            {saving ? t("saving") : t("save")}
          </Button>
          <Button
            variant="outline"
            onClick={onRestore}
            disabled={saving || !blueprint}
            data-testid="blueprint-restore"
          >
            {t("restore")}
          </Button>

          {isOverride && (
            <span
              data-testid="blueprint-overridden"
              className="rounded-md bg-primary/10 px-1.5 py-0.5 text-xs text-primary"
            >
              {t("overridden")}
            </span>
          )}
          {saved && (
            <span
              className="flex items-center gap-1.5 text-sm font-medium text-emerald-600 dark:text-emerald-400"
              data-testid="blueprint-saved"
            >
              <Check className="size-4" />
              {t("saved")}
            </span>
          )}
        </div>

        {errorCode && (
          <div
            role="alert"
            data-testid="blueprint-error"
            className="flex items-start gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive"
          >
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <span>
              {t.has(`errors.${errorCode}`) ? t(`errors.${errorCode}`) : t("errors.save_failed")}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** The server's machine-readable `code`, or a fallback — never `detail`, which
 * is server prose the tutor must never actually read (mirrors
 * `prompt-list.tsx`'s own `codeOf`). */
function codeOf(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.code ? e.code : fallback;
}
