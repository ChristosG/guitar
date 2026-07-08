"use client";

import { BlockCard } from "@/components/curriculum/block-card";
import type { BlockNode } from "@/lib/api";

interface TreeBoardProps {
  root: BlockNode;
  onRootDeleted: () => void;
}

/** Thin wrapper around the recursive `BlockCard` tree: all the actual
 * rendering/mutation logic lives there (see its docstring). `key={root.id}`
 * forces a fresh mount whenever the active curriculum changes (a new
 * generation or a different template picked from the list), so no stale
 * local edit state from a previous tree ever leaks into the next one. */
export function TreeBoard({ root, onRootDeleted }: TreeBoardProps) {
  return (
    <div data-testid="tree-board" className="flex flex-col gap-3">
      <BlockCard key={root.id} node={root} onRemoved={onRootDeleted} />
    </div>
  );
}
