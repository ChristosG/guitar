"use client";

import { useTranslations } from "next-intl";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { RevisionImpact, RevisionPlan, RevisionPlanDroppedOp, RevisionPlanOp } from "@/lib/api";

interface RevisionPlanCardProps {
  plan: RevisionPlan;
  /** id -> title, built from the curriculum's own tree (the revise drawer's
   * only caller — see `revise-drawer.tsx`). Several ops carry only a bare id
   * (see below), and "Rewrite lesson «X»" is meaningless without a name to
   * put where X is. */
  blockTitles: Record<string, string>;
  resolving: boolean;
  error: string | null;
  onApprove: () => void;
  onReject: () => void;
}

type Translator = ReturnType<typeof useTranslations>;

/** One legible label per op kind — the controller's own resolved design
 * calls (2026-07-18), verbatim:
 *  - a `modify_lesson` op is a FULL RE-DRAFT, so it must read "Rewrite
 *    lesson «X»", never "edit"/"tweak" — the tutor approves a rewrite
 *    knowingly.
 *  - an `update_blueprint` op reads "Change lesson structure" and gets its
 *    own note (rendered separately below) that existing lessons keep their
 *    content until the tutor re-drafts them.
 *
 * `insert_lesson`/`insert_module` already carry their own `title` (the
 * schema requires it for those ops), as does `add_segment`;
 * `modify_lesson`/`move_lesson`/`remove_lesson`/`edit_segment`/
 * `remove_segment` carry only an id, so their name comes from `blockTitles` —
 * falling back to a generic label in the (should-be-rare) case a plan
 * references a block this client's own tree snapshot doesn't have. */
function opLabel(op: RevisionPlanOp, blockTitles: Record<string, string>, t: Translator): string {
  const titleOf = (id?: string) => (id && blockTitles[id]) || t("unknownBlock");
  switch (op.op) {
    case "insert_lesson":
      return t("op.insert_lesson", { title: op.title ?? "" });
    case "insert_module":
      return t("op.insert_module", { title: op.title ?? "" });
    case "modify_lesson":
      return t("op.modify_lesson", { title: titleOf(op.lesson_id) });
    case "move_lesson":
      return t("op.move_lesson", { title: titleOf(op.lesson_id), module: titleOf(op.to_module_id) });
    case "remove_lesson":
      return t("op.remove_lesson", { title: titleOf(op.lesson_id) });
    case "update_blueprint":
      return t("op.update_blueprint");
    // 2026-07-20 hotfix: the common case for enabling/renaming ONE recurring
    // section no longer goes through update_blueprint's full-object rewrite —
    // it's this op instead, carrying just the section_key (+ optional label).
    // `enabled` defaults true server-side, so an absent field still reads as
    // "enable", not a blank label.
    case "set_section_enabled":
      return op.enabled === false
        ? t("op.set_section_enabled_off", { section: op.section_key ?? "" })
        : t("op.set_section_enabled_on", { section: op.section_key ?? "" });
    // The three surgical segment ops (2026-07-20, Spec A; named-per-op fix
    // 2026-07-20 review follow-up): `remove_segment` in particular is
    // destructive, so the tutor must see WHICH segment before approving —
    // same house pattern as `modify_lesson`/`move_lesson`/`remove_lesson`
    // above, resolving the id through `titleOf` (falls back to
    // `unknownBlock` for a stale plan whose id isn't in this client's own
    // tree snapshot). `add_segment` carries its OWN proposed `title` (the
    // schema requires it), shown together with the parent lesson's resolved
    // name for context.
    case "add_segment":
      return t("op.add_segment", { title: op.title ?? "", lesson: titleOf(op.lesson_id) });
    case "edit_segment":
      return t("op.edit_segment", { title: titleOf(op.segment_id) });
    case "remove_segment":
      return t("op.remove_segment", { title: titleOf(op.segment_id) });
    default:
      // Defensive only — the backend's `_OP_ENUM` is closed and validate_ops
      // drops anything else before it ever reaches an approval.
      return op.op;
  }
}

/** Composes the approval card's blast-radius banner from `plan.impact`
 * (server-computed, never LLM-derived — see `RevisionImpact` in `lib/api.ts`).
 * Two registers, chosen by `impact.destructive`:
 *  - destructive: one sentence per non-zero bucket that REWRITES or REMOVES
 *    existing material — `rewrites` (`modify_lesson`+`move_lesson`), then
 *    removals (`lesson_removals` + `segment_removals`, combined into one
 *    count: the tutor doesn't need to parse "1 lesson and 2 segments" to
 *    know something existing is going away). `destructive` is only ever true
 *    because one of these is >0, so this branch never falls through empty.
 *  - surgical: names only the additive/edit segment counts
 *    (`segment_additions`, `segment_edits`) that make this plan
 *    non-destructive; falls back to a bare "everything else stays as is"
 *    when neither is present (e.g. a plan that's pure `insert_lesson`/
 *    `update_blueprint` — those already get their own per-op label in the
 *    list below, so the banner doesn't repeat them).
 * Each part carries its own ICU plural for the count noun; the SENTENCES
 * (not the counts within one sentence) are what drop out at zero, composed
 * here in JS rather than baked into one giant conditional message key. */
