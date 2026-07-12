"use client";

import type { RefObject } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, Loader2 } from "lucide-react";
import { apiMediaUrl, type PageDetailOut } from "@/lib/api";

/** `Page.status` values that mean "there is no finished text to show yet" —
 * mirrors the API's `PAGE_STATUSES` (`schemas/library.py`) minus "ready".
 * Each gets its own honest line in the text pane (see `pageStatus` below) —
 * same "never show an empty pane, say what's actually true" posture as
 * `source-row.tsx`'s `BROKEN_STATUSES` handling, just at page granularity. */
const INCOMPLETE_STATUSES = new Set(["pending", "ocr_running", "failed", "empty"]);

interface ReaderPaneProps {
  page: PageDetailOut;
  /** Attached to the text pane's own DOM node so `SelectionAction` (a
   * sibling, not a child — it needs to render its own floating bar outside
   * this grid) can tell whether a live `window.getSelection()` actually
   * lies inside the readable text, not e.g. the page-scan image's alt text
   * or the surrounding chrome. */
  textRef: RefObject<HTMLDivElement | null>;
}

/** The two-column reading surface itself: the real scanned page on the left
 * (spec D1 — a scan is the trust mechanism, not decoration; this book is
 * full of amp photos and knob diagrams a transcript alone can't carry) and
 * the OCR'd text on the right, selectable but never editable (spec D5 — see
 * this component's sibling `SelectionAction` and the reader page's own
 * docstring for why there is no "fix OCR" affordance anywhere near this).
 *
 * `image_url` is only rendered when non-null — a text/url source has
 * exactly one `Page` with no scan at all (spec D2), and this deliberately
 * shows no broken-image placeholder for that case: the grid just collapses
 * to a single, full-width text column instead. */
export function ReaderPane({ page, textRef }: ReaderPaneProps) {
  const t = useTranslations("library.reader");
  const hasScan = page.image_url != null;
  const showStatusLine = INCOMPLETE_STATUSES.has(page.status);

  return (
    <div className={hasScan ? "grid gap-6 md:grid-cols-2" : "grid gap-6"}>
      {hasScan && (
        <figure className="flex flex-col gap-2">
          <div className="flex items-center justify-center rounded-xl bg-neutral-900 p-3 ring-1 ring-foreground/10 dark:bg-neutral-950">
            {/* eslint-disable-next-line @next/next/no-img-element -- external, cross-origin API host; next/image can't optimize it without remote-pattern config this PoC doesn't carry */}
            <img
              data-testid="page-scan"
              src={apiMediaUrl(page.image_url!)}
              alt={t("scanAlt", { page: page.page_no })}
              className="max-h-[75vh] w-auto rounded-md shadow-lg"
            />
          </div>
        </figure>
      )}

      <div
        data-testid="page-text"
        ref={textRef}
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
