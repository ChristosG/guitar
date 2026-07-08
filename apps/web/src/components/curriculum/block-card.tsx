"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronRight, Loader2, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { SegmentDialog } from "@/components/curriculum/segment-dialog";
import { ApiError, deleteBlock, updateBlock, type BlockNode } from "@/lib/api";

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
 */
export function BlockCard({ node, onRemoved }: BlockCardProps) {
  const t = useTranslations("curricula.tree");

  const [title, setTitle] = useState(node.title);
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(node.title);
  const [titleError, setTitleError] = useState<string | null>(null);

  const [children, setChildren] = useState(node.children);
  const [expanded, setExpanded] = useState(true);

  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const contentChildren = children.filter((c) => c.plane === "content");
  const deliveryChildren = children.filter((c) => c.plane !== "content");
  const hasChildren = children.length > 0;

  const kindKey = `kinds.${node.kind}`;
  const kindLabel = t.has(kindKey) ? t(kindKey) : node.kind;
  const badgeVariant = KIND_BADGE_VARIANT[node.kind] ?? "outline";
  const accent = KIND_ACCENT[node.kind] ?? "var(--border)";

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

  if (!hasChildren) {
    return (
      <Card size="sm" data-testid="block-card" data-kind={node.kind} className="border-l-4" style={{ borderLeftColor: accent }}>
        <CardHeader className="gap-1.5">
          {headerRow}
          {errors}
        </CardHeader>
        {body && <CardContent>{body}</CardContent>}
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
