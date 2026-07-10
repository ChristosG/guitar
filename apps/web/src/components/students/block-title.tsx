"use client";

import { useEffect, useState } from "react";
import { getBlock } from "@/lib/api";

interface BlockTitleProps {
  blockId: string;
}

/** Resolves a bare block id (as stored on `ProgressOut.block_id`/
 * `LessonLogOut.session_block_id` — the student-detail aggregate has no
 * batch "titles for these ids" route) to its Block's title via `GET
 * /blocks/{id}`, one request per row — same "acceptable for this PoC's
 * scale, one fetch per item" trade-off `curriculum/block-card.tsx`'s own
 * per-node `fetchArtifacts` already documents for an analogous per-node
 * fetch. Shows the raw id itself while loading/on failure (never a blank or
 * flashing placeholder, and still a genuinely useful value — an id IS a
 * valid, if unfriendly, way to locate the block) rather than a separate
 * "Loading…" string. Used by both `progress-row.tsx` and
 * `lesson-log-list.tsx`. */
export function BlockTitle({ blockId }: BlockTitleProps) {
  const [title, setTitle] = useState<string | null>(null);

  // Every setState call stays lexically inside the `.then` callback (never a
  // bare statement directly in the effect body), which is what react-hooks/
  // set-state-in-effect actually checks for — same convention `students/
  // page.tsx`'s own `fetchStudents` docstring documents. No explicit
  // "reset to null" on `blockId` change: every caller mounts one `BlockTitle`
  // per row keyed by that row's own id, so a live blockId swap on an
  // already-mounted instance never actually happens in practice.
  useEffect(() => {
    getBlock(blockId)
      .then((block) => setTitle(block.title))
      .catch(() => {});
  }, [blockId]);

  return <>{title ?? blockId}</>;
}
