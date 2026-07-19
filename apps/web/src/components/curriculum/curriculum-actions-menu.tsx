"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Loader2, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { ApiError, deleteCurriculum, renameCurriculum } from "@/lib/api";

interface CurriculumActionsMenuProps {
  rootId: string;
  title: string;
  onRenamed: (nextTitle: string) => void;
  onDeleted: () => void;
}

/** The "⋯" on a curriculum — Rename (dialog) and Delete (confirm, destructive).
 * Used on the index cards and the detail header; the CALLER decides what
 * follows (refresh the list / navigate away).
 *
 * Delete's 409 ("lessons are still drafting") is a codebase-convention
 * ENGLISH string from the API (`routers/curricula.py`) — this is the one
 * error the tutor is guaranteed to hit in the ordinary course of using the
 * app (materialize a curriculum, immediately try to delete it while it's
 * still writing), so it gets a real Greek/English sentence via `code`-free
 * status branching rather than surfacing `err.detail` verbatim. */
export function CurriculumActionsMenu({ rootId, title, onRenamed, onDeleted }: CurriculumActionsMenuProps) {
  const t = useTranslations("curricula.actions");
  const confirm = useConfirm();
  const [renameOpen, setRenameOpen] = useState(false);
  const [draftTitle, setDraftTitle] = useState(title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submitRename = async (e: FormEvent) => {
    e.preventDefault();
    const next = draftTitle.trim();
    if (!next || next === title) {
      setRenameOpen(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await renameCurriculum(rootId, next);
      setRenameOpen(false);
      onRenamed(updated.title);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("renameError"));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: t("deleteTitle"),
      body: t("deleteDescription", { title }),
      confirmLabel: t("deleteConfirm"),
      destructive: true,
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await deleteCurriculum(rootId);
      onDeleted();
    } catch (err) {
      setError(
        err instanceof ApiError ? (err.status === 409 ? t("deleteDrafting") : err.detail) : t("deleteError"),
      );
      setBusy(false);
    }
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              type="button"
              size="icon-sm"
              variant="ghost"
              data-testid="curriculum-actions-trigger"
              aria-label={t("menuLabel")}
              disabled={busy}
            />
          }
        >
          {busy ? <Loader2 className="animate-spin" /> : <MoreHorizontal />}
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem
            data-testid="curriculum-rename"
            onClick={() => {
              setDraftTitle(title);
              setError(null);
              setRenameOpen(true);
            }}
          >
            <Pencil />
            {t("rename")}
          </DropdownMenuItem>
          <DropdownMenuItem destructive data-testid="curriculum-delete" onClick={handleDelete}>
            <Trash2 />
            {t("delete")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Delete has no dialog of its own (it goes straight through
       * `useConfirm()`), so a failed delete has nowhere else to surface —
       * this is the only place it shows. Gated on `!renameOpen` purely to
       * avoid a double-render of the same message: the two flows never run
       * at once, but the rename dialog owns its own copy below while open. */}
      {error && !renameOpen && (
        <p
          role="alert"
          data-testid="curriculum-actions-error"
          className="mt-1 max-w-48 text-right text-xs text-destructive"
        >
          {error}
        </p>
      )}

      <Dialog
        open={renameOpen}
        onOpenChange={(next) => {
          if (busy) return;
          setRenameOpen(next);
          if (!next) setError(null);
        }}
      >
        <DialogContent data-testid="curriculum-rename-dialog">
          <DialogHeader>
            <DialogTitle>{t("renameTitle")}</DialogTitle>
          </DialogHeader>
          <form onSubmit={submitRename} className="flex flex-col gap-3">
            <Input
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              data-testid="curriculum-rename-input"
              autoFocus
              disabled={busy}
            />
            {error && (
              <p role="alert" data-testid="curriculum-actions-error" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={busy} onClick={() => setRenameOpen(false)}>
                {t("cancel")}
              </Button>
              <Button type="submit" disabled={busy || !draftTitle.trim()} data-testid="curriculum-rename-save">
                {busy && <Loader2 className="animate-spin" />}
                {t("renameSave")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
