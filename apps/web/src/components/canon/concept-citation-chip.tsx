"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { BookOpen, Image as ImageIcon } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ConceptCitation } from "@/lib/api";

/** A concept citation as a chip that deep-links into the Reader — the SAME idiom
 * as `components/lessons/provenance-chip.tsx` (rounded pill, BookOpen, the exact
 * `/library/{sourceId}?page={page}` deep-link the Reader's `?page=` contract
 * pins), adapted for the two things a canon citation needs that a lesson one does
 * not:
 *
 *   1. THE GAP-AWARE PAGE LABEL. The server already rendered `pages_label`
 *      ("p.113" / "pp.57-58" / "pp.110-112, 118") and deliberately does NOT bridge
 *      gaps (`canon/render._page_ranges` — a range the claim doesn't support would
 *      be a page the tutor clicks and finds nothing on). So we show that string
 *      verbatim rather than re-deriving one, and link to the FIRST cited page.
 *
 *   2. THE [FIGURE] CONTRACT. `grounding: "figure"` means this citation is OUR
 *      description of a picture/diagram, not the author's own words — citable,
 *      never quotable. It gets a distinct icon and a tooltip that says so, because
 *      quoting our caption as the author's sentence (with his real page number on
 *      it) is the exact fabrication this whole codebase is architected against.
 */
export function ConceptCitationChip({
  citation,
  locale,
}: {
  citation: ConceptCitation;
  locale: string;
}) {
  const t = useTranslations("canon");
  const isFigure = citation.grounding === "figure";
  // Link to the first cited page; `pages` is validated + sorted server-side, so
  // `pages[0]` is a real page of a real book. Fall back to the source with no
  // `?page=` if a citation somehow arrived with no pages.
  const firstPage = citation.pages[0];
  const href =
    firstPage != null
      ? `/${locale}/library/${citation.source_id}?page=${firstPage}`
      : `/${locale}/library/${citation.source_id}`;

  const inner = (
    <>
      {isFigure ? (
        <ImageIcon className="size-3.5 shrink-0 text-amber-600 dark:text-amber-500" />
      ) : (
        <BookOpen className="size-3.5 shrink-0 text-primary" />
      )}
      <span className="min-w-0 truncate">{citation.source_title}</span>
      <span className="shrink-0 tabular-nums text-foreground/70">· {citation.pages_label}</span>
      {isFigure && (
        <span className="shrink-0 font-medium text-amber-600 dark:text-amber-500">
          · {t("figure")}
        </span>
      )}
    </>
  );

  const className =
    "inline-flex w-fit max-w-full items-center gap-1.5 rounded-full border border-primary/30 bg-primary/5 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:bg-primary/10 hover:text-foreground";

  // The figure chip explains itself on hover/focus (same Tooltip shell+children
  // idiom as `source-row.tsx`) — the tutor must understand it opens a picture
  // description, not the author's prose. A plain author chip needs no tooltip.
  if (!isFigure) {
    return (
      <Link
        href={href}
        data-testid="concept-citation-chip"
        data-grounding={citation.grounding}
        className={className}
      >
        {inner}
      </Link>
    );
  }

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Link
            href={href}
            data-testid="concept-citation-chip"
            data-grounding={citation.grounding}
            className={className}
          />
        }
      >
        {inner}
      </TooltipTrigger>
      <TooltipContent>{t("figureExplained")}</TooltipContent>
    </Tooltip>
  );
}
