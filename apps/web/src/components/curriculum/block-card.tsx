"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import {
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Loader2,
  MoreVertical,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";
import { Artifact } from "@/components/artifacts/artifact";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useConfirm } from "@/components/ui/confirm";
import { AttachArtifactDialog } from "@/components/curriculum/attach-artifact-dialog";
import { SegmentDialog } from "@/components/curriculum/segment-dialog";
import { ExtendWithChat } from "@/components/curriculum/extend-with-chat";
import { LessonSources } from "@/components/curriculum/lesson-sources";
import { TierBadge } from "@/components/curriculum/tier-badge";
import {
  ApiError,
  addLesson,
  deepenLesson,
  deleteBlock,
  deleteCurriculum,
  reorderBlock,
  updateBlock,
  type ArtifactOut,
  type BlockNode,
  type DraftStatus,
  type SegmentStatus,
} from "@/lib/api";
import { cn } from "@/lib/utils";

interface SubtreeCounts {
  total: number;
  modules: number;
  lessons: number;
}

/** What a `DELETE /blocks/{id}` is actually about to take with it. The API cascades
 * (ORM `delete-orphan` AND `ON DELETE CASCADE`), so one click on the root's Trash
 * icon destroys the entire generated tree — which is exactly why the confirm dialog
 * has to be able to SAY so, with numbers, rather than asking "are you sure?" about
 * an unnamed quantity. */
function countSubtree(nodes: BlockNode[]): SubtreeCounts {
  return nodes.reduce<SubtreeCounts>(
    (acc, child) => {
      const sub = countSubtree(child.children);
      return {
        total: acc.total + 1 + sub.total,
        modules: acc.modules + (child.kind === "module" ? 1 : 0) + sub.modules,
        lessons: acc.lessons + (child.kind === "lesson" ? 1 : 0) + sub.lessons,
      };
    },
    { total: 0, modules: 0, lessons: 0 },
  );
}

const KIND_TITLE_CLASS: Record<string, string> = {
  course: "text-xl font-semibold tracking-tight",
  module: "text-base font-semibold",
  lesson: "text-sm font-medium",
  segment: "text-sm font-medium text-muted-foreground",
  delivery_root: "text-base font-semibold",
  session: "text-sm font-medium",
};

const KIND_SHELL_CLASS: Record<string, string> = {
  course: "rounded-2xl border border-border bg-card p-5 ring-1 ring-foreground/5",
  module: "rounded-2xl border border-border bg-card p-4 ring-1 ring-foreground/5",
  lesson: "rounded-xl border border-border/70 bg-background p-3",
  segment: "rounded-xl bg-muted/30 p-3",
  delivery_root: "rounded-2xl border border-border bg-card p-4 ring-1 ring-foreground/5",
  session: "rounded-xl border border-border/70 bg-background p-3",
};

const DRAFT_STATUS_CLASS: Record<DraftStatus, string> = {
  queued: "border-border bg-muted text-muted-foreground",
  drafting: "border-primary/40 bg-primary/10 text-primary",
  ready: "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  failed: "border-destructive/40 bg-destructive/10 text-destructive",
};

// `done` (or no `segment_status` at all — every segment older than this
// chip) renders nothing; only the two states worth a tutor's attention get a
// chip, same "chip per non-default state" idiom as `DRAFT_STATUS_CLASS`.
const SEGMENT_STATUS_CLASS: Record<Exclude<SegmentStatus, "done">, string> = {
  queued: "border-border bg-muted text-muted-foreground",
  failed: "border-destructive/40 bg-destructive/10 text-destructive",
};

interface BlockCardProps {
  node: BlockNode;
  locale: string;
  isRoot?: boolean;
  index?: number;
  siblingCount?: number;
  onChanged: (node: BlockNode) => void;
  onRemoved: (id: string) => void;
  onRefresh?: () => void;
}

