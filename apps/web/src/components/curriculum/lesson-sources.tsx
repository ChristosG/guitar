"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { BookOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import type { Citation } from "@/lib/api";

interface LessonSourcesProps {
  citations: Citation[] | undefined;
  locale: string;
  lessonTitle: string;
}

/** One compact "Πηγές" button per lesson → modal listing the lesson's sources,
 * grouped by book/article with its cited pages as Reader deep-links.
 *
 * Replaces the per-SECTION ProvenanceChips rows: the grounding data is the
 * tutor's trust signal, but spread under every section it drowned the lessons
 * it was meant to support. Same data (`lesson.meta.citations`, the union
 * `draft.py` already persists), same validated (source, page) pairs, same
 * deep-links — one click away instead of everywhere. */
export function LessonSources({ citations, locale, lessonTitle }: LessonSourcesProps) {
  const t = useTranslations("curricula.tree");
  const [open, setOpen] = useState(false);

  const grouped = useMemo(() => {
    const bySource = new Map<string, { title: string; pages: number[] }>();
    for (const c of citations ?? []) {
      const entry = bySource.get(c.source_id) ?? { title: c.source_title ?? c.source_ref, pages: [] };
      if (!entry.pages.includes(c.page)) entry.pages.push(c.page);
      bySource.set(c.source_id, entry);
    }
    return [...bySource.entries()].map(([sourceId, e]) => ({
      sourceId, title: e.title, pages: [...e.pages].sort((a, b) => a - b),
    }));
  }, [citations]);

  if (grouped.length === 0) return null;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <Button type="button" size="sm" variant="ghost" data-testid="lesson-sources-trigger" />
        }
      >
        <BookOpen className="size-3.5 text-primary" aria-hidden />
        {t("sourcesButton")}
      </DialogTrigger>
      <DialogContent data-testid="lesson-sources-modal">
        <DialogHeader>
          <DialogTitle>{t("sourcesTitle", { lesson: lessonTitle })}</DialogTitle>
        </DialogHeader>
        <ul className="flex flex-col gap-3">
          {grouped.map((s) => (
            <li key={s.sourceId} data-testid="lesson-source-item" className="flex flex-col gap-1">
              <span className="text-sm font-medium">{s.title}</span>
              <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                {s.pages.map((page) => (
                  <Link
                    key={page}
                    href={`/${locale}/library/${s.sourceId}?page=${page}`}
                    data-testid="lesson-source-page"
                    className="rounded-full border border-primary/25 bg-primary/5 px-2 py-0.5 transition-colors hover:border-primary/60 hover:text-foreground"
                    onClick={() => setOpen(false)}
                  >
                    {t("sourcesPage", { page })}
                  </Link>
                ))}
              </span>
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
