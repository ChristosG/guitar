"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft } from "lucide-react";
import { ReaderPageRow } from "@/components/library/reader-page-row";
import { SelectionAction } from "@/components/library/selection-action";
import {
  ApiError,
  getSourcePage,
  listSourcePages,
  type PageDetailOut,
  type PageSummary,
} from "@/lib/api";

/** How many of the manifest's pages are fetched (their JSON text+scan-url
 * detail, NOT the scan image itself — see `LazyPageImage`) at once. Each
 * request is cheap (small JSON, no image bytes), but 77 of them fired
 * simultaneously is still 77 open connections; this is a courtesy cap on
 * the browser's/API's connection pool, not a correctness requirement. */
const DETAIL_FETCH_CONCURRENCY = 6;

/** The Reader: THE screen this project exists for (Plan 9 Task 9), rebuilt
 * as a CONTINUOUS SCROLL for Plan 12 Task 4 (G4). Chris, having actually
 * used the `< >` pager: "i cant select more than one page.. maybe we need a
 * scrolling way of pages and not the < > buttons?" — he was right on both
 * counts: the pager made a passage-spanning selection impossible, and a
 * single highlighted line was too thin a thing to ground a lesson in.
 *
 * The whole book's pages stack vertically (scan left, OCR text right, per
 * `ReaderPageRow`/`ReaderPane`), so a selection can now cross a page
 * boundary — `SelectionAction` resolves that into `{page_from, page_to}`.
 *
 * PERFORMANCE: 77 real scans at ~250-350KB each is ~25MB — rendering every
 * `<img>` up front would make the whole book crawl in. The split this file
 * makes: the MANIFEST (`listSourcePages` — just `{page_no, status}` per
 * page) loads first and renders every page's marker + a skeleton
 * immediately; each page's TEXT (`getSourcePage` — cheap JSON, no image
 * bytes) is then fetched for every page up front, `DETAIL_FETCH_CONCURRENCY`
 * at a time, deep-link target first; only the SCAN IMAGE is deferred to
 * `LazyPageImage`'s `IntersectionObserver`, per page, near the viewport.
 *
 * DEEP-LINKING (`?page=N`): every page's marker (`id="reader-page-{n}"`)
 * exists the instant the manifest does, so scrolling to it only ever waits
 * on that one cheap request — not on all 77 pages' text/scans having
 * loaded. Citation chips all over the app (lesson provenance, chat
 * citations, curriculum interview preview) depend on this link shape
 * (`/library/{sourceId}?page={n}`) continuing to work exactly as before.
 *
 * SPEC D5 — NO OCR EDITING, ANYWHERE ON THIS PAGE. There is no "fix OCR"
 * button, no contenteditable, no edit mode, no save-text call. The machine
 * retries a failed page; the tutor is never handed that chore. Do not add
 * one — `reader.spec.ts`'s "there is NO OCR editing affordance" test is
 * binding. */
