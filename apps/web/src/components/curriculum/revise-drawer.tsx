"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Layers, Loader2, Maximize2, MessagesSquare, Minimize2, RotateCcw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ChatSessionsProvider } from "@/components/chat/chat-sessions";
import { ApiError, createChatSession, getOrCreateCurriculumChatSession, type BlockNode } from "@/lib/api";
import { useReviseScope } from "@/components/curriculum/revise-scope";
import { cn } from "@/lib/utils";

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
 * place of X rather than a raw id.
 *
 * Recurses through EVERY child regardless of `kind`, so this already reaches
 * segment leaves (`course -> module -> lesson -> segment` — see `BlockNode`'s
 * own docstring in `lib/api.ts`) without any extra branch: the surgical
 * `add_segment`/`edit_segment`/`remove_segment` ops (2026-07-20, Spec A)
 * reference a `lesson_id` or `segment_id` the same way the lesson-level ops
 * above reference theirs, and both resolve through this one map. */
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
 * PERSISTENCE (chat overhaul Task 4): the session is fetched LAZILY, on
 * first open — not on every board visit, so browsing a curriculum never
 * spends a chat-session row on a tutor who never opens the drawer — via
 * `getOrCreateCurriculumChatSession`, which resumes the ONE session already
 * bound to this `root_id` or creates the first one. This used to call
 * `createChatSession` unconditionally: harmless within a single page visit
 * (this component still only calls it once, guarded by `sessionId ||
 * creating`, exactly as now), but a page RELOAD resets that React state, so
 * every reload spent a brand-new session and orphaned whatever conversation
 * was already under way. Keying off the curriculum itself, server-side,
 * fixes that: re-opening (even across a reload) resumes the same thread.
 *
 * "Clear chat" is the deliberate escape hatch: it starts a genuinely NEW
 * session bound to the same `root_id` (plain `createChatSession`, unchanged)
 * and switches the drawer to it. The old session is not deleted — it simply
 * stops being the one `getOrCreateCurriculumChatSession` resumes next time
 * (that endpoint picks the MOST RECENTLY CREATED session for a root) — so
 * this is a fresh start, not data loss, which is why it goes through a
 * non-destructive confirm rather than the red delete-style one.
 *
 * FULL-SCREEN (chat overhaul Task 3): `fullScreen` is plain component state,
 * which is all "persist for the session's lifetime" needs here — this
 * component itself does not unmount on close (only the `{open && ...}` block
 * does), so the choice survives a close/re-open without anything extra.
 *
 * No dedicated Sheet/Drawer primitive exists yet under `components/ui`
 * (checked: only `dialog.tsx`, which is a centered modal, not a side panel) —
 * this is a plain fixed `aside` + backdrop, the documented fallback for
 * exactly this case.
 */
