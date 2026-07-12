"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ReaderPane } from "@/components/library/reader-pane";
import { SelectionAction } from "@/components/library/selection-action";
import { ApiError, getSourcePage, type PageDetailOut } from "@/lib/api";

/** The Reader: THE screen this project exists for (Plan 9 Task 9). The
 * tutor's book is a raster scan; this shows the real scanned page beside
 * its selectable OCR text so he can trust the transcription (spec D1) and
 * author a lesson from a passage (spec — see `SelectionAction`). Client
 * component, `useParams()` for the dynamic segment — same shape as
 * `students/[id]/page.tsx`'s own docstring on why (a plain "use client"
 * default-export page, not an async server component awaiting the
 * now-Promise `params`).
 *
 * SPEC D5 — NO OCR EDITING, ANYWHERE ON THIS PAGE. There is no "fix OCR"
 * button, no contenteditable, no edit mode, no save-text call. The machine
 * retries a failed page; the tutor is never handed that chore. Do not add
 * one — `reader.spec.ts`'s "there is NO OCR editing affordance" test is
 * binding.
 *
 * Deliberately does NOT call `listSourcePages` (the Library home's own page
 * strip) — this reader only ever needs one page at a time, and
 * `GET .../pages/{n}` already returns `total_pages` for the prev/next
 * bounds, so there is nothing a second list call would add here. */
export default function LibraryReaderPage() {
  const t = useTranslations("library.reader");
  const locale = useLocale();
  const router = useRouter();
  const params = useParams<{ sourceId: string }>();
  const searchParams = useSearchParams();
  const sourceId = params.sourceId;

  const [pageNo, setPageNo] = useState(() => Math.max(1, Number(searchParams.get("page")) || 1));
  const [page, setPage] = useState<PageDetailOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const textRef = useRef<HTMLDivElement>(null);

  // Same .then/.catch/.finally shape as every other cockpit page's fetch
  // callback (see e.g. `students/[id]/page.tsx`'s `fetchDetail`), for the
  // same reason — every `setState` call stays lexically inside a callback
  // rather than a bare statement in the function body (what
  // react-hooks/set-state-in-effect actually checks for). Also means a page
  // turn never flashes a "Loading…" over the current page — the old page's
  // scan/text just stay put until the new one is ready, same "never flicker
  // a calm surface" posture as `library/page.tsx`'s own `refresh()`.
  const fetchPage = useCallback(
    (n: number) => {
      return getSourcePage(sourceId, n)
        .then((p) => {
          setPage(p);
          setError(null);
        })
        .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
        .finally(() => setLoading(false));
    },
    [sourceId, t],
  );

  useEffect(() => {
    fetchPage(pageNo);
  }, [fetchPage, pageNo]);

  // Keeps the URL's `?page=` in sync (so the page a tutor is on survives a
  // refresh/share) without waiting on a `searchParams` round trip to drive
  // the actual fetch above — `pageNo` state is the single source of truth
  // for what's on screen; the URL just follows it.
  function goToPage(n: number) {
    setPageNo(n);
    router.replace(`/${locale}/library/${sourceId}?page=${n}`, { scroll: false });
  }

  const total = page?.total_pages ?? null;
  const canPrev = pageNo > 1;
  const canNext = total != null && pageNo < total;

  return (
    <div className="flex flex-col gap-6 pb-20">
      <Link
        href={`/${locale}/library`}
        data-testid="reader-back"
        className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        {t("back")}
      </Link>

      {loading && !page && (
        <p className="text-sm text-muted-foreground" data-testid="reader-loading">
          {t("loading")}
        </p>
      )}

      {error && (
        <p role="alert" data-testid="reader-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {page && (
        <>
          <ReaderPane page={page} textRef={textRef} />

          <div className="flex items-center justify-center gap-4">
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              disabled={!canPrev}
              data-testid="page-prev"
              aria-label={t("prev")}
              onClick={() => goToPage(pageNo - 1)}
            >
              <ChevronLeft />
            </Button>
            <span
              data-testid="page-indicator"
              className="min-w-[6rem] text-center text-sm tabular-nums text-muted-foreground"
            >
              {t("pageOf", { page: pageNo, total: total ?? pageNo })}
            </span>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              disabled={!canNext}
              data-testid="page-next"
              aria-label={t("next")}
              onClick={() => goToPage(pageNo + 1)}
            >
              <ChevronRight />
            </Button>
          </div>

          {/* `key={pageNo}` forces a fresh instance per page — see
              `SelectionAction`'s own docstring on why that's the reset
              mechanism instead of an effect. */}
          <SelectionAction key={pageNo} sourceId={sourceId} pageNo={pageNo} textRef={textRef} />
        </>
      )}
    </div>
  );
}
