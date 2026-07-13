"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { BookOpen } from "lucide-react";
import { getSource } from "@/lib/api";

interface ProvenanceChipProps {
  sourceId: string;
  pageNo: number;
  /** The end of the drafted-from range (G4, Plan 12 Task 4) — omitted or
   * equal to `pageNo` for a single-page selection, in which case the chip
   * reads exactly as it always has ("p.21"). A real range reads "p.21–23"
   * instead, and still links to the range's START page. */
  pageTo?: number;
  locale: string;
}

// Module-level cache, keyed by sourceId: Chris's whole library is a handful
// of books, so many lessons/rows on one screen (the lessons list) can cite
// the exact same source — this avoids firing an identical `GET /knowledge/
// sources/{id}` once per chip on screen. Process-lifetime only (no
// eviction), same "small PoC, no need for a real cache" posture as the rest
// of this app.
const titleCache = new Map<string, Promise<string>>();
function fetchSourceTitle(sourceId: string): Promise<string> {
  let cached = titleCache.get(sourceId);
  if (!cached) {
    cached = getSource(sourceId).then((s) => s.title);
    titleCache.set(sourceId, cached);
  }
  return cached;
}

/** THE PAYOFF of the whole Library project (Plan 10 Task 4's brief, verbatim):
 * a small citation chip — "from *Getting Great Guitar Sounds*, p.21" — that
 * links straight into the Reader at the exact page a lesson was drafted
 * from (`/library/{sourceId}?page={pageNo}`, the `?page=` support Plan 9
 * built — see `library/[sourceId]/page.tsx`). The `href` is built
 * synchronously from props alone, never gated on the title fetch below, so
 * the link is live and correctly targeted the instant this mounts — even
 * before the book's own title has loaded (`chipLoading` covers that brief
 * window with just the page number). */
export function ProvenanceChip({ sourceId, pageNo, pageTo, locale }: ProvenanceChipProps) {
  const t = useTranslations("lessons.provenance");
  const [title, setTitle] = useState<string | null>(null);
  const isRange = pageTo != null && pageTo !== pageNo;

  useEffect(() => {
    let cancelled = false;
    fetchSourceTitle(sourceId)
      .then((s) => {
        if (!cancelled) setTitle(s);
      })
      .catch(() => {
        // fail quiet — the chip still links correctly with just the page
        // number (`chipLoading`); a missing/removed source is not this
        // chip's problem to report on.
      });
    return () => {
      cancelled = true;
    };
  }, [sourceId]);

  return (
    <Link
      href={`/${locale}/library/${sourceId}?page=${pageNo}`}
      data-testid="provenance-chip"
      className="inline-flex w-fit items-center gap-1.5 rounded-full border border-primary/30 bg-primary/5 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:bg-primary/10 hover:text-foreground"
    >
      <BookOpen className="size-3.5 shrink-0 text-primary" />
      <span>
        {isRange
          ? title
            ? t("chipRange", { source: title, from: pageNo, to: pageTo })
            : t("chipRangeLoading", { from: pageNo, to: pageTo })
          : title
            ? t("chip", { source: title, page: pageNo })
            : t("chipLoading", { page: pageNo })}
      </span>
    </Link>
  );
}