export function ReviseDrawer({ rootId, tree, onApplied }: ReviseDrawerProps) {
  const t = useTranslations("curricula.revise");
  const locale = useLocale();
  const confirm = useConfirm();

  const [open, setOpen] = useState(false);
  // Scope lives in the shared provider above BOTH this drawer and the board —
  // the module ⋯ menu is the other writer. See `revise-scope.tsx`.
  const reviseScope = useReviseScope();
  const scope = reviseScope?.scope ?? null;
  const seed = reviseScope?.seed;
  const [fullScreen, setFullScreen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);

  const blockTitles = useMemo(() => collectTitles(tree, {}), [tree]);

  // Escape-to-close (chat overhaul, Piece B review follow-up): in full-
  // screen mode the backdrop button is covered by the drawer itself
  // (`fullScreen ? "max-w-full" : ...` below leaves no backdrop showing to
  // click), so without this the only way out was the small X in the corner.
  // A plain `window` listener, not an `onKeyDown` on the aside — focus can be
  // anywhere inside the chat panel (the composer input, a button) when the
  // tutor reaches for Escape, and a listener scoped to one element would miss
  // every one of those. Skips a keystroke `defaultPrevented` by something
  // else (e.g. the "Clear chat" confirm dialog also closing on Escape) so the
  // two don't both react to the same press.
  useEffect(() => {
    if (!open) return;
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !e.defaultPrevented) setOpen(false);
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  const ensureSession = useCallback(async () => {
    if (sessionId || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await getOrCreateCurriculumChatSession(rootId);
      setSessionId(created.session_id);
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.detail : t("createError"));
    } finally {
      setCreating(false);
    }
  }, [creating, rootId, sessionId, t]);

  async function handleOpen() {
    // This button is the WHOLE-COURSE door, so it clears any scope a previous
    // module opening left behind — otherwise "Revise with AI" would silently
    // stay pointed at whatever module he restructured last.
    reviseScope?.clearScope();
    setOpen(true);
    await ensureSession();
  }

  // The module ⋯ menu asks to open by bumping `openRequest`. A counter rather
  // than a boolean because THIS component owns open/closed (Escape, the X, the
  // backdrop all close it) and a shared boolean would fight that.
  const lastRequest = useRef(0);
  useEffect(() => {
    const n = reviseScope?.openRequest ?? 0;
    if (n === 0 || n === lastRequest.current) return;
    lastRequest.current = n;
    setOpen(true);
    void ensureSession();
  }, [reviseScope?.openRequest, ensureSession]);

  async function handleClearChat() {
    const ok = await confirm({
      title: t("clearChatConfirmTitle"),
      body: t("clearChatConfirmBody"),
      confirmLabel: t("clearChat"),
      destructive: false,
    });
    if (!ok) return;

    setClearing(true);
    setClearError(null);
    try {
      const created = await createChatSession(null, locale, rootId);
      setSessionId(created.session_id);
    } catch (err) {
      setClearError(err instanceof ApiError ? err.detail : t("clearChatError"));
    } finally {
      setClearing(false);
    }
  }

  return (
    <>
      <Button type="button" variant="outline" data-testid="revise-open" onClick={handleOpen}>
        <MessagesSquare />
        {t("open")}
      </Button>

      {open && (
        <div
          className={cn("fixed inset-0 z-40 flex", fullScreen ? "justify-center" : "justify-end")}
          data-testid="revise-drawer"
          data-fullscreen={fullScreen ? "true" : "false"}
        >
          <button
            type="button"
            aria-label={t("close")}
            className="absolute inset-0 h-full w-full bg-black/10 backdrop-blur-xs"
            onClick={() => setOpen(false)}
          />
          <aside
            className={cn(
              "relative flex h-full w-full flex-col gap-4 overflow-hidden border-border bg-background p-4 shadow-xl",
              // `max-w-lg` — same widening as the lesson panel, for the same
              // reason and so the two side panels stay the same size.
              fullScreen ? "max-w-full border-l-0" : "max-w-lg border-l",
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <h2 className="font-heading text-base font-medium">{t("heading")}</h2>
                <p className="text-sm text-muted-foreground">{t("description")}</p>
                {/* NOT DECORATION. A scoped planner that looks unscoped is a
                    trap: he would ask for something course-wide, watch the plan
                    come back with half of it missing, and have no way to tell
                    why. The chip says which module, and clearing it is how you
                    get the whole course back. */}
                {scope && (
                  <div className="mt-2 flex items-center gap-1.5" data-testid="revise-scope-chip">
                    <span className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-muted px-2.5 py-0.5 text-xs">
                      <Layers className="size-3 shrink-0" aria-hidden />
                      {/* `min-w-0`: same latent bug as the lesson panel's chip —
                          a flex child will not shrink below its content width
                          without it, so `truncate` never engages. */}
                      <span className="min-w-0 truncate" title={scope.title}>
                        {t("scopedTo", { title: scope.title })}
                      </span>
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      data-testid="revise-scope-clear"
                      title={t("scopeClear")}
                      onClick={() => reviseScope?.clearScope()}
                    >
                      <X />
                      <span className="sr-only">{t("scopeClear")}</span>
                    </Button>
                  </div>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {sessionId && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    data-testid="revise-clear-chat"
                    disabled={clearing}
                    onClick={handleClearChat}
                    title={t("clearChat")}
                  >
                    {clearing ? <Loader2 className="animate-spin" /> : <RotateCcw />}
                    <span className="sr-only">{t("clearChat")}</span>
                  </Button>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  data-testid="revise-fullscreen-toggle"
                  onClick={() => setFullScreen((prev) => !prev)}
                  title={fullScreen ? t("collapse") : t("expand")}
                >
                  {fullScreen ? <Minimize2 /> : <Maximize2 />}
                  <span className="sr-only">{fullScreen ? t("collapse") : t("expand")}</span>
                </Button>
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
            </div>

            {clearError && (
              <p role="alert" data-testid="revise-clear-error" className="text-sm text-destructive">
                {clearError}
              </p>
            )}

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
                //
                // Keyed on `sessionId`: "Clear chat" swaps in a fresh id, and
                // `ChatPanel` must fully remount (fresh hydration, empty
                // transcript) rather than keep rendering the old
                // conversation's state alongside the new session's requests.
                <ChatSessionsProvider>
                  <ChatPanel
                    key={sessionId}
                    sessionId={sessionId}
                    rootId={rootId}
                    blockTitles={blockTitles}
                    onJobDone={onApplied}
                    seedDraft={seed}
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
