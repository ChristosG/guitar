"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Check, Loader2, Pencil, Plus, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { useChatSessions } from "@/components/chat/chat-sessions";
import {
  ApiError,
  createChatSession,
  deleteChatSession,
  renameChatSession,
  type ChatSessionSummary,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/** The conversation list (Plan 13 Stage 5.6). Before this, every chat was
 * orphaned by a page refresh: `chat-panel.tsx` created a session on mount and
 * kept its id in React state only, so the transcript the API had faithfully
 * persisted was unreachable forever.
 *
 * "New chat" creates the session EAGERLY (`POST /chat`) and navigates to its
 * URL, rather than deferring creation until the first message. The lazy
 * alternative sounds tidier and isn't: the panel would have to swap the URL
 * out from under itself mid-turn (`/chat` -> `/chat/{id}`), which in the App
 * Router remounts the page component — dropping an in-flight SSE stream and
 * re-hydrating from a transcript the server hasn't finished writing. The cost
 * of eagerness is an empty session row for a "new chat" the tutor never types
 * into, and `GET /chat` (INNER JOIN on `message`) never lists those.
 */
export function ChatSidebar() {
  const t = useTranslations("chat.history");
  const locale = useLocale();
  const router = useRouter();
  const confirm = useConfirm();
  const params = useParams<{ sessionId?: string }>();
  const activeId = params?.sessionId;

  const { sessions, loading, error, refresh, applyTitle, removeSession } = useChatSessions();

  const [creating, setCreating] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function handleNew() {
    setCreating(true);
    setActionError(null);
    try {
      const { session_id } = await createChatSession(null, locale);
      router.push(`/${locale}/chat/${session_id}`);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : t("createError"));
    } finally {
      setCreating(false);
    }
  }

  function startRename(session: ChatSessionSummary) {
    setRenamingId(session.id);
    setDraftTitle(session.title ?? "");
    setActionError(null);
  }

  async function submitRename(e: FormEvent, sessionId: string) {
    e.preventDefault();
    const title = draftTitle.trim();
    if (!title) return;

    setBusyId(sessionId);
    setActionError(null);
    try {
      await renameChatSession(sessionId, title);
      applyTitle(sessionId, title);
      setRenamingId(null);
      await refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : t("renameError"));
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(session: ChatSessionSummary) {
    // Deleting a conversation cascades its whole transcript and every
    // approval on it — the exact class of irreversible click the confirm
    // dialog exists for (Plan 13 Stage 5.x).
    const ok = await confirm({
      title: t("deleteTitle"),
      body: t("deleteBody", {
        title: session.title ?? t("untitled"),
        count: session.message_count,
      }),
      confirmLabel: t("delete"),
      destructive: true,
    });
    if (!ok) return;

    setBusyId(session.id);
    setActionError(null);
    try {
      await deleteChatSession(session.id);
      removeSession(session.id);
      // Deleting the conversation you're LOOKING AT leaves the panel pointed
      // at a 404. Bounce to the index, which resumes the next-most-recent
      // session (or starts a fresh one).
      if (session.id === activeId) router.replace(`/${locale}/chat`);
      await refresh();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : t("deleteError"));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <aside data-testid="chat-sidebar" className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-muted-foreground">{t("heading")}</h2>
        <Button
          type="button"
          size="sm"
          variant="outline"
          data-testid="chat-new"
          onClick={handleNew}
          disabled={creating}
        >
          {creating ? <Loader2 className="animate-spin" /> : <Plus />}
          {t("newChat")}
        </Button>
      </div>

      {actionError && (
        <p role="alert" data-testid="chat-sessions-error" className="text-sm text-destructive">
          {actionError}
        </p>
      )}
      {error && !actionError && (
        <p role="alert" data-testid="chat-sessions-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {loading ? (
        <p className="text-sm text-muted-foreground">
          <Loader2 className="inline size-4 animate-spin" />
        </p>
      ) : sessions.length === 0 ? (
        <p data-testid="chat-sessions-empty" className="text-sm text-muted-foreground">
          {t("empty")}
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {sessions.map((session) => {
            const isActive = session.id === activeId;
            const busy = busyId === session.id;

            if (renamingId === session.id) {
              return (
                <li key={session.id}>
                  <form
                    onSubmit={(e) => submitRename(e, session.id)}
                    className="flex items-center gap-1 rounded-lg border border-border p-1"
                  >
                    <Input
                      autoFocus
                      data-testid="chat-rename-input"
                      value={draftTitle}
                      onChange={(e) => setDraftTitle(e.target.value)}
                      placeholder={t("renamePlaceholder")}
                      className="h-8"
                    />
                    <Button
                      type="submit"
                      size="icon"
                      variant="ghost"
                      className="size-8 shrink-0"
                      data-testid="chat-rename-save"
                      aria-label={t("save")}
                      disabled={busy || !draftTitle.trim()}
                    >
                      {busy ? <Loader2 className="animate-spin" /> : <Check />}
                    </Button>
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      className="size-8 shrink-0"
                      aria-label={t("cancel")}
                      onClick={() => setRenamingId(null)}
                    >
                      <X />
                    </Button>
                  </form>
                </li>
              );
            }

            return (
              <li
                key={session.id}
                data-testid="chat-session-item"
                data-session-id={session.id}
                data-active={isActive ? "true" : undefined}
                className={cn(
                  "group flex items-center gap-1 rounded-lg px-2 py-1.5 hover:bg-muted/60",
                  isActive && "bg-muted",
                )}
              >
                <Link
                  href={`/${locale}/chat/${session.id}`}
                  data-testid="chat-session-link"
                  className="min-w-0 flex-1"
                >
                  <span
                    data-testid="chat-session-title"
                    className="block truncate text-sm font-medium"
                  >
                    {session.title ?? t("untitled")}
                  </span>
                  {session.preview && (
                    <span className="block truncate text-xs text-muted-foreground">
                      {session.preview}
                    </span>
                  )}
                </Link>

                {/* Always in the DOM (only visually revealed on hover/focus):
                    a `hidden`-until-hover control is unreachable to keyboard
                    users and to Playwright without a synthetic hover. */}
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="size-7 shrink-0 opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                  data-testid="chat-rename"
                  aria-label={t("rename")}
                  onClick={() => startRename(session)}
                >
                  <Pencil />
                </Button>
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  className="size-7 shrink-0 text-destructive opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                  data-testid="chat-delete"
                  aria-label={t("delete")}
                  onClick={() => handleDelete(session)}
                  disabled={busy}
                >
                  {busy ? <Loader2 className="animate-spin" /> : <Trash2 />}
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}
