"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { apiMediaUrl } from "@/lib/api";

interface LazyPageImageProps {
  /** Server-relative media path (`PageDetailOut.image_url`) — resolved via
   * `apiMediaUrl` exactly like the old single-page Reader did. */
  src: string;
  pageNo: number;
}

/** One scanned page's `<img>` — mounted WITHOUT a `src` until its slot is
 * near the viewport (Plan 12 Task 4 / G4). The continuous-scroll Reader
 * stacks every page of the tutor's book at once; his real book is 77 scans
 * at ~250-350KB each, ~25MB total, and a naive `<img>` per page would make
 * the whole thing crawl on load. `IntersectionObserver` with a generous
 * `rootMargin` swaps the placeholder for the real image just BEFORE it
 * would enter view, not the instant it does — same "prefetch just ahead of
 * the read position" reasoning the AlphaTab lazy-mount notes in the
 * progress ledger use for tab rendering.
 *
 * Deliberately does NOT unmount the image again once it has loaded — a book
 * is read roughly linearly top to bottom, and re-fetching a scan the tutor
 * just scrolled past would cost more (a re-request, a flash of placeholder)
 * than the modest memory of leaving a handful of already-seen JPEGs
 * decoded.
 */
export function LazyPageImage({ src, pageNo }: LazyPageImageProps) {
  const t = useTranslations("library.reader");
  const slotRef = useRef<HTMLDivElement>(null);
  // Lazy initializer (not an effect) for the no-IO fallback — setting state
  // synchronously inside an effect body cascades an extra render for
  // everyone, whereas an initializer runs once, before first paint.
  const [inView, setInView] = useState(() => typeof IntersectionObserver === "undefined");

  useEffect(() => {
    if (inView) return; // already showing the real image (no-IO fallback)
    const node = slotRef.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setInView(true);
          observer.disconnect();
        }
      },
      { rootMargin: "800px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [inView]);

  return (
    <div
      ref={slotRef}
      data-testid="page-scan-slot"
      className="flex min-h-[320px] items-center justify-center rounded-xl bg-neutral-900 p-3 ring-1 ring-foreground/10 dark:bg-neutral-950"
    >
      {inView ? (
        // eslint-disable-next-line @next/next/no-img-element -- external, cross-origin API host; next/image can't optimize it without remote-pattern config this PoC doesn't carry
        <img
          data-testid="page-scan"
          src={apiMediaUrl(src)}
          alt={t("scanAlt", { page: pageNo })}
          className="max-h-[75vh] w-auto rounded-md shadow-lg"
        />
      ) : (
        <div
          data-testid="page-scan-placeholder"
          aria-hidden="true"
          className="h-[60vh] w-full max-w-sm animate-pulse rounded-md bg-neutral-800/60"
        />
      )}
    </div>
  );
}
