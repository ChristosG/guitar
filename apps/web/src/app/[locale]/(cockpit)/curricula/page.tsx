"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { CurriculumActionsMenu } from "@/components/curriculum/curriculum-actions-menu";
import { InterviewDialog } from "@/components/curriculum/interview-dialog";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  getOpenInterview,
  listCurricula,
  type CurriculumListItem,
  type OpenInterviewOut,
} from "@/lib/api";

// Resume chips the tutor explicitly dismissed — kept client-side because
// dismissal is a UI preference, not interview state: the server row stays
// resumable (another browser may still want it), this browser just stops
// offering it.
const DISMISSED_KEY = "curricula.dismissedInterviews";

function dismissedIds(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? "[]");
    return Array.isArray(raw) ? raw.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

// Client component for the same reason as every other cockpit page: it calls
// the API straight from the browser (see lib/api.ts's docstring on why).
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
  // A crash-orphaned interview to offer resuming (2026-07-23: a desktop OOM
  // kill closed the wizard mid-flight; the server state survived, the pointer
  // didn't). Best-effort: a failed probe means no chip, never an error state.
  const [openInterview, setOpenInterview] = useState<OpenInterviewOut | null>(null);

  const fetchTemplates = useCallback(() => {
    return listCurricula()
      .then((data) => setTemplates(data))
      .catch((err) => setTemplatesError(err instanceof ApiError ? err.detail : t("templatesError")))
      .finally(() => setTemplatesLoading(false));
  }, [t]);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  useEffect(() => {
    getOpenInterview()
      .then((oi) => {
        if (oi && !dismissedIds().includes(oi.interview_id)) setOpenInterview(oi);
      })
      .catch(() => {
        // Best-effort — the page works exactly as before without the chip.
      });
  }, []);

  const dismissOpenInterview = useCallback(() => {
    setOpenInterview((current) => {
      if (current) {
        localStorage.setItem(
          DISMISSED_KEY,
          JSON.stringify([...dismissedIds(), current.interview_id]),
        );
      }
      return null;
    });
  }, []);

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
        <InterviewDialog
          onMaterialized={handleMaterialized}
          resume={openInterview}
          onResumeDismissed={dismissOpenInterview}
        />
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
        {/* A GRID THAT ANSWERS TO ITS CONTAINER, not a row of fixed cards.
            `flex-wrap` with `sm:w-56` pinned every card at 224px forever: a
            wider window bought more columns and never a wider card, so the last
            column left a ragged gap and the whole list read as unresponsive.
            `auto-fill` + `minmax(14rem, 1fr)` keeps 14rem as the FLOOR — the
            width the titles were designed against — and spends whatever is left
            over widening the columns evenly, so a row always reaches the right
            edge. Fewer, wider cards on a narrow window; more, still-full-width
            cards on a maximised one. */}
        {shown.length > 0 && (
          <div
            className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-3"
            data-testid="templates-list"
          >
            {shown.map((item) => (
              <div key={item.id} className="relative" data-testid="template-item">
                <Link
                  href={`/${locale}/curricula/${item.id}`}
                  className="flex w-full flex-col gap-1 rounded-xl border border-border bg-card p-3 pr-10 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50"
                >
                  {/* `title=` because the span TRUNCATES — a long course name
                      ends in an ellipsis and, without this, there is no way to
                      read the rest of it. Native tooltip rather than a
                      component: it works in the desktop WebKitGTK build with no
                      portal to position, which is the machinery that was
                      mispositioning things in the first place. */}
                  <span className="truncate font-medium" data-testid="template-title" title={item.title}>
                    {item.title}
                  </span>
                  <span className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Badge variant="outline">{item.language}</Badge>
                    {typeof item.target_profile?.level === "string" && <span>{item.target_profile.level}</span>}
                  </span>
                </Link>
                <div className="absolute right-1 top-1">
                  <CurriculumActionsMenu
                    rootId={item.id}
                    title={item.title}
                    onRenamed={() => fetchTemplates()}
                    onDeleted={() => fetchTemplates()}
                    // No navigation: the list is ordered `created_at desc`, so
                    // the copy simply appears at the top and he can open it if
                    // he wants to. Being teleported into a copy made for
                    // safekeeping is the wrong default.
                    onDuplicated={() => fetchTemplates()}
                  />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
