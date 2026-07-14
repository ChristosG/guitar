"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { BookOpen } from "lucide-react";
import type { Citation } from "@/lib/api";

interface ProvenanceChipsProps {
  citations: Citation[] | undefined;
  locale: string;
}

/** "HIS BOOK, PAGE 56" — clickable, and it lands on page 56.
 *
 * This data was being written to the database and thrown away at the API boundary
 * (`block_to_tree` serialized nine fields and `meta` was not one of them), so the
 * chips the spec promised could not have rendered even if the board had tried.
 * Now `meta.citations` arrives with the tree and every chip deep-links into the
 * Reader at the cited page.
 *
 * The chips are trustworthy, which is the only reason they are worth having: every
 * (source, page) pair was validated against the pages the model was ACTUALLY SHOWN
 * before it was persisted (`curriculum/draft.py` — a chip that lands him on a page
 * that does not say what the lesson claims it says is worse than no chip, because
 * he would be right to stop trusting the true ones after it).
 *
 * Deduplicated: a section that cites p.56 three times is one chip, not three.
 */
export function ProvenanceChips({ citations, locale }: ProvenanceChipsProps) {
  const t = useTranslations("curricula.tree");
  if (!citations || citations.length === 0) return null;

  const seen = new Set<string>();
  const unique = citations.filter((c) => {
    const key = `${c.source_id}#${c.page}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="provenance-chips">
      {unique.map((c) => (
        <Link
          key={`${c.source_id}#${c.page}`}
          href={`/${locale}/library/${c.source_id}?page=${c.page}`}
          data-testid="provenance-chip"
          className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-primary/25 bg-primary/5 px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:bg-primary/10 hover:text-foreground"
        >
          <BookOpen className="size-3.5 shrink-0 text-primary" aria-hidden />
          <span className="truncate">
            {t("citation", { source: c.source_title ?? c.source_ref, page: c.page })}
          </span>
        </Link>
      ))}
    </div>
  );
}
