"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronRight, Loader2, Trash2 } from "lucide-react";
import { Artifact } from "@/components/artifacts/artifact";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { AttachArtifactDialog } from "@/components/curriculum/attach-artifact-dialog";
import { SegmentDialog } from "@/components/curriculum/segment-dialog";
import { ApiError, deleteBlock, listArtifacts, updateBlock, type ArtifactOut, type BlockNode } from "@/lib/api";

interface SubtreeCounts {
  total: number;
  modules: number;
  lessons: number;
}

/** What a `DELETE /blocks/{id}` is actually about to take with it. The API
 * cascades (`delete_block`: ORM `delete-orphan` AND `ON DELETE CASCADE`), so
 * one click on the root's Trash icon destroys the entire generated tree —
 * which is exactly why the confirm dialog has to be able to SAY so, with
 * numbers, instead of asking "are you sure?" about an unnamed quantity. */
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

type BadgeVariant = "default" | "secondary" | "outline";

// Subtle per-kind shading, deepest at the root — reuses the neutral
// `--chart-*` ramp already defined in globals.css (a pure grayscale
// progression in this theme, light and dark alike) rather than inventing
// new colors, so the tree reads as one system with the rest of the app.
const KIND_BADGE_VARIANT: Record<string, BadgeVariant> = {
  course: "default",
  module: "secondary",
  lesson: "outline",
  segment: "outline",
  delivery_root: "secondary",
  session: "outline",
};
const KIND_ACCENT: Record<string, string> = {
  course: "var(--chart-5)",
  module: "var(--chart-4)",
  lesson: "var(--chart-3)",
  segment: "var(--chart-2)",
  delivery_root: "var(--chart-4)",
  session: "var(--chart-2)",
};

interface BlockCardProps {
  node: BlockNode;
  onRemoved: (id: string) => void;
  /** True only for the node `TreeBoard` mounts — the curriculum root. Passed
   * explicitly rather than sniffed from `node.kind === "course"` because the
   * ONLY thing that makes this node special is that nothing above it survives
   * its deletion, and that is a fact about its position, not its kind. The
   * root's confirm copy is the loudest in the app for the same reason. */
  isRoot?: boolean;
}

/** One node of a curriculum Block tree, rendered recursively: children
 * render as nested BlockCards inside an indent guide, so `tree-board.tsx`
 * only ever mounts one of these (for the root) and the rest follows from
 * `node.children`. Owns all of a node's own mutations:
 *  - click-to-edit title (optimistic `PATCH /blocks/{id}`, reverts on error)
 *  - delete (`DELETE /blocks/{id}`, then tells the parent via `onRemoved`
 *    so *it* drops this node from its own children — see `handleChildRemoved`)
 *  - for a "course" node only, segmenting into delivery sessions (via
 *    `SegmentDialog`); the returned delivery_root is merged into this
 *    node's own `children` (keyed by id, so re-segmenting replaces the
 *    previous plan rather than duplicating it) and rendered through the
 *    same recursive BlockCard, under a small "Delivery sessions" heading —
 *    `children` is partitioned by `plane` for that, which also covers the
 *    case where a delivery_root arrived via a normal tree fetch instead of
 *    a live segment call (`block_to_tree` doesn't filter by plane either).
 *  - for a "segment" node only (Plan 4 Task 5), generating/attaching a
 *    teaching artifact (via `AttachArtifactDialog` -> `generateArtifact
 *    ({..., blockId: node.id})`) and fetching+rendering any already-attached
 *    ones inline (`listArtifacts({blockId: node.id})`) — the same "dialog
 *    triggers the action, inline area shows the result" split segmenting
 *    already uses above, just scoped to the leaf "segment" kind instead of
 *    "course".
 */
