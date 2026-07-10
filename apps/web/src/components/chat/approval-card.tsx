"use client";

import { Fragment, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";

interface ApprovalCardProps {
  description: string;
  toolName: string;
  toolArgs: Record<string, unknown>;
  resolving: boolean;
  error: string | null;
  onApprove: (editedArgs?: Record<string, unknown>) => void;
  onReject: () => void;
}

/** Renders one `tool_args` value "readably" per the brief: strings as-is
 * (no quotes/escaping noise), anything else (numbers, booleans, nested
 * objects/arrays) as compact JSON — good enough for a tutor to sanity-check
 * an arg at a glance without this needing bespoke per-tool formatting (the
 * API's own `_default_description` docstring in `routers/chat.py` is
 * explicit that per-tool copy is NOT this task's concern). */
function formatArgValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/**
 * The HITL gate every mutation tool call goes through (Plan 5 Task 4's
 * `awaiting_approval` turn): the model's own lead-in narration (`
 * description` — always populated, since the API falls back to a generic
 * "Proposed action: ..." when the model gave none, per `routers/chat.py`'s
 * `_default_description`), the raw `tool_name` + `tool_args` so the tutor
 * can see exactly what's about to run, and three actions:
 *  - Approve: runs it exactly as proposed (`onApprove()`, no `editedArgs`).
 *  - Edit: reveals a JSON textarea seeded from `tool_args`; Approve from
 *    here parses the draft and calls `onApprove(parsed)` instead of the
 *    original args — invalid JSON shows an inline error and does NOT call
 *    back, matching this codebase's "validate client-side before the
 *    network call" convention (e.g. `segment-dialog.tsx`'s number inputs).
 *  - Reject: `onReject()` — edited args are meaningless on this path (the
 *    API only ever reads `edited_args` on the approve branch — see
 *    `resolve_approval`'s reject branch in `routers/chat.py`), so any
 *    in-progress edit is simply discarded.
 *
 * `resolving`/`error` are owned by the parent (`chat-panel.tsx`) — this
 * component only renders them, same split as `generate-dialog.tsx`'s own
 * `submitting`/`error` props. The parent also keys this component by
 * `approval_id` (see `chat-panel.tsx`) so a resumed turn that immediately
 * proposes a DIFFERENT mutation — a real, documented case in `routers/
 * chat.py`'s `_respond_to_turn` — gets a fresh instance instead of stale
 * `editing`/`draft` state left over from the previous approval.
 */
export function ApprovalCard({
  description,
  toolName,
  toolArgs,
  resolving,
  error,
  onApprove,
  onReject,
}: ApprovalCardProps) {
  const t = useTranslations("chat.approval");

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => JSON.stringify(toolArgs, null, 2));
  const [parseError, setParseError] = useState<string | null>(null);

  function toggleEdit() {
    if (editing) {
      // Cancelling: discard the draft, back to the read-only view.
      setDraft(JSON.stringify(toolArgs, null, 2));
      setParseError(null);
    }
    setEditing((prev) => !prev);
  }

  function handleApprove() {
    if (!editing) {
      onApprove();
      return;
    }
    try {
      const parsed = JSON.parse(draft);
      setParseError(null);
      onApprove(parsed);
    } catch {
      setParseError(t("invalidJson"));
    }
  }

  const argEntries = Object.entries(toolArgs);

  return (
    <Card data-testid="approval-card" className="border-primary/30">
      <CardHeader className="gap-1.5">
        <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
          {t("heading")}
          <Badge variant="outline" data-testid="approval-tool-name">
            {toolName}
          </Badge>
        </CardTitle>
        {description && <CardDescription data-testid="approval-description">{description}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <fieldset disabled={resolving} className="flex flex-col gap-3">
          {!editing && argEntries.length > 0 && (
            <dl data-testid="approval-args" className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
              {argEntries.map(([key, value]) => (
                <Fragment key={key}>
                  <dt className="font-medium text-muted-foreground">{key}</dt>
                  <dd className="truncate" data-testid="approval-arg-value">
                    {formatArgValue(value)}
                  </dd>
                </Fragment>
              ))}
            </dl>
          )}

          {editing && (
            <div className="flex flex-col gap-1.5">
              <Textarea
                data-testid="approval-edit-args"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={6}
                className="font-mono text-xs"
              />
              {parseError && (
                <p role="alert" data-testid="approval-edit-error" className="text-xs text-destructive">
                  {parseError}
                </p>
              )}
            </div>
          )}

          {error && (
            <p role="alert" data-testid="approval-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <Button type="button" data-testid="approval-approve" onClick={handleApprove}>
              {resolving && <Loader2 className="animate-spin" />}
              {t("approve")}
            </Button>
            <Button type="button" variant="outline" data-testid="approval-edit" onClick={toggleEdit}>
              {editing ? t("cancelEdit") : t("edit")}
            </Button>
            <Button type="button" variant="destructive" data-testid="approval-reject" onClick={onReject}>
              {t("reject")}
            </Button>
          </div>
        </fieldset>
      </CardContent>
    </Card>
  );
}
