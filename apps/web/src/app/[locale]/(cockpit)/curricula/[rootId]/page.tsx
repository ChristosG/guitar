"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft } from "lucide-react";
import { ReviseDrawer } from "@/components/curriculum/revise-drawer";
import { TreeBoard } from "@/components/curriculum/tree-board";
import { ApiError, getCurriculum, type BlockNode } from "@/lib/api";

/** The per-curriculum board, split out of the old `curricula/page.tsx` so the
 * list page can become a plain navigable index (Unit A). Same shape as
 * `lessons/[lessonId]/page.tsx` / `students/[id]/page.tsx` / `library/
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getCurriculum(rootId)
      .then((data) => {
        if (!cancelled) setTree(data);
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
