"use client";

import { useCallback, useEffect, useState, type KeyboardEvent } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft } from "lucide-react";
import { LessonOutline } from "@/components/lessons/lesson-outline";
import { ProvenanceChip } from "@/components/lessons/provenance-chip";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  getLesson,
  listLessons,
  updateBlock,
  type BlockNode,
  type LessonProvenance,
} from "@/lib/api";

/** THE lesson editor (Plan 10 Task 4): the outline (lesson -> sessions ->
 * items) plus, right up top, the provenance chip that links back into the
 * exact page of the exact scan this lesson was drafted from — the payoff of
 * the whole Library project. Client component, `useParams()` for the
 * dynamic segment, same shape as `library/[sourceId]/page.tsx`'s own
 * docstring on why (a plain "use client" default-export page, not an async
 * server component awaiting the now-Promise `params` — also what makes this
 * visible to Playwright's `page.route`).
 *
 * STATE OWNERSHIP — deliberately simple over deliberately clever: this page
 * holds the ONE canonical `tree` (the whole lesson -> session -> item
 * shape). `SessionCard`/`ItemRow` never keep a silent "locally patched"
 * copy of a committed title/children — they either hand this page an
 * already-fresh tree from an API response that returns one (`splitSession`/
 * `mergeSessions`/`addSession`, via `applyTree`), or, for the two routes
 * that DON'T return a tree (`PATCH`/`DELETE /blocks/{id}`, reused as-is
 * from the curriculum board), they just ask this page to re-fetch `GET
 * /lessons/{id}` (`refreshTree`). One extra GET per rename/delete is a
 * fully deliberate trade: it costs a small round trip and buys freedom from
 * ANY client-side tree-surgery (no recursive "find this node and splice its
 * new title in" helpers to get subtly wrong) — for a lesson's fixed 3-level
 * shape and this app's single-tutor PoC scale, correctness-by-construction
 * beats shaving one network call. */
export default function LessonEditorPage() {
  const t = useTranslations("lessons.editor");
  const locale = useLocale();
  const params = useParams<{ lessonId: string }>();
  const lessonId = params.lessonId;

  const [tree, setTree] = useState<BlockNode | null>(null);
  const [provenance, setProvenance] = useState<LessonProvenance | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const applyTree = useCallback((next: BlockNode) => setTree(next), []);

  const refreshTree = useCallback(() => {
    return getLesson(lessonId)
      .then(applyTree)
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")));
  }, [lessonId, applyTree, t]);

  // Mount-only fetch — `loading`'s own `useState(true)` initializer already
  // covers the "first load" spinner (the same discipline every cockpit
  // detail page follows, e.g. `library/[sourceId]/page.tsx`'s manifest
  // effect), so this never needs a bare
  // `setLoading(true)` inside the effect body itself (flagged by
  // `react-hooks/set-state-in-effect` — cascading-render risk).
  useEffect(() => {
    Promise.all([getLesson(lessonId), listLessons()])
      .then(([lessonTree, list]) => {
        setTree(lessonTree);
        setProvenance(list.find((l) => l.id === lessonId)?.provenance ?? null);
        setError(null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [lessonId, t]);

  // --- lesson title, inline-editable exactly like a session's own title
  // (see session-card.tsx). `draftTitle` is seeded at the moment editing
  // STARTS (`startEditingTitle` below), not kept continuously in sync via
  // an effect — the non-editing view always renders `tree.title` directly
  // (see the JSX below), so there is nothing for an effect to synchronize
  // outside of that one edit-start moment. */
  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [titleError, setTitleError] = useState<string | null>(null);

  function startEditingTitle() {
    if (tree) setDraftTitle(tree.title);
    setEditingTitle(true);
  }

  async function commitTitle() {
    if (!tree) return;
    const next = draftTitle.trim();
    setEditingTitle(false);
    if (!next || next === tree.title) {
      setDraftTitle(tree.title);
      return;
    }
    setTitleError(null);
    try {
      await updateBlock(tree.id, { title: next });
      await refreshTree();
    } catch (err) {
      setDraftTitle(tree.title);
      setTitleError(err instanceof ApiError ? err.detail : t("renameError"));
    }
  }

  function handleTitleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitTitle();
    } else if (e.key === "Escape" && tree) {
      setDraftTitle(tree.title);
      setEditingTitle(false);
    }
  }

  return (
    <div className="flex flex-col gap-6 pb-20">
      <Link
        href={`/${locale}/lessons`}
        data-testid="lessons-back"
        className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        {t("back")}
      </Link>

      {loading && (
        <p className="text-sm text-muted-foreground" data-testid="lesson-editor-loading">
          {t("loading")}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="lesson-editor-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {tree && (
        <>
          <div className="flex flex-col gap-2">
            {editingTitle ? (
              <Input
                autoFocus
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
                onBlur={commitTitle}
                onKeyDown={handleTitleKeyDown}
                data-testid="lesson-title-input"
                className="h-10 max-w-xl text-2xl font-semibold"
              />
            ) : (
              <button
                type="button"
                onClick={startEditingTitle}
                data-testid="lesson-title"
                className="w-fit text-left text-2xl font-semibold hover:underline"
              >
                {tree.title}
              </button>
            )}
            {titleError && (
              <p role="alert" data-testid="lesson-title-error" className="text-xs text-destructive">
                {titleError}
              </p>
            )}
            {provenance && (
              <ProvenanceChip
                sourceId={provenance.source_id}
                pageNo={provenance.page_no}
                pageTo={provenance.page_to}
                locale={locale}
              />
            )}
          </div>

          <LessonOutline
            lessonId={lessonId}
            sessions={tree.children}
            applyTree={applyTree}
            refreshTree={refreshTree}
          />
        </>
      )}
    </div>
  );
}