/** One node of the curriculum tree, rendered recursively.
 *
 * THE TUTOR-FRIENDLY REWRITE (Chris: "everything is expanded and a chaos"):
 *
 *  - COLLAPSED BY DEFAULT. Only the course root opens expanded, so a curriculum
 *    opens as a list of module rows — a table of contents, not a 45,000-word
 *    wall. A module opens to its lesson rows; a lesson opens to its full prose.
 *  - THE WHOLE ROW IS THE TOGGLE. Clicking anywhere on a header expands or
 *    collapses it — not just the 16px chevron. Buttons inside the row stop
 *    propagation, so actions never accidentally fold the tree.
 *  - RENAME IS AN ACTION, NOT A TITLE CLICK. Clicking a title used to open the
 *    rename input, which is why "click the row" could never work and why the
 *    tutor believed only titles were editable. Rename now lives in the ⋯ menu.
 *  - THE BODY IS EDITABLE. Every module objective, lesson objective and segment's
 *    prose gets an Edit button (`PATCH /blocks/{id}` has accepted `body` all
 *    along — the UI just never offered it).
 *  - THE ACTION CLUTTER IS FOLDED into one ⋯ menu per row (rename / move /
 *    add lesson / delete). What stays visible is what the tutor actually reads:
 *    tier badges, draft status, word counts — and Deepen, the one-click fix for
 *    a thin lesson.
 *
 * Artifacts arrive embedded in the tree (`node.artifacts`); ones attached THIS
 * session land in `added`, deduped against the embedded list — the board refetches
 * the whole tree on every draft-poll tick, and without the dedupe every attached
 * artifact rendered twice as soon as the refetch landed.
 */
