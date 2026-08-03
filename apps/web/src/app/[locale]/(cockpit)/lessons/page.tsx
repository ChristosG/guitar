"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { ProvenanceChip } from "@/components/lessons/provenance-chip";
import { ApiError, listLessons, type LessonListItem } from "@/lib/api";

/** The Lesson list: every lesson the tutor has drafted from his library, in
 * order (newest first, per `GET /lessons`), each showing only its title and
 * where it came from — clicking a row opens the outline editor. Client
 * component for the same reason as every other cockpit page (see
 * `lib/api.ts`'s own docstring): it calls the API straight from the
 * browser, which is also what makes it visible to Playwright's
 * `page.route`.
 *
 * Deliberately has NO "new lesson" button: a lesson is only ever born from
 * a passage the tutor selected while reading (`POST /lessons/from-
 * selection`, wired into the Reader's `SelectionAction`) — there is no
 * blank-lesson affordance to offer here without inventing a second, ungrounded
 * creation path the backend doesn't support. */
export default function LessonsPage() {
  const t = useTranslations("lessons");
  const locale = useLocale();

  const [lessons, setLessons] = useState<LessonListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchLessons = useCallback(() => {
    return listLessons()
      .then((data) => {
        setLessons(data);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    fetchLessons();
  }, [fetchLessons]);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="lessons-heading">
          {t("heading")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground" data-testid="lessons-loading">
          {t("loading")}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="lessons-error" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && !error && lessons.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="lessons-empty">
          {t("empty")}
        </p>
      )}

      {lessons.length > 0 && (
        <div className="flex flex-col gap-3" data-testid="lessons-list">
          {lessons.map((lesson) => (
            <div
              key={lesson.id}
              data-testid="lesson-item"
              className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-card p-4 ring-1 ring-foreground/10 transition-colors hover:bg-muted/50"
            >
              <Link
                href={`/${locale}/lessons/${lesson.id}`}
                data-testid="lesson-title-link"
                className="flex-1 truncate text-sm font-medium hover:underline"
              >
                {lesson.title}
              </Link>
              {lesson.provenance ? (
                <ProvenanceChip
                  sourceId={lesson.provenance.source_id}
                  pageNo={lesson.provenance.page_no}
                  pageTo={lesson.provenance.page_to}
                  locale={locale}
                />
              ) : (
                <span className="text-xs text-muted-foreground" data-testid="no-provenance">
                  {t("noProvenance")}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
