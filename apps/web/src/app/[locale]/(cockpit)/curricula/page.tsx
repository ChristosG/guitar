"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { InterviewDialog } from "@/components/curriculum/interview-dialog";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { ApiError, listCurricula, type CurriculumListItem } from "@/lib/api";

// Client component for the same reason as knowledge/page.tsx and
// students/page.tsx: it calls the API straight from the browser.
//
// Unit A: this page is now a plain navigable INDEX — cards link out to
// `/[locale]/curricula/[rootId]` (the new detail route) instead of opening a
// board inline. The interview's confirm step still materializes the tree
// here, but `handleMaterialized` now navigates to the detail route rather
// than fetching and holding it in local state — the "read module 1 while
// module 5 drafts" flagship flow survives unchanged, just one route further
// along (the detail page's own mount-only fetch + TreeBoard is where that
// now lives).
export default function CurriculaPage() {
  const t = useTranslations("curricula");
  const locale = useLocale();
  const router = useRouter();

  const [templates, setTemplates] = useState<CurriculumListItem[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const fetchTemplates = useCallback(() => {
    return listCurricula()
      .then((data) => setTemplates(data))
      .catch((err) => setTemplatesError(err instanceof ApiError ? err.detail : t("templatesError")))
      .finally(() => setTemplatesLoading(false));
  }, [t]);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  /** The interview's confirm step MATERIALIZED the tree — it exists right now, with
   * every lesson `queued` and not one word drafted. So this navigates straight to
   * its detail route instead of holding the tutor on a spinner for the four
   * minutes the lessons take to write: the detail page's own fetch + TreeBoard's
   * progress bar is what lets him read module 1 while module 5 is still being
   * written; that is the whole flagship claim, and this function is where it
   * hands off to it. */
  const handleMaterialized = useCallback(
    (rootId: string) => {
      router.push(`/${locale}/curricula/${rootId}`);
    },
    [router, locale],
  );

  const shown = templates.filter((tm) => tm.title.toLowerCase().includes(query.trim().toLowerCase()));

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="curricula-heading">
            {t("heading")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subheading")}</p>
        </div>
        <InterviewDialog onMaterialized={handleMaterialized} />
      </div>

      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-medium text-muted-foreground">{t("templatesHeading")}</h2>
        {templatesLoading && <p className="text-sm text-muted-foreground">{t("templatesLoading")}</p>}
        {templatesError && (
          <p role="alert" data-testid="templates-error" className="text-sm text-destructive">
            {templatesError}
          </p>
        )}
        {!templatesLoading && !templatesError && templates.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="templates-empty">
            {t("templatesEmpty")}
          </p>
        )}
        {templates.length > 0 && (
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("searchPlaceholder")}
            data-testid="curricula-search"
            className="max-w-sm"
          />
        )}
        {templates.length > 0 && shown.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="curricula-search-no-match">
            {t("searchNoMatch")}
          </p>
        )}
        {shown.length > 0 && (
          <div className="flex flex-wrap gap-3" data-testid="templates-list">
            {shown.map((item) => (
              <Link
                key={item.id}
                href={`/${locale}/curricula/${item.id}`}
                data-testid="template-item"
                className="flex w-56 flex-col gap-1 rounded-xl border border-border bg-card p-3 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50"
              >
                <span className="truncate font-medium" data-testid="template-title">
                  {item.title}
                </span>
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="outline">{item.language}</Badge>
                  {typeof item.target_profile?.level === "string" && <span>{item.target_profile.level}</span>}
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