export function BlockCard({
  node,
  locale,
  isRoot = false,
  index = 0,
  siblingCount = 1,
  onChanged,
  onRemoved,
  onRefresh,
}: BlockCardProps) {
  const t = useTranslations("curricula.tree");
  // Root-only: reused for the SAME 409 ("still drafting") copy
  // `CurriculumActionsMenu` shows for the header's ⋯ menu — one Greek/English
  // sentence for the one 409 the tutor is guaranteed to hit, whichever door
  // he deletes through.
  const tActions = useTranslations("curricula.actions");
  const confirm = useConfirm();

  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(node.title);
  // Collapsed by default — the root is the only node that opens expanded, so the
  // board reads as a table of contents.
  // NOTE FOR E2E AUTHORS: non-root nodes mount collapsed, so a test must expand
  // one (e.g. click its title) before asserting on lesson content beneath it.
  const [expanded, setExpanded] = useState(isRoot);
  const [editingBody, setEditingBody] = useState(false);
  const [draftBody, setDraftBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState<ArtifactOut[]>([]);

  const meta = node.meta ?? {};
  const isSegment = node.kind === "segment";
  const isLesson = node.kind === "lesson";
  const isModule = node.kind === "module";
  const reorderable = (isModule || isLesson) && siblingCount > 1;

  const contentChildren = node.children.filter((c) => c.plane === "content");
  const deliveryChildren = node.children.filter((c) => c.plane !== "content");
  const hasChildren = node.children.length > 0;
  const lessonCount = contentChildren.filter((c) => c.kind === "lesson").length;

  const embedded = node.artifacts ?? [];
  const artifacts = [
    ...added.filter((a) => !embedded.some((e) => e.id === a.id)),
    ...embedded,
  ];
  const draftStatus = meta.draft_status;
  const wordCount = meta.word_count;
  const segmentStatus = meta.segment_status;

  const kindKey = `kinds.${node.kind}`;
  const kindLabel = t.has(kindKey) ? t(kindKey) : node.kind;

  async function run(fn: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : fallback);
    } finally {
      setBusy(false);
    }
  }

  async function commitTitle() {
    const next = draftTitle.trim();
    setEditing(false);
    if (!next || next === node.title) {
      setDraftTitle(node.title);
      return;
    }
    await run(async () => {
      onChanged(await updateBlock(node.id, { title: next }));
    }, t("updateError"));
  }

  function startBodyEdit() {
    setDraftBody(node.body ?? "");
    setEditingBody(true);
    setExpanded(true);
  }

  async function commitBody(e: FormEvent) {
    e.preventDefault();
    const next = draftBody;
    setEditingBody(false);
    if (next === (node.body ?? "")) return;
    await run(async () => {
      onChanged(await updateBlock(node.id, { body: next }));
    }, t("editBodyError"));
  }

  async function handleDelete() {
    const counts = countSubtree(node.children);
    const ok = await confirm({
      title: isRoot
        ? t("confirmDelete.rootTitle", { title: node.title })
        : t("confirmDelete.title", { title: node.title }),
      body: (
        <>
          <p>{t("confirmDelete.lead", { title: node.title, kind: kindLabel })}</p>
          {counts.total > 0 && <p>{t("confirmDelete.counts", { ...counts })}</p>}
          <p className="font-medium text-destructive">
            {isRoot ? t("confirmDelete.rootWarning") : t("confirmDelete.irreversible")}
          </p>
        </>
      ),
      confirmLabel: t("confirmDelete.confirm"),
      destructive: true,
    });
    if (!ok) return;

    // ROOT CARD, NOT A GENERIC BLOCK (review fix — closes the "two-door"
    // seam): a course root deleted from HERE used to go straight through
    // `deleteBlock` -> `DELETE /blocks/{id}`, which has no drafting guard
    // and skips `delete_curriculum`'s cleanup of bound chat sessions,
    // interviews, and `GenerationJob.result_root_id` pointers
    // (`routers/curriculum.py`). The header's ⋯ menu
    // (`CurriculumActionsMenu`) already deletes roots the right way; this
    // card must go through the exact same `deleteCurriculum()` call, not a
    // second, incomplete implementation of the same action. Not routed
    // through the generic `run()` helper below because the 409 needs its
    // own localized copy, same as `CurriculumActionsMenu.handleDelete`.
    if (isRoot) {
      setBusy(true);
      setError(null);
      try {
        await deleteCurriculum(node.id);
        onRemoved(node.id);
      } catch (err) {
        setError(
          err instanceof ApiError
            ? (err.status === 409 ? tActions("deleteDrafting") : err.detail)
            : t("deleteError"),
        );
      } finally {
        setBusy(false);
      }
      return;
    }

    await run(async () => {
      await deleteBlock(node.id);
      onRemoved(node.id);
    }, t("deleteError"));
  }

  async function handleReorder(direction: "up" | "down") {
    await run(async () => {
      await reorderBlock(node.id, direction);
      onRefresh?.();
    }, t("reorderError"));
  }

  async function handleAddLesson() {
    await run(async () => {
      await addLesson(node.id, { title: t("newLessonTitle") });
      onRefresh?.();
    }, t("addLessonError"));
  }

  async function handleDeepen() {
    await run(async () => {
      await deepenLesson(node.id);
      onChanged({ ...node, meta: { ...meta, draft_status: "queued", error: null } });
    }, t("deepenError"));
  }

  function toggle() {
    if (hasChildren && !editing) setExpanded((v) => !v);
  }

  const header = (
    <div
      className={cn(
        "flex min-w-0 flex-wrap items-center gap-2",
        // THE WHOLE ROW IS THE TOGGLE — with a hover tint so it reads as
        // clickable before the first click.
        hasChildren && "-m-1.5 cursor-pointer rounded-lg p-1.5 transition-colors hover:bg-muted/50",
      )}
      data-testid="block-card-header"
      onClick={toggle}
    >
      {hasChildren ? (
        <CollapsibleTrigger
          aria-label={expanded ? t("collapse") : t("expand")}
          data-testid="block-card-toggle"
          className="shrink-0 text-muted-foreground hover:text-foreground"
          onClick={(e) => e.stopPropagation()}
        >
          {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </CollapsibleTrigger>
      ) : (
        <span className="inline-block size-4 shrink-0" />
      )}

      {editing ? (
        <Input
          autoFocus
          value={draftTitle}
          onChange={(e) => setDraftTitle(e.target.value)}
          onClick={(e) => e.stopPropagation()}
          onBlur={commitTitle}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitTitle();
            } else if (e.key === "Escape") {
              setDraftTitle(node.title);
              setEditing(false);
            }
          }}
          placeholder={t("editTitlePlaceholder")}
          data-testid="block-card-title-input"
          className="h-8 min-w-0 flex-1"
        />
      ) : (
        <span
          data-testid="block-card-title"
          // Mobile: the title WRAPS and the badges drop below it — every chip in
          // this row is shrink-0, so a truncating flex-1 title was the only thing
          // that could yield and it collapsed to nothing. From `sm` up there is
          // room for one line, so it truncates as before.
          className={cn(
            "min-w-0 flex-1 break-words text-left sm:truncate",
            KIND_TITLE_CLASS[node.kind] ?? "text-sm font-medium",
          )}
        >
          {node.title}
        </span>
      )}

      {isModule && <TierBadge tier={meta.tier} coverageNote={meta.coverage_note} />}

      {isModule && !expanded && lessonCount > 0 && (
        <Badge variant="outline" data-testid="module-lesson-count">
          {t("lessonCount", { count: lessonCount })}
        </Badge>
      )}

      {isLesson && draftStatus && (
        <span
          data-testid="lesson-status"
          data-status={draftStatus}
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
            DRAFT_STATUS_CLASS[draftStatus] ?? DRAFT_STATUS_CLASS.queued,
          )}
        >
          {draftStatus === "drafting" && <Loader2 className="size-3 animate-spin" aria-hidden />}
          {t.has(`draftStatus.${draftStatus}`) ? t(`draftStatus.${draftStatus}`) : draftStatus}
        </span>
      )}

      {isSegment && (segmentStatus === "queued" || segmentStatus === "failed") && (
        <span
          data-testid="segment-status"
          data-status={segmentStatus}
          title={segmentStatus === "failed" ? meta.segment_error ?? undefined : undefined}
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
            SEGMENT_STATUS_CLASS[segmentStatus],
          )}
        >
          {t(`segmentStatus.${segmentStatus}`)}
        </span>
      )}

      {isLesson && typeof wordCount === "number" && wordCount > 0 && (
        <Badge
          variant="outline"
          data-testid="lesson-word-count"
          className={cn(
            meta.meets_floor === false && "border-amber-500/50 text-amber-600 dark:text-amber-400",
          )}
        >
          {t("wordCount", { count: wordCount })}
        </Badge>
      )}

      {!isLesson && node.est_minutes != null && (
        <Badge variant="outline" data-testid="block-card-minutes">
          {t("estMinutes", { count: node.est_minutes })}
        </Badge>
      )}

      <div
        className="ml-auto flex shrink-0 items-center gap-1"
        onClick={(e) => e.stopPropagation()}
      >
        {isLesson && (
          <Button
            type="button" variant="ghost" size="sm"
            data-testid="lesson-deepen"
            disabled={busy || draftStatus === "drafting"}
            onClick={handleDeepen}
          >
            {busy ? <Loader2 className="animate-spin" /> : <Sparkles />}
            {t("deepen")}
          </Button>
        )}

        {node.kind === "course" && (
          <SegmentDialog blockId={node.id} blockTitle={node.title} onSegmented={() => onRefresh?.()} />
        )}
        {isSegment && (
          <AttachArtifactDialog
            blockId={node.id}
            blockTitle={node.title}
            onAttached={(artifact) => setAdded((prev) => [artifact, ...prev])}
          />
        )}

        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                data-testid="block-card-menu"
                aria-label={t("moreActions")}
                disabled={busy}
              />
            }
          >
            {busy ? <Loader2 className="animate-spin" /> : <MoreVertical />}
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            {/* ROOT CARD HAS NO INLINE RENAME (review fix — closes the other
             * half of the "two-door" seam). `commitTitle` below calls
             * `updateBlock` -> `PATCH /blocks/{id}` directly, which never
             * reaches the detail page's own `title` state (the page owns a
             * SEPARATE copy for the header, synced only through
             * `CurriculumActionsMenu`'s `onRenamed` — see that page's own
             * docstring on why `TreeBoard` never syncs a prop into its
             * state). A rename here would silently save while the header
             * beside it kept showing the old name until the next full
             * navigation. No clean callback channel exists from this card up
             * to the page today, and adding one is a bigger change than this
             * fix warrants — so the smaller, cleaner fix is: the header's ⋯
             * menu (`CurriculumActionsMenu`) is the one rename door for a
             * course root, full stop. Every other kind keeps this item. */}
            {!isRoot && (
              <DropdownMenuItem
                data-testid="menu-rename"
                onClick={() => {
                  setDraftTitle(node.title);
                  setEditing(true);
                }}
              >
                <Pencil />
                {t("rename")}
              </DropdownMenuItem>
            )}
            {reorderable && (
              <>
                <DropdownMenuItem
                  data-testid="menu-move-up"
                  disabled={index === 0}
                  onClick={() => handleReorder("up")}
                >
                  <ChevronUp />
                  {t("moveUp")}
                </DropdownMenuItem>
                <DropdownMenuItem
                  data-testid="menu-move-down"
                  disabled={index === siblingCount - 1}
                  onClick={() => handleReorder("down")}
                >
                  <ChevronDown />
                  {t("moveDown")}
                </DropdownMenuItem>
              </>
            )}
            {isModule && (
              <DropdownMenuItem data-testid="menu-add-lesson" onClick={handleAddLesson}>
                <Plus />
                {t("addLesson")}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem destructive data-testid="menu-delete" onClick={handleDelete}>
              <Trash2 />
              {t("delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );

  const canEditBody = isSegment || isModule || isLesson;

  const body = (
    <div className="flex flex-col gap-3">
      {editingBody ? (
        <form onSubmit={commitBody} className="flex flex-col gap-2" data-testid="body-edit-form">
          <Textarea
            autoFocus
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setEditingBody(false);
            }}
            rows={Math.min(18, Math.max(4, draftBody.split("\n").length + 1))}
            data-testid="body-edit-textarea"
            disabled={busy}
            className={cn(isSegment && "text-sm leading-relaxed")}
          />
          <div className="flex items-center gap-2 self-end">
            <Button
              type="button" size="sm" variant="outline"
              disabled={busy}
              onClick={() => setEditingBody(false)}
            >
              {t("cancel")}
            </Button>
            <Button type="submit" size="sm" disabled={busy} data-testid="body-edit-save">
              {busy && <Loader2 className="animate-spin" />}
              {t("editSave")}
            </Button>
          </div>
        </form>
      ) : (
        node.body && (
          <p
            data-testid="block-card-body"
            className={cn(
              "whitespace-pre-wrap",
              isSegment ? "text-sm leading-relaxed" : "text-xs text-muted-foreground",
            )}
          >
            {node.body}
          </p>
        )
      )}

      {isLesson && !editingBody && (
        <LessonSources citations={meta.citations} locale={locale} lessonTitle={node.title} />
      )}

      {isLesson && meta.error && (
        <p role="alert" data-testid="lesson-error" className="text-xs text-destructive">
          {meta.error}
        </p>
      )}

      {artifacts.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="segment-artifacts">
          <span className="text-xs font-medium text-muted-foreground">
            {t("attachedArtifactsHeading")}
          </span>
          <div className="flex flex-wrap gap-3" data-testid="segment-artifact-list">
            {artifacts.map((artifact) => (
              <div key={artifact.id} data-testid="segment-artifact-item">
                <Artifact kind={artifact.kind} spec={artifact.spec} />
              </div>
            ))}
          </div>
        </div>
      )}

      {!editingBody && (canEditBody || isSegment || isModule || (isLesson && draftStatus === "ready")) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {canEditBody && (
            <Button
              type="button" size="sm" variant="ghost"
              data-testid="body-edit-trigger"
              disabled={busy}
              onClick={startBodyEdit}
            >
              <Pencil />
              {t("editContent")}
            </Button>
          )}
          {(isSegment || isModule || (isLesson && draftStatus === "ready")) && (
            <ExtendWithChat blockId={node.id} canUndo={Boolean(meta.prev_body)} onRefined={onChanged} />
          )}
        </div>
      )}

      {error && (
        <p role="alert" data-testid="block-card-error" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );

  const children = (
    <>
      {contentChildren.length > 0 && (
        <div
          className={cn(
            "flex flex-col",
            node.kind === "course" ? "gap-4" : node.kind === "module" ? "gap-3" : "gap-2",
            node.kind !== "course" && "border-l border-border pl-4",
          )}
          data-testid="block-card-children"
        >
          {contentChildren.map((child, i) => (
            <BlockCard
              key={child.id}
              node={child}
              locale={locale}
              index={i}
              siblingCount={contentChildren.length}
              onChanged={onChanged}
              onRemoved={onRemoved}
              onRefresh={onRefresh}
            />
          ))}
        </div>
      )}

      {deliveryChildren.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">{t("deliveryHeading")}</span>
          <div className="flex flex-col gap-2 border-l border-border pl-4" data-testid="block-card-delivery">
            {deliveryChildren.map((child, i) => (
              <BlockCard
                key={child.id}
                node={child}
                locale={locale}
                index={i}
                siblingCount={deliveryChildren.length}
                onChanged={onChanged}
                onRemoved={onRemoved}
                onRefresh={onRefresh}
              />
            ))}
          </div>
        </div>
      )}
    </>
  );

  const shell = cn(
    "flex min-w-0 flex-col gap-3",
    KIND_SHELL_CLASS[node.kind] ?? "rounded-xl border border-border/70 bg-background p-3",
  );

  if (!hasChildren) {
    return (
      <section className={shell} data-testid="block-card" data-kind={node.kind}>
        {header}
        {body}
      </section>
    );
  }

  return (
    <section className={shell} data-testid="block-card" data-kind={node.kind}>
      <Collapsible open={expanded} onOpenChange={setExpanded}>
        {header}
        <CollapsibleContent className="overflow-hidden transition-[height] duration-200 ease-out">
          <div className="flex flex-col gap-4 pt-3">
            {body}
            {children}
          </div>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
