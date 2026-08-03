"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { AddSourceDialog } from "@/components/library/add-source-dialog";
import { CollectionTree, type SourceGroup } from "@/components/library/collection-tree";
import { LibrarySearch } from "@/components/library/library-search";
import { NewCollectionDialog } from "@/components/library/new-collection-dialog";
import {
  ApiError,
  deleteCollection,
  deleteSource,
  getSourceProgress,
  listCollections,
  listSources,
  moveSource,
  reocrSource,
  retrySource,
  type CollectionOut,
  type SourceOut,
  type SourceProgressOut,
} from "@/lib/api";

/** How often to re-ask the server where a running OCR has got to. 2s is the same
 * cadence as `curriculum/generate-dialog.tsx`'s job poll; there is no cap and no
 * timeout, because there is nothing to time out — progress is a SERVER fact now
 * (`GET /knowledge/sources/{id}/progress`), so this loop simply stops when the
 * server says the job is done, and a tab opened an hour into a 9-minute OCR picks
 * it up mid-flight exactly as if it had started it. */
const POLL_INTERVAL_MS = 2000;

// Client component for the same reason as every other cockpit page (see
// `lib/api.ts`'s docstring): it talks to the API straight from the
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
  const [progress, setProgress] = useState<Record<string, SourceProgressOut>>({});

  // Same .then/.catch/.finally shape as e.g. `artifacts/page.tsx`'s own
  // fetch, for the same reason (every setState call stays
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

  // Which sources the SERVER says are being read right now. `ocr_active` is an
  // in-flight `GenerationJob` — not something this tab remembers — so a hard
  // reload at page 30 of 77 lands here with the same answer the tab that started
  // the job would get.
  const activeIds = useMemo(
    () => sources.filter((s) => s.ocr_active).map((s) => s.id),
    [sources],
  );
  // A stable dependency for the poll effect: `activeIds` is a fresh array on every
  // refresh (and this page refreshes every 2s while a job runs), so depending on
  // the array itself would tear down and rebuild the interval on every tick.
  const activeKey = activeIds.join(",");

  // The COMPILE blind spot, closed: `activeIds` tracks OCR jobs only, so a
  // canon compile — minutes of `compile.status === "running"` — used to show
  // "Ξεκινά…" and then sit on the stale prior state until a manual reload
  // (and a compile orphaned by a force-quit sat on its spinner forever; the
  // boot sweep now fails those, but only a refetch ever showed it). While any
  // source is compiling, re-read the list on a slow cadence; when the last
  // compile leaves `running` the key collapses to "" and this stops itself.
  const compilingKey = useMemo(
    () => sources.filter((s) => s.compile?.status === "running").map((s) => s.id).join(","),
    [sources],
  );
  useEffect(() => {
    if (compilingKey === "") return;
    const timer = setInterval(() => refresh(), POLL_INTERVAL_MS * 2);
    return () => clearInterval(timer);
  }, [compilingKey, refresh]);

  /** THE DURABLE PROGRESS LOOP. This replaced a `watchOcr` state machine that
   * lived entirely in this tab: it started only when THIS tab pressed the button,
   * and on F5 the row fell back to the source's at-rest status — `empty` — so the
   * tutor's book showed RED, "nothing was read from this source", WITH A RETRY
   * BUTTON, during the nine minutes it was actually being read. Pressing that
   * button enqueued a second job racing the first.
   *
   * Now: the server owns the truth, this only asks. When a job finishes, the next
   * tick reports `active: false` and we re-read the list once (which is what
   * flips the row to its final green/amber status and stops this loop). */
  useEffect(() => {
    if (activeKey === "") return;
    const ids = activeKey.split(",");
    let cancelled = false;

    async function tick() {
      const results = await Promise.all(
        ids.map((id) => getSourceProgress(id).catch(() => null)),
      );
      if (cancelled) return;
      const next: Record<string, SourceProgressOut> = {};
      let anyFinished = false;
      results.forEach((p) => {
        if (!p) return;
        if (p.active) next[p.source_id] = p;
        else anyFinished = true;
      });
      setProgress(next);
      if (anyFinished) refresh();
    }

    tick();
    const timer = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [activeKey, refresh]);

  /** Retry / re-read. The SERVER decides whether this starts a job at all: if one
   * is already in flight it hands back the running job's id and starts nothing
   * (`routers/library.py::_enqueue_ocr`). So a double-click is one job, and this
   * handler doesn't need to guess — it just refreshes and lets `ocr_active` tell
   * it the truth. */
  async function handleRetry(id: string) {
    setRetryingId(id);
    setError(null);
    try {
      await retrySource(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("retryError"));
    } finally {
      setRetryingId(null);
    }
  }

  /** Re-read a book with the vision model. Shares `retryingId` with `handleRetry`
   * on purpose: both disable the same row's buttons while a request is in flight,
   * and a row can only be doing one of them at a time — the server's in-flight
   * guard makes the pair mutually exclusive anyway (`_enqueue_ocr`).
   *
   * Refreshes and then does nothing else: the durable progress loop above picks
   * the job up from `ocr_active` on its next tick, exactly as it does for a job
   * some other tab started. This handler has no idea how long the run is, and
   * that is the point — an 8-hour read is the server's business, not this tab's. */
  async function handleReocr(id: string) {
    setRetryingId(id);
    setError(null);
    try {
      await reocrSource(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("reocrError"));
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
          <AddSourceDialog onCreated={refresh} />
        </div>
      </div>

      {/* Search sits ABOVE the shelves, not in the top bar: it searches THIS —
          the books on this page — and a hit opens the Reader at the page it came
          from. See `LibrarySearch`. */}
      {!loading && sources.length > 0 && <LibrarySearch locale={locale} />}

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

      {/* The tree renders whenever there ARE sources — a transient refresh
          failure (e.g. the automatic one when an OCR job finishes) used to
          unmount the entire library behind one error line, even though the
          state still held perfectly renderable data. */}
      {!loading && sources.length > 0 && (
        <CollectionTree
          groups={groups}
          locale={locale}
          collectionOptions={collectionOptions}
          progress={progress}
          retryingId={retryingId}
          deletingId={deletingId}
          deletingCollectionId={deletingCollectionId}
          movingId={movingId}
          onRetry={handleRetry}
          onReocr={handleReocr}
          onDelete={handleDelete}
          onDeleteCollection={handleDeleteCollection}
          onMove={handleMove}
          onChanged={refresh}
        />
      )}
    </div>
  );
}
