"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { AddSourceDialog } from "@/components/library/add-source-dialog";
import { CollectionTree, type SourceGroup } from "@/components/library/collection-tree";
import { NewCollectionDialog } from "@/components/library/new-collection-dialog";
import {
  ApiError,
  deleteCollection,
  deleteSource,
  getJob,
  listCollections,
  listSourcePages,
  listSources,
  moveSource,
  retrySource,
  type CollectionOut,
  type SourceOut,
} from "@/lib/api";
import type { OcrProgress } from "@/components/library/source-row";

/** Poll cadence + cap while this tab is watching an OCR job it just started
 * (see `watchOcr` below) — same convention as `curriculum/generate-dialog.
 * tsx`'s job poll, but a wider cap: OCR runs page-by-page against a vision
 * model, so a real book can take much longer than one guided-JSON call. 300
 * * 2s = 10 minutes; exceeding it doesn't cancel the job (it keeps running
 * server-side) — this tab just stops narrating it live and falls back to
 * whatever `listSources()` reports on the next manual refresh. */
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 300;

// Client component for the same reason as every other cockpit page (see
// `students/page.tsx`'s docstring): it talks to the API straight from the
// browser, which is also what makes it visible to Playwright's `page.route`.
export default function LibraryPage() {
  const t = useTranslations("library");
  const locale = useLocale();

  const [sources, setSources] = useState<SourceOut[]>([]);
  const [collections, setCollections] = useState<CollectionOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deletingCollectionId, setDeletingCollectionId] = useState<string | null>(null);
  const [movingId, setMovingId] = useState<string | null>(null);
  const [ocrProgress, setOcrProgress] = useState<Record<string, OcrProgress>>({});

  // Same .then/.catch/.finally shape as e.g. `students/page.tsx`'s own
  // `fetchStudents`, for the same reason (every setState call stays
  // lexically inside a callback rather than a bare statement).
  const fetchAll = useCallback(() => {
    return Promise.all([listSources(), listCollections()])
      .then(([s, c]) => {
        setSources(s);
        setCollections(c);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Deliberately does NOT flip `loading` back to true: this page refreshes
  // itself after nearly every action (retry/move/delete/add), and swapping
  // the whole list out for a "Loading…" line each time would flicker and
  // undercut the "calm home base" this redesign exists to be. Only the very
  // first mount (the `useState(true)` above) shows the loading line; every
  // refresh after that just quietly swaps the data in once it arrives.
  const refresh = useCallback(() => {
    setError(null);
    return fetchAll();
  }, [fetchAll]);

  /** Polls `GET /jobs/{jobId}` + `GET /knowledge/sources/{id}/pages` until
   * the job reaches a terminal status (or the cap above is hit), updating
   * `ocrProgress[sourceId]` on each tick so the row can show "reading page N
   * of M" live. This is the one place this tab can ever legitimately show an
   * "ocr_running"-style state — `KnowledgeSource.status` itself never
   * becomes that (see `lib/api.ts`'s `SourceStatus` docstring), only
   * `Page.status` does, and only a durable job/poll can see that in
   * progress rather than at rest. */
  const watchOcr = useCallback(
    (sourceId: string, jobId: string) => {
      async function poll() {
        for (let i = 0; i < MAX_POLLS; i++) {
          try {
            const [job, pages] = await Promise.all([getJob(jobId), listSourcePages(sourceId)]);
            const ready = pages.filter((p) => p.status === "ready").length;
            setOcrProgress((prev) => ({ ...prev, [sourceId]: { ready, total: pages.length } }));
            if (job.status === "succeeded" || job.status === "failed") break;
          } catch {
            break; // fail closed — stop watching silently; the next refresh shows the truth
          }
          await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        }
        setOcrProgress((prev) => {
          const next = { ...prev };
          delete next[sourceId];
          return next;
        });
        refresh();
      }
      poll();
    },
    [refresh],
  );

  async function handleRetry(id: string) {
    setRetryingId(id);
    setError(null);
    try {
      const { job_id } = await retrySource(id);
      if (job_id) watchOcr(id, job_id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("retryError"));
    } finally {
      setRetryingId(null);
    }
  }

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteSource(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("deleteError"));
    } finally {
      setDeletingId(null);
    }
  }

  // The folder goes; its sources land in Unfiled (SET NULL, server-side) —
  // which is why this just `refresh()`es like every other mutation here
  // instead of trying to re-file anything client-side.
  async function handleDeleteCollection(id: string) {
    setDeletingCollectionId(id);
    setError(null);
    try {
      await deleteCollection(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("deleteCollectionError"));
    } finally {
      setDeletingCollectionId(null);
    }
  }

  async function handleMove(id: string, collectionId: string | null) {
    setMovingId(id);
    setError(null);
    try {
      await moveSource(id, collectionId);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("moveError"));
    } finally {
      setMovingId(null);
    }
  }

  // Grouped by `collection_id`, with a synthetic "Unfiled" bucket for
  // `null` — real collections first (already name-sorted by the API), then
  // Unfiled last, always shown (spec: it's the one bucket that's always
  // there, like a filing cabinet's own bottom drawer).
  const groups: SourceGroup[] = useMemo(() => {
    const byCollection = new Map<string, SourceOut[]>();
    const unfiled: SourceOut[] = [];
    for (const s of sources) {
      if (s.collection_id) {
        const arr = byCollection.get(s.collection_id) ?? [];
        arr.push(s);
        byCollection.set(s.collection_id, arr);
      } else {
        unfiled.push(s);
      }
    }
    const named = collections.map((c) => ({
      key: c.id,
      testId: `collection-${c.name}`,
      name: c.name,
      collectionId: c.id,
      sources: byCollection.get(c.id) ?? [],
    }));
    return [
      ...named,
      { key: "unfiled", testId: "collection-unfiled", name: t("unfiled"), collectionId: null, sources: unfiled },
    ];
  }, [collections, sources, t]);

  const collectionOptions = useMemo(
    () => [{ id: null as string | null, name: t("unfiled") }, ...collections.map((c) => ({ id: c.id, name: c.name }))],
    [collections, t],
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="library-heading">
            {t("heading")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subheading")}</p>
        </div>
        <div className="flex items-center gap-2">
          <NewCollectionDialog onCreated={refresh} />
          <AddSourceDialog onCreated={refresh} onOcrStarted={watchOcr} />
        </div>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground" data-testid="library-loading">
          {t("loading")}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="library-error" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && !error && sources.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="library-empty">
          {t("empty")}
        </p>
      )}

      {!loading && !error && sources.length > 0 && (
        <CollectionTree
          groups={groups}
          locale={locale}
          collectionOptions={collectionOptions}
          ocrProgress={ocrProgress}
          retryingId={retryingId}
          deletingId={deletingId}
          deletingCollectionId={deletingCollectionId}
          movingId={movingId}
          onRetry={handleRetry}
          onDelete={handleDelete}
          onDeleteCollection={handleDeleteCollection}
          onMove={handleMove}
        />
      )}
    </div>
  );
}
