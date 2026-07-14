"use client";

import { useCallback, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, BookOpen, Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { BlockCard } from "@/components/curriculum/block-card";
import { DraftProgressBar } from "@/components/curriculum/draft-progress-bar";
import { ApiError, addModule, getCurriculum, type BlockNode } from "@/lib/api";

interface TreeBoardProps {
  root: BlockNode;
  locale: string;
  onRootDeleted: () => void;
}

/** Replace one node anywhere in the tree, immutably. Returns the SAME object when
 * nothing matched, so React skips re-rendering every untouched branch — which
 * matters here: a 20-lesson curriculum is ~120 blocks and a draft poll lands every
 * 2 seconds. */
function replaceNode(node: BlockNode, next: BlockNode): BlockNode {
  if (node.id === next.id) return next;
  let changed = false;
  const children = node.children.map((child) => {
    const updated = replaceNode(child, next);
    if (updated !== child) changed = true;
    return updated;
  });
  return changed ? { ...node, children } : node;
}

function removeNode(node: BlockNode, id: string): BlockNode {
  const kept = node.children.filter((c) => c.id !== id);
  const children = kept.map((c) => removeNode(c, id));
  const changed =
    kept.length !== node.children.length || children.some((c, i) => c !== kept[i]);
  return changed ? { ...node, children } : node;
}

/** THE BOARD. Chris: "the component is neat but a bit messy, some more spacing
 * might be needed."
 *
 * A `max-w-3xl` reading column, because this is PROSE now — 2,200 words a lesson,
 * not a title and a badge. A curriculum spread across a 27-inch iMac is a
 * spreadsheet; it should read like the book he is writing.
 *
 * THE BOARD OWNS THE TREE, and every mutation flows back up to it. `BlockCard` used
 * to keep its own `children` state, which was fine while nothing outside it could
 * change a block — and became wrong the moment lessons started arriving from a
 * background draft while he was reading them. One tree, one owner, and the progress
 * poll refetches into it.
 *
 * The whole tree's artifacts arrive EMBEDDED (`BlockNode.artifacts`, from one
 * `WHERE block_id IN (...)`). No leaf fetches anything. That stampede of ~120
 * parallel `GET /artifacts?block_id=` requests IS the "Could not load attached
 * artifacts" error he kept seeing.
 */
export function TreeBoard({ root, locale, onRootDeleted }: TreeBoardProps) {
  const t = useTranslations("curricula.tree");

  // The board OWNS the tree from here on. `root` is the seed, not the source of
  // truth — a draft poll landing a new lesson, a rename, a delete all mutate this
  // copy. Switching curricula is a REMOUNT (`key={root.id}` at the call site), not a
  // prop sync: syncing props into state inside an effect is a cascading render, and
  // this tree is ~120 nodes deep.
  const [tree, setTree] = useState<BlockNode>(root);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setTree(await getCurriculum(root.id));
    } catch {
      // A failed refetch is not worth a banner: the lessons are being written by a
      // background task that does not care whether this tab can reach the API, the
      // progress bar has its own error line, and the next poll tries again.
    }
  }, [root.id]);

  const handleChanged = useCallback((next: BlockNode) => {
    setTree((prev) => replaceNode(prev, next));
  }, []);

  const handleRemoved = useCallback(
    (id: string) => {
      if (id === root.id) {
        onRootDeleted();
        return;
      }
      setTree((prev) => removeNode(prev, id));
    },
    [root.id, onRootDeleted],
  );

  async function handleAddModule() {
    setAdding(true);
    setError(null);
    try {
      await addModule(tree.id, { title: t("newModuleTitle") });
      // Refetch rather than splice: `add_module` renormalises every sibling's
      // `order` server-side, and a client-side splice would be guessing at the
      // result of that.
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("addModuleError"));
    } finally {
      setAdding(false);
    }
  }

  // What the board is ALREADY showing as drafted — the progress bar's baseline for
  // its very first poll (see its `readyInTree` prop).
  const readyInTree = tree.children.reduce(
    (n, module) =>
      n + module.children.filter((lesson) => lesson.meta?.draft_status === "ready").length,
    0,
  );

  const library = tree.meta?.library;
  const shape = tree.meta?.shape;
  // FALSE means the library did NOT fit whole and the lessons were drafted from
  // per-module retrieval instead. He is told, in words. A silent downgrade to
  // retrieval is the exact failure this stage exists to remove.
  const degraded = library != null && library.full_context === false;

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6" data-testid="tree-board">
      <header className="flex flex-col gap-3">
        {library && (
          <div
            data-testid="library-banner"
            data-degraded={degraded ? "true" : "false"}
            className={
              degraded
                ? "flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300"
                : "flex items-start gap-2 rounded-xl border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground"
            }
          >
            {degraded ? (
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            ) : (
              <BookOpen className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            )}
            <span>
              {degraded
                ? t("libraryDegraded", { tokens: library.token_count ?? 0 })
                : t("libraryWhole", {
                    sources: library.sources?.length ?? 0,
                    tokens: library.token_count ?? 0,
                  })}
              {shape?.target_words_per_lesson
                ? ` · ${t("shapeWords", { words: shape.target_words_per_lesson })}`
                : ""}
            </span>
          </div>
        )}

        <DraftProgressBar rootId={tree.id} readyInTree={readyInTree} onLessonReady={refresh} />
      </header>

      <BlockCard
        key={tree.id}
        node={tree}
        locale={locale}
        isRoot
        onChanged={handleChanged}
        onRemoved={handleRemoved}
        onRefresh={refresh}
      />

      <footer className="flex flex-col gap-1.5">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="self-start"
          data-testid="board-add-module"
          disabled={adding}
          onClick={handleAddModule}
        >
          {adding ? <Loader2 className="animate-spin" /> : <Plus />}
          {t("addModule")}
        </Button>
        {error && (
          <p role="alert" data-testid="board-add-error" className="text-xs text-destructive">
            {error}
          </p>
        )}
      </footer>
    </div>
  );
}
