"use client";

import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { RevisionPlan, RevisionPlanOp } from "@/lib/api";

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
 * schema requires it for those ops); `modify_lesson`/`move_lesson`/
 * `remove_lesson` carry only an id, so their name comes from `blockTitles` —
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
    default:
      // Defensive only — the backend's `_OP_ENUM` is closed and validate_ops
      // drops anything else before it ever reaches an approval.
      return op.op;
  }
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
