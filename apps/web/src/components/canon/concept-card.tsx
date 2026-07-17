"use client";

import { useTranslations } from "next-intl";
import { BookOpen, Split, Users, Sparkle } from "lucide-react";
import { ConceptCitationChip } from "@/components/canon/concept-citation-chip";
import type { ConceptHit, ConceptPosition } from "@/lib/api";

/** ONE CONCEPT, and the reason the tutor bought ten books.
 *
 * Chris: *"10 books all of them talking for guitar TONE, with much information
 * repeated, but also some unique perspectives from each writer... whats the plan
 * there to create the ultimate curriculum, combining the knowledge of the 10
 * books all together?"* — and the answer, per `canon/render.py`, is: the
 * DIVERGENCES. So this card renders them as THE HEADLINE, not a footnote. A book
 * card that showed "the books broadly agree" and hid where Hunter and Gallagher
 * contradict each other would have thrown away the one thing a shelf of ten books
 * gives that no single book can.
 *
 * The API already did the hard part (`canon/search._positions`, single-sourced
 * from `render.py`'s EXACT-equality rule): every position arrives tagged
 * `divergence` | `consensus` | `only_in`. This component only decides the visual
 * weight — divergence loud and first, consensus quiet below, a lone take under
 * its own honest label — and never re-derives agreement itself.
 */
export function ConceptCard({ hit, locale }: { hit: ConceptHit; locale: string }) {
  const t = useTranslations("canon");

  const divergences = hit.positions.filter((p) => p.kind === "divergence");
  const consensus = hit.positions.filter((p) => p.kind === "consensus");
  const onlyIn = hit.positions.filter((p) => p.kind === "only_in");

  return (
    <article
      data-testid={`concept-${hit.key}`}
      data-divergence={hit.divergence}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4"
    >
      {/* Header: the concept's name (English + Greek), coverage, and — when the
          books disagree — a loud divergence badge so the payoff is visible before
          you even read the positions. */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-base font-semibold leading-tight">{hit.label_en}</h3>
          {hit.label_el && hit.label_el !== hit.label_en && (
            <p className="text-sm text-muted-foreground">{hit.label_el}</p>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          {hit.divergence && (
            <span
              data-testid="divergence-badge"
              className="inline-flex items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-400"
            >
              <Split className="size-3.5" />
              {t("disagree")}
            </span>
          )}
          <span className="inline-flex items-center gap-1 rounded-full border border-border bg-muted/50 px-2 py-0.5 text-xs text-muted-foreground">
            <BookOpen className="size-3.5" />
            {t("coverage", { count: hit.coverage })}
          </span>
        </div>
      </div>

      {/* THE HEADLINE. Where the authors disagree, rendered against each other —
          each position with its own citation, a "vs" between them. This is the
          whole product; it must never be averaged into one line. */}
      {divergences.length > 0 && (
        <div
          data-testid="divergence-block"
          className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3"
        >
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-400">
            <Split className="size-3.5" />
            {t("divergenceHeading")}
          </div>
          <div className="flex flex-col gap-2.5">
            {divergences.map((pos, i) => {
              // The "vs" divider marks an AUTHOR boundary, not every position:
              // one book making several points is not disagreeing with itself.
              // Positions arrive sorted by book, so same-book takes are already
              // consecutive — the divider falls exactly where the author changes.
              const prevBook = i > 0 ? divergences[i - 1].books[0] : undefined;
              const newAuthor = i > 0 && pos.books[0] !== prevBook;
              return (
                <div key={`${pos.position}-${i}`}>
                  {newAuthor && (
                    <div className="my-1.5 flex items-center gap-2 text-xs font-medium text-amber-600/80 dark:text-amber-500/80">
                      <span className="h-px flex-1 bg-amber-500/20" />
                      {t("versus")}
                      <span className="h-px flex-1 bg-amber-500/20" />
                    </div>
                  )}
                  <PositionBody pos={pos} locale={locale} emphatic />
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Where the books agree — real, useful, but quiet: it is the 80% the tutor
          already had from any one book. */}
      {consensus.length > 0 && (
        <div data-testid="consensus-block" className="flex flex-col gap-2.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
            <Users className="size-3.5" />
            {t("consensusHeading")}
          </div>
          {consensus.map((pos, i) => (
            <PositionBody key={`${pos.position}-${i}`} pos={pos} locale={locale} />
          ))}
        </div>
      )}

      {/* A concept only one book covers: nobody to disagree with, but still one of
          the "unique perspectives from each writer" the tutor asked to keep. It
          renders under its own honest label — never dressed up as a divergence,
          never dropped. */}
      {onlyIn.length > 0 && (
        <div data-testid="only-in-block" className="flex flex-col gap-2.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            <Sparkle className="size-3.5" />
            {t("onlyInHeading")}
          </div>
          {onlyIn.map((pos, i) => (
            <PositionBody key={`${pos.position}-${i}`} pos={pos} locale={locale} />
          ))}
        </div>
      )}
    </article>
  );
}

/** One position: what it says, then its citation chip(s). `emphatic` bolds the
 * position text for the divergence block, where the contradiction is the point. */
function PositionBody({
  pos,
  locale,
  emphatic = false,
}: {
  pos: ConceptPosition;
  locale: string;
  emphatic?: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <p className={emphatic ? "text-sm font-medium text-foreground" : "text-sm text-foreground/90"}>
        {pos.position}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {pos.citations.map((cite, i) => (
          <ConceptCitationChip key={`${cite.source_id}-${i}`} citation={cite} locale={locale} />
        ))}
      </div>
    </div>
  );
}
