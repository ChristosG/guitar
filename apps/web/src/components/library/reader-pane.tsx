"use client";

import { useTranslations } from "next-intl";
import { AlertTriangle, Loader2 } from "lucide-react";
import { LazyPageImage } from "@/components/library/lazy-page-image";
import type { PageDetailOut } from "@/lib/api";

/** `Page.status` values that mean "there is no finished text to show yet" —
 * mirrors the API's `PAGE_STATUSES` (`schemas/library.py`) minus "ready".
 * Each gets its own honest line in the text pane (see `pageStatus` below) —
 * same "never show an empty pane, say what's actually true" posture as
 * `source-row.tsx`'s `BROKEN_STATUSES` handling, just at page granularity. */
const INCOMPLETE_STATUSES = new Set(["pending", "ocr_running", "failed", "empty"]);

interface ReaderPaneProps {
  page: PageDetailOut;
}

/** One page's two-column reading surface inside the continuous-scroll
 * Reader (Plan 12 Task 4 / G4 — this used to be the whole Reader, paged one
 * at a time; `ReaderPageRow` now stacks one of these per page): the real
 * scanned page on the left (spec D1 — a scan is the trust mechanism, not
 * decoration; this book is full of amp photos and knob diagrams a
 * transcript alone can't carry), lazy-loaded via `LazyPageImage` (see its
 * own docstring — 77 scans is too much JPEG to render all at once), and the
 * OCR'd text on the right, selectable but never editable (spec D5 — see
 * this component's sibling `SelectionAction` and the reader page's own
 * docstring for why there is no "fix OCR" affordance anywhere near this).
 *
 * The text pane carries `data-reader-text-page={page.page_no}` — the ONLY
 * thing that makes a `window.getSelection()` landing inside it count as
 * "page {page_no}" for `SelectionAction`'s cross-page range detection (a
 * selection that starts in one page's pane and ends in another's resolves
 * to `{page_from, page_to}` by reading this attribute off both ends — see
 * that component's own docstring).
 *
 * `image_url` is only rendered when non-null — a text/url source has
 * exactly one `Page` with no scan at all (spec D2), and this deliberately
 * shows no broken-image placeholder for that case: the grid just collapses
 * to a single, full-width text column instead. */
export function ReaderPane({ page }: ReaderPaneProps) {
  const t = useTranslations("library.reader");
  const hasScan = page.image_url != null;
  const showStatusLine = INCOMPLETE_STATUSES.has(page.status);

  return (
    // `items-start` (not the grid default `stretch`) — a page whose OCR
    // text runs long must not stretch the scan's column to match it: a
    // grid row sized by its tallest cell, with the scan's own `flex
    // items-center` centering it inside that stretched cell, otherwise
    // floats the scan far down a mostly-empty column (found live, testing
    // this Task 4 rewrite against the real 77-page book — page 21's OCR
    // text alone is ~1900px tall). `md:sticky` then keeps the scan in view
    // WHILE that long column of text scrolls past beside it — the reading
    // surface Chris is meant to trust (spec D1) stays visible for as long
    // as its own row is on screen, not just its top sliver.
    <div className={hasScan ? "grid gap-6 md:grid-cols-2 md:items-start" : "grid gap-6"}>
      {hasScan && (
        <div className="md:sticky md:top-6">
          <LazyPageImage src={page.image_url!} pageNo={page.page_no} />
        </div>
      )}

      <div
        data-testid={`page-text-${page.page_no}`}
        data-reader-text-page={page.page_no}
        className="rounded-xl bg-card p-6 font-serif text-[1.05rem] leading-relaxed text-card-foreground ring-1 ring-foreground/10 selection:bg-primary/20"
      >
        {showStatusLine ? (
          <p className="flex items-center gap-2 font-sans text-sm text-muted-foreground">
            {page.status === "ocr_running" ? (
              <Loader2 className="size-3.5 shrink-0 animate-spin" />
            ) : page.status === "failed" ? (
              <AlertTriangle className="size-3.5 shrink-0 text-destructive" />
            ) : null}
            {t(`pageStatus.${page.status}`)}
          </p>
        ) : page.text ? (
          <p className="whitespace-pre-wrap">{page.text}</p>
        ) : (
          <p className="font-sans text-sm text-muted-foreground">{t("noText")}</p>
        )}
      </div>
    </div>
  );
}
