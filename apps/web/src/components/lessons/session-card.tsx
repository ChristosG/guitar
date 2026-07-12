"use client";

import { useState, type FormEvent, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import { ChevronsDown, Loader2, Scissors, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, deleteBlock, mergeSessions, splitSession, updateBlock, type BlockNode } from "@/lib/api";

interface SessionCardProps {
  session: BlockNode;
  lessonId: string;
  index: number;
  /** The immediately-following session in the outline, or `null` for the
   * last one — the ONLY pair this card will ever offer to merge (see this
   * component's own "merge with the one below" button). Sessions are
   * ordered by `order` server-side (`block_to_tree`'s own sort), so passing
   * exactly this pair to `mergeSessions` is always adjacent by
   * construction — the picker cannot ask for an invalid merge, per the
   * brief. */
  nextSession: BlockNode | null;
  /** Structural mutations (delete this session, split it, merge it down)
   * change what the OUTLINE renders, not just this card, so they bubble up
   * through this single pair of callbacks rather than local state:
   * `applyTree` replaces the page's whole lesson tree with an already-known
   * new one (split/merge responses ARE the new tree — no extra round trip
   * needed); `refreshTree` re-fetches `GET /lessons/{id}` for mutations
   * that DON'T return a tree (`PATCH`/`DELETE /blocks/{id}`, reused as-is
   * from the curriculum board). Trading one extra GET per rename/delete for
   * never hand-splicing a nested tree client-side is a deliberate
   * simplicity choice — see `lessons/[lessonId]/page.tsx`'s own docstring. */
  applyTree: (tree: BlockNode) => void;
  refreshTree: () => Promise<void>;
}

/** One session in the lesson outline: an inline-editable title + length,
 * its items listed underneath (via `ItemRow`, kept in this same file — the
 * brief's own file list names only three lesson components, and an item row
 * has no reason to exist outside a session), and the three actions the
 * brief calls for as "obvious buttons": Split, "merge with the one below",
 * and delete. Nothing else — this is an outline, not a form. */
export function SessionCard({ session, lessonId, index, nextSession, applyTree, refreshTree }: SessionCardProps) {
  const t = useTranslations("lessons.editor");

  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState(session.title);
  const [titleError, setTitleError] = useState<string | null>(null);

  async function commitTitle() {
    const next = draftTitle.trim();
    setEditingTitle(false);
    if (!next || next === session.title) {
      setDraftTitle(session.title);
      return;
    }
    setTitleError(null);
    try {
      await updateBlock(session.id, { title: next });
      await refreshTree();
    } catch (err) {
      setDraftTitle(session.title);
      setTitleError(err instanceof ApiError ? err.detail : t("renameError"));
    }
  }

  const [editingMinutes, setEditingMinutes] = useState(false);
  const [draftMinutes, setDraftMinutes] = useState(String(session.est_minutes ?? ""));
  const [minutesError, setMinutesError] = useState<string | null>(null);

  async function commitMinutes() {
    setEditingMinutes(false);
    const trimmed = draftMinutes.trim();
    const parsed = trimmed === "" ? null : Number(trimmed);
    if (parsed !== null && (!Number.isFinite(parsed) || parsed <= 0)) {
      setDraftMinutes(String(session.est_minutes ?? ""));
      return;
    }
    if (parsed === session.est_minutes) return;
    setMinutesError(null);
    try {
      await updateBlock(session.id, { est_minutes: parsed });
      await refreshTree();
    } catch (err) {
      setDraftMinutes(String(session.est_minutes ?? ""));
      setMinutesError(err instanceof ApiError ? err.detail : t("renameError"));
    }
  }

  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDelete() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteBlock(session.id);
      await refreshTree();
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.detail : t("deleteSessionError"));
      setDeleting(false);
    }
  }

  const [splitOpen, setSplitOpen] = useState(false);
  const [splitMinutes, setSplitMinutes] = useState("30");
  const [splitting, setSplitting] = useState(false);
  const [splitError, setSplitError] = useState<string | null>(null);

  async function handleSplit(e: FormEvent) {
    e.preventDefault();
    setSplitting(true);
    setSplitError(null);
    try {
      const tree = await splitSession(lessonId, session.id, { session_minutes: Number(splitMinutes) });
      applyTree(tree);
      setSplitOpen(false);
    } catch (err) {
      setSplitError(err instanceof ApiError ? err.detail : t("splitError"));
    } finally {
      setSplitting(false);
    }
  }

  const [merging, setMerging] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  async function handleMergeDown() {
    if (!nextSession) return;
    setMerging(true);
    setMergeError(null);
    try {
      const tree = await mergeSessions(lessonId, [session.id, nextSession.id]);
      applyTree(tree);
    } catch (err) {
      setMergeError(err instanceof ApiError ? err.detail : t("mergeError"));
    } finally {
      setMerging(false);
    }
  }

  function handleTitleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitTitle();
    } else if (e.key === "Escape") {
      setDraftTitle(session.title);
      setEditingTitle(false);
    }
  }

  function handleMinutesKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitMinutes();
    } else if (e.key === "Escape") {
      setDraftMinutes(String(session.est_minutes ?? ""));
      setEditingMinutes(false);
    }
  }

  const items = session.children; // already order-sorted by block_to_tree

  return (
    <Card
      size="sm"
      data-testid="session-card"
      data-session-id={session.id}
      className="border-l-4"
      style={{ borderLeftColor: "var(--chart-2)" }}
    >
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" data-testid="session-index" className="shrink-0">
            {t("sessionLabel", { index: index + 1 })}
          </Badge>

          {editingTitle ? (
            <Input
              autoFocus
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              onBlur={commitTitle}
              onKeyDown={handleTitleKeyDown}
              placeholder={t("sessionTitlePlaceholder")}
              data-testid="session-title-input"
              className="h-7 min-w-32 flex-1"
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditingTitle(true)}
              data-testid="session-title"
              className="min-w-32 flex-1 truncate text-left text-sm font-medium hover:underline"
            >
              {session.title}
            </button>
          )}

          {editingMinutes ? (
            <Input
              type="number"
              min={1}
              autoFocus
              value={draftMinutes}
              onChange={(e) => setDraftMinutes(e.target.value)}
              onBlur={commitMinutes}
              onKeyDown={handleMinutesKeyDown}
              data-testid="session-minutes-input"
              className="h-7 w-20 shrink-0"
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditingMinutes(true)}
              data-testid="session-minutes"
              className="shrink-0 text-xs text-muted-foreground hover:underline"
            >
              {session.est_minutes != null ? t("estMinutes", { count: session.est_minutes }) : t("setMinutes")}
            </button>
          )}

          <div className="ml-auto flex items-center gap-1.5">
            <Dialog
              open={splitOpen}
              onOpenChange={(next) => {
                if (splitting) return;
                setSplitOpen(next);
                if (!next) setSplitError(null);
              }}
            >
              <DialogTrigger
                render={
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    data-testid="session-split"
                    disabled={items.length === 0}
                  />
                }
              >
                <Scissors />
                {t("split")}
              </DialogTrigger>
              <DialogContent data-testid="split-dialog">
                <DialogHeader>
                  <DialogTitle>{t("splitHeading")}</DialogTitle>
                  <DialogDescription>{t("splitDescription", { title: session.title })}</DialogDescription>
                </DialogHeader>
                <form onSubmit={handleSplit} className="flex flex-col gap-3">
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor={`split-minutes-${session.id}`}>{t("splitMinutesLabel")}</Label>
                    <Input
                      id={`split-minutes-${session.id}`}
                      data-testid="split-minutes-input"
                      type="number"
                      min={1}
                      value={splitMinutes}
                      onChange={(e) => setSplitMinutes(e.target.value)}
                      disabled={splitting}
                      required
                    />
                  </div>
                  {splitError && (
                    <p role="alert" data-testid="split-error" className="text-sm text-destructive">
                      {splitError}
                    </p>
                  )}
                  <DialogFooter>
                    <DialogClose render={<Button type="button" variant="outline" disabled={splitting} />}>
                      {t("cancel")}
                    </DialogClose>
                    <Button type="submit" disabled={splitting} data-testid="split-submit">
                      {splitting ? t("splitting") : t("splitSubmit")}
                    </Button>
                  </DialogFooter>
                </form>
              </DialogContent>
            </Dialog>

            {nextSession && (
              <Button
                type="button"
                variant="outline"
                size="xs"
                data-testid="session-merge-down"
                disabled={merging}
                onClick={handleMergeDown}
                title={t("mergeDownTitle", { next: nextSession.title })}
              >
                {merging ? <Loader2 className="animate-spin" /> : <ChevronsDown />}
                {t("mergeDown")}
              </Button>
            )}

            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              data-testid="session-delete"
              disabled={deleting}
              onClick={handleDelete}
              aria-label={t("deleteSession")}
            >
              {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
            </Button>
          </div>
        </div>

        {titleError && (
          <p role="alert" data-testid="session-title-error" className="text-xs text-destructive">
            {titleError}
          </p>
        )}
        {minutesError && (
          <p role="alert" data-testid="session-minutes-error" className="text-xs text-destructive">
            {minutesError}
          </p>
        )}
        {deleteError && (
          <p role="alert" data-testid="session-delete-error" className="text-xs text-destructive">
            {deleteError}
          </p>
        )}
        {mergeError && (
          <p role="alert" data-testid="session-merge-error" className="text-xs text-destructive">
            {mergeError}
          </p>
        )}
      </CardHeader>

      <CardContent>
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground" data-testid="session-items-empty">
            {t("noItems")}
          </p>
        ) : (
          <ul className="flex flex-col gap-2" data-testid="session-items">
            {items.map((item) => (
              <ItemRow key={item.id} item={item} refreshTree={refreshTree} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

interface ItemRowProps {
  item: BlockNode;
  refreshTree: () => Promise<void>;
}

/** One item inside a session: an inline-editable title, its body shown as
 * plain read-only text underneath, and a delete button — the leaf of the
 * outline. There is no "add item" here: the API has no route to create one
 * (items only ever come from the LLM draft that authored the lesson), so
 * this deliberately doesn't invent an affordance the backend can't serve. */
function ItemRow({ item, refreshTree }: ItemRowProps) {
  const t = useTranslations("lessons.editor");

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.title);
  const [titleError, setTitleError] = useState<string | null>(null);

  async function commitTitle() {
    const next = draft.trim();
    setEditing(false);
    if (!next || next === item.title) {
      setDraft(item.title);
      return;
    }
    setTitleError(null);
    try {
      await updateBlock(item.id, { title: next });
      await refreshTree();
    } catch (err) {
      setDraft(item.title);
      setTitleError(err instanceof ApiError ? err.detail : t("renameError"));
    }
  }

  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleDelete() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteBlock(item.id);
      await refreshTree();
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.detail : t("deleteItemError"));
      setDeleting(false);
    }
  }

  return (
    <li data-testid="item-row" data-item-id={item.id} className="flex flex-col gap-1 border-l-2 border-border pl-3">
      <div className="flex items-center gap-2">
        <span className="size-1.5 shrink-0 rounded-full bg-muted-foreground/50" />
        {editing ? (
          <Input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitTitle();
              } else if (e.key === "Escape") {
                setDraft(item.title);
                setEditing(false);
              }
            }}
            data-testid="item-title-input"
            className="h-6 flex-1 text-xs"
          />
        ) : (
          <button
            type="button"
            onClick={() => setEditing(true)}
            data-testid="item-title"
            className="flex-1 truncate text-left text-xs font-medium hover:underline"
          >
            {item.title}
          </button>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          data-testid="item-delete"
          disabled={deleting}
          onClick={handleDelete}
          aria-label={t("deleteItem")}
        >
          {deleting ? <Loader2 className="size-3 animate-spin" /> : <Trash2 className="size-3" />}
        </Button>
      </div>
      {item.body && <p className="pl-3.5 text-xs whitespace-pre-wrap text-muted-foreground">{item.body}</p>}
      {titleError && (
        <p role="alert" data-testid="item-title-error" className="pl-3.5 text-xs text-destructive">
          {titleError}
        </p>
      )}
      {deleteError && (
        <p role="alert" data-testid="item-delete-error" className="pl-3.5 text-xs text-destructive">
          {deleteError}
        </p>
      )}
    </li>
  );
}
