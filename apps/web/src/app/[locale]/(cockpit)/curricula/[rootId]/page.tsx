"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft, FileDown, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CurriculumActionsMenu } from "@/components/curriculum/curriculum-actions-menu";
import { ReviseDrawer } from "@/components/curriculum/revise-drawer";
import { TreeBoard } from "@/components/curriculum/tree-board";
import {
  ApiError,
  downloadCurriculumDocx,
  getCurriculum,
  type BlockNode,
  type CurriculumListItem,
} from "@/lib/api";

/** The per-curriculum board, split out of the old `curricula/page.tsx` so the
 * list page can become a plain navigable index (Unit A). Same shape as
 * `lessons/[lessonId]/page.tsx` / `library/
 * [sourceId]/page.tsx`: a client component reading its id via `useParams()`,
 * one mount-only fetch, a back link at the top.
 *
 * `TreeBoard` OWNS its tree once mounted (draft polling writes into it via
 * its own `refresh`), so this page hands it a fresh `root` on every distinct
 * `rootId` via `key={tree.id}` rather than syncing a prop into its state —
 * exactly the discipline the old inline board on the list page already
 * followed. `refreshTree` (Unit D, Task D2b) is the one exception: an
 * approved revision from the `ReviseDrawer` changes the tree from OUTSIDE
 * `TreeBoard`'s own action handlers, so nothing inside it would ever notice.
 * Rather than teaching `TreeBoard` to sync a prop into its state (the thing
 * its own docstring says not to do), `refreshTree` bumps `refreshNonce` into
 * the `key` alongside `tree.id` — a deliberate, contained remount that hands
 * `TreeBoard` the freshly-fetched tree exactly the way a fresh `rootId`
 * navigation already does, so its own draft-progress poll starts clean and
 * picks up the newly `queued` lessons. */
export default function CurriculumDetailPage() {
  const t = useTranslations("curricula");
  const locale = useLocale();
  const router = useRouter();
  const params = useParams<{ rootId: string }>();
  const rootId = params.rootId;

  const [tree, setTree] = useState<BlockNode | null>(null);
  // Held separately from `tree.title`: `TreeBoard` owns its OWN copy of the tree
  // once mounted (see its own docstring — draft polling writes into that copy,
  // not this page's), so a rename here can't just mutate `tree` and expect the
  // header inside `BlockCard` to notice. This state is what the header beside
  // the ⋯ menu actually reads; `handleRenamed` below also folds the new title
  // into `tree` and bumps `refreshNonce` so `TreeBoard` remounts with it.
  const [title, setTitle] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  // The copy this page just made, if it made one. Held so the header can offer
  // a way INTO it — the menu's own notice says a copy exists, but on this page
  // nothing else moves when one lands, and "it's in the list" is a worse answer
  // than a link. Cleared on navigation by the remount, which is correct: the
  // offer belongs to the moment it was made.
  const [duplicated, setDuplicated] = useState<CurriculumListItem | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getCurriculum(rootId)
      .then((data) => {
        if (!cancelled) {
          setTree(data);
          setTitle(data.title);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : t("boardError"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [rootId, t]);

  const handleRootDeleted = useCallback(() => {
    router.push(`/${locale}/curricula`);
  }, [router, locale]);

  /** `renameCurriculum` already returned the saved title — this just fans it
   * out to the two things that display it: this page's own header, and
   * `TreeBoard`'s tree (via a `refreshNonce` bump, the same remount trick
   * `refreshTree` below uses for a revision — `TreeBoard` never syncs a prop
   * into its state, so a plain `setTree` here would leave its course-card
   * header showing the stale name). */
  const handleRenamed = useCallback((next: string) => {
    setTitle(next);
    setTree((prev) => (prev ? { ...prev, title: next } : prev));
    setRefreshNonce((n) => n + 1);
  }, []);

  const refreshTree = useCallback(() => {
    getCurriculum(rootId)
      .then((data) => {
        setTree(data);
        setRefreshNonce((n) => n + 1);
      })
      .catch(() => {
        // Best-effort — the board's own draft-progress poll (once it exists
        // again after this remount attempt) will catch up on its own next
        // tick regardless; a failed refetch here is not worth surfacing an
        // error over on top of whatever the chat turn already reported.
      });
  }, [rootId]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link
          href={`/${locale}/curricula`}
          data-testid="curricula-back"
          className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          {t("backToList")}
        </Link>

        {tree && <ReviseDrawer rootId={rootId} tree={tree} onApplied={refreshTree} />}
      </div>

      {tree && title && (
        <div className="flex flex-wrap items-center gap-2">
          {/* `title=` for the same reason as the board's block titles: this
              truncates, and a long course name was unreadable past the
              ellipsis. */}
          <h1
            className="min-w-0 truncate text-xl font-semibold"
            data-testid="curriculum-detail-title"
            title={title}
          >
            {title}
          </h1>
          <Button
            type="button"
            size="sm"
            variant="outline"
            data-testid="curriculum-export-docx"
            disabled={exporting}
            onClick={async () => {
              setExporting(true);
              setExportError(null);
              try {
                await downloadCurriculumDocx(rootId);
              } catch {
                setExportError(t("exportError"));
              } finally {
                setExporting(false);
              }
            }}
          >
            {exporting ? <Loader2 className="animate-spin" /> : <FileDown />}
            {t("exportDocx")}
          </Button>
          <CurriculumActionsMenu
            rootId={rootId}
            title={title}
            onRenamed={handleRenamed}
            onDeleted={handleRootDeleted}
            // Stay put. The copy is the BACKUP — he keeps working on the thing
            // he was working on, and gets a link if he wants the other one.
            onDuplicated={setDuplicated}
          />
        </div>
      )}

      {duplicated && (
        <p className="text-sm text-muted-foreground" data-testid="curriculum-duplicate-link">
          <Link
            href={`/${locale}/curricula/${duplicated.id}`}
            className="underline underline-offset-4 hover:text-foreground"
          >
            {t("openTheCopy", { title: duplicated.title })}
          </Link>
        </p>
      )}
      {exportError && (
        <p role="alert" data-testid="curriculum-export-error" className="text-sm text-destructive">
          {exportError}
        </p>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground" data-testid="board-loading">
          {t("boardLoading")}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="board-error" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && !error && tree && (
        <TreeBoard
          key={`${tree.id}:${refreshNonce}`}
          root={tree}
          locale={locale}
          onRootDeleted={handleRootDeleted}
        />
      )}
    </div>
  );
}
