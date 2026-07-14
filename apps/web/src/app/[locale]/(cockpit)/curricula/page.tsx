"use client";

import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { InterviewDialog } from "@/components/curriculum/interview-dialog";
import { TreeBoard } from "@/components/curriculum/tree-board";
import { Badge } from "@/components/ui/badge";
import {
  ApiError,
  getCurriculum,
  listCurricula,
  type BlockNode,
  type CurriculumListItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";

// Client component for the same reason as knowledge/page.tsx and
// students/page.tsx: it calls the API straight from the browser.
export default function CurriculaPage() {
  const t = useTranslations("curricula");
  const locale = useLocale();

  const [templates, setTemplates] = useState<CurriculumListItem[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [templatesError, setTemplatesError] = useState<string | null>(null);

  const [activeTree, setActiveTree] = useState<BlockNode | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [boardLoading, setBoardLoading] = useState(false);
  const [boardError, setBoardError] = useState<string | null>(null);

  const fetchTemplates = useCallback(() => {
    return listCurricula()
      .then((data) => setTemplates(data))
      .catch((err) => setTemplatesError(err instanceof ApiError ? err.detail : t("templatesError")))
      .finally(() => setTemplatesLoading(false));
  }, [t]);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  const refreshTemplates = useCallback(() => {
    setTemplatesLoading(true);
    setTemplatesError(null);
    return fetchTemplates();
  }, [fetchTemplates]);

  /** The interview's confirm step MATERIALIZED the tree — it exists right now, with
   * every lesson `queued` and not one word drafted. So this fetches it and opens the
   * board on it IMMEDIATELY, with a progress bar, instead of holding the tutor on a
   * spinner for the four minutes the lessons take to write. He reads module 1 while
   * module 5 is still being written; that is the whole flagship claim, and this
   * function is where it becomes true. */
  const handleMaterialized = useCallback(
    async (rootId: string) => {
      setActiveId(rootId);
      setBoardLoading(true);
      setBoardError(null);
      try {
        setActiveTree(await getCurriculum(rootId));
      } catch (err) {
        setActiveTree(null);
        setBoardError(err instanceof ApiError ? err.detail : t("boardError"));
      } finally {
        setBoardLoading(false);
      }
      refreshTemplates();
    },
    [refreshTemplates, t],
  );

  async function handleSelectTemplate(item: CurriculumListItem) {
    setActiveId(item.id);
    setBoardLoading(true);
    setBoardError(null);
    try {
      const tree = await getCurriculum(item.id);
      setActiveTree(tree);
    } catch (err) {
      setActiveTree(null);
      setBoardError(err instanceof ApiError ? err.detail : t("boardError"));
    } finally {
      setBoardLoading(false);
    }
  }

  function handleRootDeleted() {
    setActiveTree(null);
    setActiveId(null);
    refreshTemplates();
  }

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
          <div className="flex flex-wrap gap-3" data-testid="templates-list">
            {templates.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => handleSelectTemplate(item)}
                data-testid="template-item"
                aria-current={activeId === item.id ? "true" : undefined}
                className={cn(
                  "flex w-56 flex-col gap-1 rounded-xl border border-border bg-card p-3 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50",
                  activeId === item.id && "border-primary ring-primary/40",
                )}
              >
                <span className="truncate font-medium" data-testid="template-title">
                  {item.title}
                </span>
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="outline">{item.language}</Badge>
                  {typeof item.target_profile?.level === "string" && <span>{item.target_profile.level}</span>}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2">
        {boardLoading && (
          <p className="text-sm text-muted-foreground" data-testid="board-loading">
            {t("boardLoading")}
          </p>
        )}
        {boardError && (
          <p role="alert" data-testid="board-error" className="text-sm text-destructive">
            {boardError}
          </p>
        )}
        {!boardLoading && !boardError && !activeTree && (
          <p className="text-sm text-muted-foreground" data-testid="board-empty">
            {t("boardEmpty")}
          </p>
        )}
        {activeTree && (
          // `key` is load-bearing: the board OWNS its tree once mounted (draft polls
          // write into it), so switching curricula must give it a fresh one rather
          // than syncing a prop into state inside an effect.
          <TreeBoard
            key={activeTree.id}
            root={activeTree}
            locale={locale}
            onRootDeleted={handleRootDeleted}
          />
        )}
      </div>
    </div>
  );
}
