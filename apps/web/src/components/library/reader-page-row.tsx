"use client";

import { useTranslations } from "next-intl";
import { ReaderPane } from "@/components/library/reader-pane";
import type { PageDetailOut } from "@/lib/api";

interface ReaderPageRowProps {
  pageNo: number;
  /** `PageDetailOut` once its own fetch has resolved, `undefined` until
   * then — see the reader page's own docstring on why every page's text is
   * fetched up front (cheap JSON) while only its scan image is lazy. */
  detail?: PageDetailOut;
}

/** One page's slot in the continuous-scroll Reader (Plan 12 Task 4 / G4).
 *
 * The page-marker divider ALWAYS renders immediately — from the manifest
 * (`listSourcePages`) alone, a single cheap request — regardless of whether
 * this page's own `detail` (text + scan url, a separate per-page fetch) has
 * arrived yet. That marker is:
 *   1. the scroll target `?page=N` deep-linking lands on
 *      (`id="reader-page-{n}"` — the reader page's own effect calls
 *      `document.getElementById(...)?.scrollIntoView()` against exactly
 *      this id, so it must exist the instant the manifest does, not wait
 *      on 77 individual page fetches);
 *   2. what the scrollspy watches (`data-reader-marker`) to know which page
 *      is currently on screen for the "Page N of {total}" indicator.
 *
 * Until `detail` arrives, this renders a calm skeleton in its place rather
 * than nothing — the row's HEIGHT existing up front (even if approximate)
 * is what keeps the deep-link scroll target from jumping around too badly
 * as later rows above it swap in their real content.
 */
export function ReaderPageRow({ pageNo, detail }: ReaderPageRowProps) {
  const t = useTranslations("library.reader");

  return (
    <section className="flex flex-col gap-3" data-testid={`page-row-${pageNo}`}>
      <div
        id={`reader-page-${pageNo}`}
        data-reader-marker={pageNo}
        data-testid={`page-marker-${pageNo}`}
        className="flex scroll-mt-6 items-center gap-3 text-xs font-medium uppercase tracking-wider text-muted-foreground/70"
      >
        <span aria-hidden="true" className="h-px flex-1 bg-border" />
        {t("pageMarker", { page: pageNo })}
        <span aria-hidden="true" className="h-px flex-1 bg-border" />
      </div>

      {detail ? (
        <ReaderPane page={detail} />
      ) : (
        <div className="grid gap-6 md:grid-cols-2" data-testid={`page-skeleton-${pageNo}`}>
          <div className="h-[50vh] animate-pulse rounded-xl bg-muted/60 ring-1 ring-foreground/10" aria-hidden="true" />
          <div className="h-[50vh] animate-pulse rounded-xl bg-muted/40 ring-1 ring-foreground/10" aria-hidden="true" />
        </div>
      )}
    </section>
  );
}