export function BlockCard({ node, onRemoved, isRoot = false }: BlockCardProps) {
  const t = useTranslations("curricula.tree");
  const confirm = useConfirm();

  const [title, setTitle] = useState(node.title);
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(node.title);
  const [titleError, setTitleError] = useState<string | null>(null);

  const [children, setChildren] = useState(node.children);
  const [expanded, setExpanded] = useState(true);

  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const isSegment = node.kind === "segment";
  const [artifacts, setArtifacts] = useState<ArtifactOut[]>([]);
  const [artifactsLoading, setArtifactsLoading] = useState(isSegment);
  const [artifactsError, setArtifactsError] = useState<string | null>(null);

  const contentChildren = children.filter((c) => c.plane === "content");
  const deliveryChildren = children.filter((c) => c.plane !== "content");
  const hasChildren = children.length > 0;

  const kindKey = `kinds.${node.kind}`;
  const kindLabel = t.has(kindKey) ? t(kindKey) : node.kind;
  const badgeVariant = KIND_BADGE_VARIANT[node.kind] ?? "outline";
  const accent = KIND_ACCENT[node.kind] ?? "var(--border)";

  // Same fetch-on-mount shape as e.g. `students/page.tsx`'s `fetchStudents`
  // (a `useCallback` .then/.catch/.finally, driven by a `useEffect`), scoped
  // to "segment" nodes only — every OTHER kind (course/module/lesson/
  // delivery_root/session) never had artifacts attached to it by this UI, so
  // there's no reason to fire a `GET /artifacts?block_id=` for those. A big
  // curriculum tree can render many segment leaves at once, each running
  // this effect independently (one request per segment, not batched — the
  // API has no bulk "artifacts for these N block ids" route) — acceptable
  // for this PoC's tree sizes; a real bulk endpoint would be the fix if this
  // ever shows up as a real bottleneck.
  const fetchArtifacts = useCallback(() => {
    return listArtifacts({ blockId: node.id })
      .then((data) => setArtifacts(data))
      .catch((err) => setArtifactsError(err instanceof ApiError ? err.detail : t("artifactsError")))
      .finally(() => setArtifactsLoading(false));
  }, [node.id, t]);

  useEffect(() => {
    if (isSegment) fetchArtifacts();
  }, [isSegment, fetchArtifacts]);

  function handleArtifactAttached(artifact: ArtifactOut) {
    setArtifacts((prev) => [artifact, ...prev]);
  }

  async function commitTitle() {
    const next = draftTitle.trim();
    setEditing(false);
    if (!next || next === title) {
      setDraftTitle(title);
      return;
    }
    const previous = title;
    setTitle(next); // optimistic: flip immediately, reconcile in the background
    setTitleError(null);
    try {
      await updateBlock(node.id, { title: next });
    } catch (err) {
      setTitle(previous);
      setDraftTitle(previous);
      setTitleError(err instanceof ApiError ? err.detail : t("updateError"));
    }
  }

  function cancelEditTitle() {
    setDraftTitle(title);
    setEditing(false);
  }

  function handleChildRemoved(childId: string) {
    setChildren((prev) => prev.filter((c) => c.id !== childId));
  }

  function handleSegmented(delivery: BlockNode) {
    setChildren((prev) => [...prev.filter((c) => c.id !== delivery.id), delivery]);
    setExpanded(true);
  }

  async function handleDelete() {
    // Counted from LIVE state (`children`), not `node.children`: a tutor who
    // just deleted three lessons must not be told they're still at risk.
    const counts = countSubtree(children);
    const ok = await confirm({
      title: isRoot ? t("confirmDelete.rootTitle", { title }) : t("confirmDelete.title", { title }),
      body: (
        <>
          <p>{t("confirmDelete.lead", { title, kind: kindLabel })}</p>
          {counts.total > 0 && (
            <p>{t("confirmDelete.counts", { ...counts })}</p>
          )}
          <p className="font-medium text-destructive">
            {isRoot ? t("confirmDelete.rootWarning") : t("confirmDelete.irreversible")}
          </p>
        </>
      ),
      confirmLabel: t("confirmDelete.confirm"),
      destructive: true,
    });
    if (!ok) return;

    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteBlock(node.id);
      onRemoved(node.id);
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.detail : t("deleteError"));
      setDeleting(false);
    }
  }

  const headerRow = (
    <div className="flex flex-wrap items-center gap-2">
      {hasChildren ? (
        <CollapsibleTrigger
          aria-label={expanded ? t("collapse") : t("expand")}
          data-testid="block-card-toggle"
          className="text-muted-foreground hover:text-foreground"
        >
          {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </CollapsibleTrigger>
      ) : (
        <span className="inline-block size-4" />
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
              cancelEditTitle();
            }
          }}
          placeholder={t("editTitlePlaceholder")}
          data-testid="block-card-title-input"
          className="h-7 flex-1"
        />
      ) : (
        <button
          type="button"
          onClick={() => setEditing(true)}
          data-testid="block-card-title"
          className="flex-1 truncate text-left text-sm font-medium hover:underline"
        >
          {title}
        </button>
      )}

      <Badge variant={badgeVariant} data-testid="block-card-kind">
        {kindLabel}
      </Badge>
      {node.est_minutes != null && (
        <Badge variant="outline" data-testid="block-card-minutes">
          {t("estMinutes", { count: node.est_minutes })}
        </Badge>
      )}

      <div className="ml-auto flex items-center gap-1.5">
        {node.kind === "course" && (
          <SegmentDialog blockId={node.id} blockTitle={title} onSegmented={handleSegmented} />
        )}
        {isSegment && (
          <AttachArtifactDialog blockId={node.id} blockTitle={title} onAttached={handleArtifactAttached} />
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          data-testid="block-card-delete"
          disabled={deleting}
          onClick={handleDelete}
          aria-label={t("delete")}
        >
          {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
        </Button>
      </div>
    </div>
  );

  const errors = (
    <>
      {titleError && (
        <p role="alert" data-testid="block-card-title-error" className="text-xs text-destructive">
          {titleError}
        </p>
      )}
      {deleteError && (
        <p role="alert" data-testid="block-card-delete-error" className="text-xs text-destructive">
          {deleteError}
        </p>
      )}
    </>
  );

  const body = node.body && <p className="text-xs whitespace-pre-wrap text-muted-foreground">{node.body}</p>;

  // Only rendered for a "segment" node, and only once there's something to
  // show (a load in flight, an error, or at least one attached artifact) —
  // most segments have none, and a permanent "no artifacts" empty state on
  // every leaf of a curriculum tree would be pure visual noise. `Artifact`
  // itself already lazy-mounts a heavy `tab` kind (see `lazy-tab-view.tsx`),
  // so nothing extra is needed here to keep a tree full of attached tabs cheap.
  const artifactsSection = isSegment && (artifactsLoading || artifactsError || artifacts.length > 0) && (
    <div className="flex flex-col gap-1.5" data-testid="segment-artifacts">
      {artifactsLoading && <p className="text-xs text-muted-foreground">{t("artifactsLoading")}</p>}
      {artifactsError && (
        <p role="alert" data-testid="segment-artifacts-error" className="text-xs text-destructive">
          {artifactsError}
        </p>
      )}
      {artifacts.length > 0 && (
        <>
          <span className="text-xs font-medium text-muted-foreground">{t("attachedArtifactsHeading")}</span>
          <div className="flex flex-wrap gap-3" data-testid="segment-artifact-list">
            {artifacts.map((artifact) => (
              <div key={artifact.id} data-testid="segment-artifact-item">
                <Artifact kind={artifact.kind} spec={artifact.spec} />
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );

  if (!hasChildren) {
    return (
      <Card size="sm" data-testid="block-card" data-kind={node.kind} className="border-l-4" style={{ borderLeftColor: accent }}>
        <CardHeader className="gap-1.5">
          {headerRow}
          {errors}
        </CardHeader>
        {(body || artifactsSection) && (
          <CardContent className="flex flex-col gap-3">
            {body}
            {artifactsSection}
          </CardContent>
        )}
      </Card>
    );
  }

  return (
    <Card size="sm" data-testid="block-card" data-kind={node.kind} className="border-l-4" style={{ borderLeftColor: accent }}>
      <Collapsible open={expanded} onOpenChange={setExpanded}>
        <CardHeader className="gap-1.5">
          {headerRow}
          {errors}
        </CardHeader>
        <CollapsibleContent className="overflow-hidden transition-[height] duration-200 ease-out">
          <CardContent className="flex flex-col gap-3">
            {body}
            {artifactsSection}
            {contentChildren.length > 0 && (
              <div className="flex flex-col gap-2 border-l border-border pl-4" data-testid="block-card-children">
                {contentChildren.map((child) => (
                  <BlockCard key={child.id} node={child} onRemoved={handleChildRemoved} />
                ))}
              </div>
            )}
            {deliveryChildren.length > 0 && (
              <div className="flex flex-col gap-2">
                <span className="text-xs font-medium text-muted-foreground">{t("deliveryHeading")}</span>
                <div className="flex flex-col gap-2 border-l border-border pl-4" data-testid="block-card-delivery">
                  {deliveryChildren.map((child) => (
                    <BlockCard key={child.id} node={child} onRemoved={handleChildRemoved} />
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}
