"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft } from "lucide-react";
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
 * followed. */
export default function CurriculumDetailPage() {
  const t = useTranslations("curricula");
  const locale = useLocale();
  const router = useRouter();
  const params = useParams<{ rootId: string }>();
  const rootId = params.rootId;

  const [tree, setTree] = useState<BlockNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className="flex flex-col gap-6">
      <Link
        href={`/${locale}/curricula`}
        data-testid="curricula-back"
        className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        {t("backToList")}
      </Link>

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
        <TreeBoard key={tree.id} root={tree} locale={locale} onRootDeleted={handleRootDeleted} />
      )}
    </div>
  );
}