export default function LibraryReaderPage() {
  const t = useTranslations("library.reader");
  const locale = useLocale();
  const params = useParams<{ sourceId: string }>();
  const searchParams = useSearchParams();
  const sourceId = params.sourceId;
  const targetPage = Math.max(1, Number(searchParams.get("page")) || 1);

  const [manifest, setManifest] = useState<PageSummary[] | null>(null);
  const [details, setDetails] = useState<Record<number, PageDetailOut>>({});
  const [manifestLoading, setManifestLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(targetPage);
  const scrollRef = useRef<HTMLDivElement>(null);
  const hasScrolledToTargetRef = useRef(false);

  // The manifest — every page number + OCR status this source has, in one
  // cheap request. Everything below (which pages exist at all, what the
  // deep-link scrolls to, the total-pages count) is derived from this, not
  // from any per-page fetch.
  useEffect(() => {
    let cancelled = false;
    listSourcePages(sourceId)
      .then((pages) => {
        if (!cancelled) setManifest(pages);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : t("error"));
      })
      .finally(() => {
        if (!cancelled) setManifestLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sourceId, t]);

  // Every page's text+scan-url, fetched once the manifest is known — see
  // this file's own docstring on why this is safe to do for all of them
  // (only the scan IMAGE is lazy). The deep-linked target page is queued
  // first so the tutor's actual destination is ready as fast as possible;
  // the rest follow in top-to-bottom order behind a small concurrency cap.
  useEffect(() => {
    if (!manifest) return;
    let cancelled = false;
    const order = [
      targetPage,
      ...manifest.map((p) => p.page_no).filter((n) => n !== targetPage),
    ];
    let cursor = 0;

    async function worker() {
      while (cursor < order.length) {
        const pageNo = order[cursor++];
        try {
          const detail = await getSourcePage(sourceId, pageNo);
          if (!cancelled) setDetails((prev) => ({ ...prev, [pageNo]: detail }));
        } catch {
          // One page failing to load its own detail isn't fatal to the rest
          // of the book — it just stays a skeleton row rather than taking
          // the whole Reader down.
        }
      }
    }

    const workers = Array.from(
      { length: Math.min(DETAIL_FETCH_CONCURRENCY, order.length) },
      worker,
    );
    Promise.all(workers);
    return () => {
      cancelled = true;
    };
  }, [manifest, sourceId, targetPage]);

  // `?page=N` deep-link: scroll to that page's marker once the manifest
  // exists (every marker renders immediately regardless of load status —
  // see `ReaderPageRow`). Runs once per mount; a short-delay retry corrects
  // for the layout shift still-loading rows above the target cause as their
  // skeletons swap for real content.
  useEffect(() => {
    if (!manifest || hasScrolledToTargetRef.current) return;
    hasScrolledToTargetRef.current = true;
    const scroll = () =>
      document.getElementById(`reader-page-${targetPage}`)?.scrollIntoView({ block: "start" });
    scroll();
    const retry = setTimeout(scroll, 400);
    return () => clearTimeout(retry);
  }, [manifest, targetPage]);

  // Scrollspy: which page marker is nearest the top of the viewport right
  // now, so the "Page N of {total}" badge stays honest as he scrolls
  // instead of only ever reflecting where the deep-link landed.
  useEffect(() => {
    if (!manifest) return;
    const root = scrollRef.current;
    if (!root || typeof IntersectionObserver === "undefined") return;

    const markers = Array.from(root.querySelectorAll<HTMLElement>("[data-reader-marker]"));
    if (markers.length === 0) return;

    const visible = new Set<number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const pageNo = Number((entry.target as HTMLElement).dataset.readerMarker);
          if (entry.isIntersecting) visible.add(pageNo);
          else visible.delete(pageNo);
        }
        if (visible.size > 0) setCurrentPage(Math.min(...visible));
      },
      { rootMargin: "-10% 0px -80% 0px", threshold: 0 },
    );
    markers.forEach((marker) => observer.observe(marker));
    return () => observer.disconnect();
  }, [manifest]);

  const total = manifest?.length ?? null;

  return (
    <div className="flex flex-col gap-6 pb-20">
      <div className="flex items-center justify-between gap-4">
        <Link
          href={`/${locale}/library`}
          data-testid="reader-back"
          className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          {t("back")}
        </Link>

        {total != null && (
          <span
            data-testid="page-indicator"
            className="rounded-full border border-border bg-card px-3 py-1 text-xs tabular-nums text-muted-foreground"
          >
            {t("pageOf", { page: currentPage, total })}
          </span>
        )}
      </div>

      {manifestLoading && !manifest && (
        <p className="text-sm text-muted-foreground" data-testid="reader-loading">
          {t("loading")}
        </p>
      )}

      {error && (
        <p role="alert" data-testid="reader-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {manifest && (
        <div ref={scrollRef} data-testid="reader-scroll" className="flex flex-col gap-8">
          {manifest.map((p) => (
            <ReaderPageRow key={p.page_no} pageNo={p.page_no} detail={details[p.page_no]} />
          ))}
        </div>
      )}

      <SelectionAction sourceId={sourceId} />
    </div>
  );
}
