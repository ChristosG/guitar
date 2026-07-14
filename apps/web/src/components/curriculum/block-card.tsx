"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Loader2,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";
import { Artifact } from "@/components/artifacts/artifact";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { AttachArtifactDialog } from "@/components/curriculum/attach-artifact-dialog";
import { SegmentDialog } from "@/components/curriculum/segment-dialog";
import { ExtendWithChat } from "@/components/curriculum/extend-with-chat";
import { ProvenanceChips } from "@/components/curriculum/provenance-chips";
import { TierBadge } from "@/components/curriculum/tier-badge";
import {
  ApiError,
  addLesson,
  deepenLesson,
  deleteBlock,
  reorderBlock,
  updateBlock,
  type ArtifactOut,
  type BlockNode,
  type DraftStatus,
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

/** THE VISUAL TIERING. Chris: "the component is neat but a bit messy, some more
 * spacing might be needed."
 *
 * Four kinds of thing were rendering as one kind of card, so a course, a module, a
 * lesson and a 500-word segment all looked equally important and the board read as
 * a flat wall of boxes. Now the hierarchy is legible before a single word is: the
 * course is a page heading, a module is a titled card, a lesson is a row inside it,
 * and a segment is prose in a reading column. Spacing carries the structure — which
 * is what spacing is FOR. */
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

interface BlockCardProps {
  node: BlockNode;
  locale: string;
  /** True only for the node `TreeBoard` mounts. Passed explicitly rather than
   * sniffed from `kind === "course"` because the ONLY thing that makes this node
   * special is that nothing above it survives its deletion — a fact about its
   * position, not its kind. */
  isRoot?: boolean;
  index?: number;
  siblingCount?: number;
  onChanged: (node: BlockNode) => void;
  onRemoved: (id: string) => void;
  /** Refetch the whole tree. Used by the two mutations whose result this client
   * cannot honestly guess at: reorder and add-lesson both RENORMALISE every
   * sibling's `order` server-side, and a local splice would be inventing the
   * outcome of that. One request beats a wrong tree. */
  onRefresh?: () => void;
}

/** One node of the curriculum tree, rendered recursively.
 *
 * WHAT IS NEW HERE IS EVERYTHING THE TUTOR COULD NOT SEE:
 *
 *  - a TIER BADGE on every module (his library / Claude's own knowledge / the web /
 *    an honest gap) — the standing answer to his question, "what happens with the
 *    ones saying nothing in your library for this module?"
 *  - PROVENANCE CHIPS on every segment, deep-linking into the Reader at the cited
 *    page. This data was already in the database; it was being dropped at the API
 *    boundary and thrown away.
 *  - a per-lesson DRAFT STATE and WORD COUNT, so "queued / drafting / 2,340 words"
 *    is a fact on the row instead of a mystery.
 *  - DEEPEN, on a lesson that came back thin.
 *  - EXTEND WITH CHAT, on anything with prose in it.
 *
 * And artifacts are NOT fetched here any more. They arrive embedded in the tree
 * (`node.artifacts`); the old per-segment `GET /artifacts?block_id=` fired ~120
 * times in parallel on one board render, which is the actual cause of the "Could
 * not load attached artifacts" error.
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
  const confirm = useConfirm();

  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(node.title);
  const [expanded, setExpanded] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Artifacts attached from THIS session's dialog, on top of the embedded ones.
  const [added, setAdded] = useState<ArtifactOut[]>([]);

  const meta = node.meta ?? {};
  const isSegment = node.kind === "segment";
  const isLesson = node.kind === "lesson";
  const isModule = node.kind === "module";
  const reorderable = (isModule || isLesson) && siblingCount > 1;

  const contentChildren = node.children.filter((c) => c.plane === "content");
  const deliveryChildren = node.children.filter((c) => c.plane !== "content");
  const hasChildren = node.children.length > 0;

  const artifacts = [...added, ...(node.artifacts ?? [])];
  const draftStatus = meta.draft_status;
  const wordCount = meta.word_count;

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

  /** DEEPEN. The lesson goes back to `queued` with a raised word target, and the
   * ordinary fan-out redrafts it against the same cached library prefix. The row
   * flips to `queued` immediately, so his click visibly did something; the next
   * progress poll shows it drafting. */
  async function handleDeepen() {
    await run(async () => {
      await deepenLesson(node.id);
      onChanged({ ...node, meta: { ...meta, draft_status: "queued", error: null } });
    }, t("deepenError"));
  }

  const header = (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      {hasChildren ? (
        <CollapsibleTrigger
          aria-label={expanded ? t("collapse") : t("expand")}
          data-testid="block-card-toggle"
          className="shrink-0 text-muted-foreground hover:text-foreground"
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
        <button
          type="button"
          onClick={() => {
            setDraftTitle(node.title);
            setEditing(true);
          }}
          data-testid="block-card-title"
          className={cn(
            "min-w-0 flex-1 truncate text-left hover:underline",
            KIND_TITLE_CLASS[node.kind] ?? "text-sm font-medium",
          )}
        >
          {node.title}
        </button>
      )}

      {isModule && <TierBadge tier={meta.tier} coverageNote={meta.coverage_note} />}

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

      <div className="ml-auto flex shrink-0 items-center gap-1">
        {reorderable && (
          <>
            <Button
              type="button" variant="ghost" size="icon-sm"
              data-testid="block-card-up"
              aria-label={t("moveUp")}
              disabled={busy || index === 0}
              onClick={() => handleReorder("up")}
            >
              <ChevronUp />
            </Button>
            <Button
              type="button" variant="ghost" size="icon-sm"
              data-testid="block-card-down"
              aria-label={t("moveDown")}
              disabled={busy || index === siblingCount - 1}
              onClick={() => handleReorder("down")}
            >
              <ChevronDown />
            </Button>
          </>
        )}

        {isModule && (
          <Button
            type="button" variant="ghost" size="icon-sm"
            data-testid="block-card-add-lesson"
            aria-label={t("addLesson")}
            disabled={busy}
            onClick={handleAddLesson}
          >
            <Plus />
          </Button>
        )}

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

        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          data-testid="block-card-delete"
          disabled={busy}
          onClick={handleDelete}
          aria-label={t("delete")}
        >
          {busy ? <Loader2 className="animate-spin" /> : <Trash2 />}
        </Button>
      </div>
    </div>
  );

  const body = (
    <div className="flex flex-col gap-3">
      {/* A lesson's `body` is a one-line objective; a segment's is 500 words of
          prose. Same column, two jobs — so the segment gets the reading treatment
          and the lesson gets a caption. */}
      {node.body && (
        <p
          data-testid="block-card-body"
          className={cn(
            "whitespace-pre-wrap",
            isSegment ? "text-sm leading-relaxed" : "text-xs text-muted-foreground",
          )}
        >
          {node.body}
        </p>
      )}

      {isSegment && <ProvenanceChips citations={meta.citations} locale={locale} />}

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

      {/* Extend-with-chat, on anything that HAS prose to extend. Not on the course
          root (its body is a title, and "rewrite this block" over a whole course
          means nothing), and not on an undrafted lesson — there is nothing there
          yet, and the button for that one is Deepen. */}
      {(isSegment || isModule || (isLesson && draftStatus === "ready")) && (
        <ExtendWithChat blockId={node.id} canUndo={Boolean(meta.prev_body)} onRefined={onChanged} />
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
            // The vertical rhythm IS the hierarchy: modules breathe, lessons sit
            // closer together, segments are a stack of paragraphs.
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