function impactMessage(impact: RevisionImpact, t: Translator): string {
  if (impact.destructive) {
    const parts: string[] = [];
    if (impact.rewrites > 0) parts.push(t("impact.destructiveRewrites", { count: impact.rewrites }));
    const removals = impact.lesson_removals + impact.segment_removals;
    if (removals > 0) parts.push(t("impact.destructiveRemovals", { count: removals }));
    return parts.join(" ");
  }
  const parts: string[] = [];
  if (impact.segment_additions > 0) parts.push(t("impact.surgicalAdditions", { count: impact.segment_additions }));
  if (impact.segment_edits > 0) parts.push(t("impact.surgicalEdits", { count: impact.segment_edits }));
  if (parts.length === 0) return t("impact.surgicalNeutral");
  return t("impact.surgicalPrefix") + parts.join(", ") + t("impact.surgicalSuffix");
}

/** One line of the dropped-ops warning (2026-07-20 hotfix): `op` is the RAW,
 * FAILED op the model proposed — it may be missing required fields or carry a
 * shorthand id, so this reads only `op.op` (never assumes the rest of its
 * shape) and pairs it with the server's own `reason` string verbatim. The
 * reason is a technical validation diagnostic, not tutor-facing prose the app
 * authored — shown as-is (no i18n) the same way a stack trace would be,
 * because paraphrasing it risks losing exactly the detail (an id, a section
 * key) that explains what to fix. */
function droppedOpLabel(entry: RevisionPlanDroppedOp): string {
  return `${entry.op.op ?? "?"} — ${entry.reason}`;
}

/**
 * The `ApprovalCard` VARIANT for `apply_curriculum_revision` (Unit D, Task
 * D2b). `chat-panel.tsx` renders this instead of the generic `ApprovalCard`
 * whenever the pending tool is a curriculum revision — same Approve/Reject
 * shell and the same `resolving`/`error` split owned by the parent, but each
 * op gets a legible, per-kind label + its `reason` instead of a raw
 * `tool_args` dump.
 *
 * Renders the plan EXACTLY as `apply_curriculum_revision`'s tool args carry
 * it — which is, by construction, the plan the backend already ID-validated
 * before the `ApprovalRequest` was ever stored (controller's resolved design
 * call #3: "approved == applied must be EXACT"). There is deliberately NO
 * raw-JSON edit affordance here (unlike the generic `ApprovalCard`): a
 * hand-edited plan would defeat the id-validation story the backend already
 * ran, and the server re-validates on apply regardless — editing here would
 * only be theatre with a way to break it.
 */
export function RevisionPlanCard({
  plan,
  blockTitles,
  resolving,
  error,
  onApprove,
  onReject,
}: RevisionPlanCardProps) {
  const t = useTranslations("curricula.revise");

  return (
    <Card data-testid="revision-plan-card" className="border-primary/30">
      <CardHeader className="gap-1.5">
        <CardTitle className="text-sm">{t("planHeading")}</CardTitle>
        {plan.summary && (
          <CardDescription data-testid="revision-plan-summary">{plan.summary}</CardDescription>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <fieldset disabled={resolving} className="flex flex-col gap-3">
          {plan.impact && (
            <div
              data-testid="plan-impact"
              data-destructive={plan.impact.destructive ? "true" : "false"}
              className={
                plan.impact.destructive
                  ? "flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300"
                  : "flex items-start gap-2 rounded-xl border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
              }
            >
              {plan.impact.destructive ? (
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              ) : (
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              )}
              <span>{impactMessage(plan.impact, t)}</span>
            </div>
          )}

          {/* 2026-07-20 hotfix: `validate_ops` can silently drop a proposed op
           * (a hallucinated id, a section the blueprint doesn't have) — the
           * plan the tutor is about to approve is then SMALLER than what was
           * asked for, and he must know that before he approves it, not
           * discover it after. */}
          {plan.dropped && plan.dropped.length > 0 && (
            <div
              data-testid="plan-dropped"
              className="flex flex-col gap-1.5 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300"
            >
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span>{t("dropped.warning", { count: plan.dropped.length })}</span>
              </div>
              <ul className="ml-5 list-disc">
                {plan.dropped.map((entry, i) => (
                  <li key={i} data-testid="plan-dropped-reason">
                    {droppedOpLabel(entry)}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ul data-testid="revision-plan-ops" className="flex flex-col gap-2 text-sm">
            {plan.ops.map((op, i) => (
              <li key={i} data-testid="revision-plan-op" className="rounded-md border border-border p-2">
                <p className="font-medium">{opLabel(op, blockTitles, t)}</p>
                {op.reason && <p className="text-xs text-muted-foreground">{op.reason}</p>}
                {op.op === "update_blueprint" && (
                  <p className="mt-1 text-xs text-muted-foreground" data-testid="revision-blueprint-note">
                    {t("blueprintNote")}
                  </p>
                )}
              </li>
            ))}
          </ul>

          {error && (
            <p role="alert" data-testid="revision-plan-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <Button type="button" data-testid="revision-approve" onClick={onApprove}>
              {resolving && <Loader2 className="animate-spin" />}
              {t("approve")}
            </Button>
            <Button type="button" variant="destructive" data-testid="revision-reject" onClick={onReject}>
              {t("reject")}
            </Button>
          </div>
        </fieldset>
      </CardContent>
    </Card>
  );
}
