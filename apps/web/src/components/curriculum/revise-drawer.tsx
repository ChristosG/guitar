"use client";

import { useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Loader2, MessagesSquare, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ChatSessionsProvider } from "@/components/chat/chat-sessions";
import { ApiError, createChatSession, type BlockNode } from "@/lib/api";

interface ReviseDrawerProps {
  rootId: string;
  /** The board's own current tree — read ONLY to build the id -> title
   * lookup `RevisionPlanCard` needs (see `collectTitles` below). Never
   * mutated here; the board (`TreeBoard`, refetched via `onApplied`) stays
   * the one owner of what's actually rendered. */
  tree: BlockNode;
  /** Called when an approved revision has been applied (the async job behind
   * `apply_curriculum_revision` succeeded) — the page's own `refreshTree`,
   * so the board's tree and its draft-progress bar pick up the new/changed
   * lessons. */
  onApplied: () => void;
}

/** Flattens the tree into an id -> title map. `modify_lesson`/`move_lesson`/
 * `remove_lesson` ops carry only a bare `lesson_id`/`to_module_id`
 * (`curriculum/revise.py`'s flat op schema never repeats a title the model
 * already said once), so `RevisionPlanCard` needs this to render "Rewrite
 * lesson «X»" — the controller's own required wording — with a real name in
 * place of X rather than a raw id. */
function collectTitles(node: BlockNode, into: Record<string, string>): Record<string, string> {
  into[node.id] = node.title;
  for (const child of node.children) collectTitles(child, into);
  return into;
}

/**
 * The "Revise with AI" drawer on the curriculum detail board (Unit D, Task
 * D2b) — a right-side panel holding a `ChatPanel` bound to THIS curriculum
 * via a chat session's `root_id` (Unit D, Task D2a's nullable column). It
 * reuses `ChatPanel` wholesale: transcript, streaming, the HITL approval
 * gate, job polling — every bit of chat infrastructure the cockpit's own
 * Chat page already exercises. The only new surface `ChatPanel` adds for
 * this caller is swapping in `RevisionPlanCard` for the generic
 * `ApprovalCard` when the pending tool is `apply_curriculum_revision`.
 *
 * The session is created LAZILY, on first open — not on every board visit —
 * so browsing a curriculum never spends a chat-session row on a tutor who
 * never opens the drawer. It stays open for the rest of the page's life
 * (closing the drawer just hides it; re-opening reuses the same session id,
 * so a conversation already under way survives a close/re-open).
 *
 * No dedicated Sheet/Drawer primitive exists yet under `components/ui`
 * (checked: only `dialog.tsx`, which is a centered modal, not a side panel) —
 * this is a plain fixed `aside` + backdrop, the documented fallback for
 * exactly this case.
 */
export function ReviseDrawer({ rootId, tree, onApplied }: ReviseDrawerProps) {
  const t = useTranslations("curricula.revise");
  const locale = useLocale();

  const [open, setOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const blockTitles = useMemo(() => collectTitles(tree, {}), [tree]);

  async function handleOpen() {
    setOpen(true);
    if (sessionId || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createChatSession(null, locale, rootId);
      setSessionId(created.session_id);
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.detail : t("createError"));
    } finally {
      setCreating(false);
    }
  }

  return (
    <>
      <Button type="button" variant="outline" data-testid="revise-open" onClick={handleOpen}>
        <MessagesSquare />
        {t("open")}
      </Button>

      {open && (
        <div className="fixed inset-0 z-40 flex justify-end" data-testid="revise-drawer">
          <button
            type="button"
            aria-label={t("close")}
            className="absolute inset-0 h-full w-full bg-black/10 backdrop-blur-xs"
            onClick={() => setOpen(false)}
          />
          <aside className="relative flex h-full w-full max-w-md flex-col gap-4 overflow-hidden border-l border-border bg-background p-4 shadow-xl">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="font-heading text-base font-medium">{t("heading")}</h2>
                <p className="text-sm text-muted-foreground">{t("description")}</p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                data-testid="revise-close"
                onClick={() => setOpen(false)}
              >
                <X />
                <span className="sr-only">{t("close")}</span>
              </Button>
            </div>

            <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
              {creating && (
                <div
                  role="status"
                  data-testid="revise-session-loading"
                  className="flex items-center gap-2 text-sm text-muted-foreground"
                >
                  <Loader2 className="size-4 animate-spin" />
                </div>
              )}
              {createError && (
                <p role="alert" data-testid="revise-session-error" className="text-sm text-destructive">
                  {createError}
                </p>
              )}
              {sessionId && (
                // `ChatPanel` refreshes the CHAT SIDEBAR's own session list
                // after every turn (`useChatSessions()`), which normally
                // comes from `chat/layout.tsx`'s provider — there is no
                // sidebar here, but the hook still requires an ancestor, so
                // this reuses the SAME provider rather than teaching
                // `ChatPanel` a special case for "no sidebar to refresh".
                <ChatSessionsProvider>
                  <ChatPanel
                    sessionId={sessionId}
                    rootId={rootId}
                    blockTitles={blockTitles}
                    onJobDone={onApplied}
                  />
                </ChatSessionsProvider>
              )}
            </div>
          </aside>
        </div>
      )}
    </>
  );
}
