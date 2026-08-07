"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Undo2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { diffProse, type DiffBlock } from "@/lib/prose-diff";
import { cn } from "@/lib/utils";

interface WhatChangedProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  before: string;
  after: string;
  /** What the tutor typed to cause this, when we have it (`meta.refine_instruction`
   * / `meta.revise_instruction`). Shown FIRST, because "what did I ask for" is
   * the question a diff is being read to answer. */
  instruction?: string | null;
  /** Present only for a whole LESSON, where a snapshot of the previous segment
   * set exists. Undefined everywhere else — a segment already has its own Undo,
   * and this panel does not duplicate it. */
  onRestore?: () => Promise<void>;
  restoreLabel?: string;
}

/** «ΤΙ ΑΛΛΑΞΕ;» — the diff, as an INSPECTOR rather than a review gate.
 *
 * Chris chose this shape explicitly: the AI's change lands as it always did,
 * and this answers "what did it actually do to my text" afterwards. That is
 * why it reads `meta.prev_body`/`prev_segments` — state that is already there —
 * instead of introducing a staged-proposal lifecycle that would have to survive
 * a closed laptop and a restarted app.
 *
 * `refine.py`'s own docstring argues AGAINST a diff here, and it is worth
 * knowing why that argument does not apply: it says "a diff table over prose
 * the tutor has never read is not a review surface, it is homework". True for
 * freshly generated content. This panel is for the opposite case — prose he has
 * already read and curated, which he then asked the AI to change. There the
 * diff IS the review surface.
 *
 * A DIALOG RATHER THAN A POPOVER, and that is a Phase-1 debt being repaid: both
 * are portaled, and until the zoom fix landed every portaled element drifted off
 * screen at any zoom above 1. The dialog is the one already exercised by the
 * rename flow and the confirm box, so it is the one with the most evidence
 * behind it.
 */
export function WhatChanged({
  open,
  onOpenChange,
  before,
  after,
  instruction,
  onRestore,
  restoreLabel,
}: WhatChangedProps) {
  const t = useTranslations("curricula.extend");
  const [restoring, setRestoring] = useState(false);

  // The whole engine, memoised on the two texts. A 2,000-word lesson is a
  // ~20x20 alignment — microseconds — but this also keeps the render pure.
  const diff = useMemo(() => diffProse(before, after), [before, after]);

  const rendered = useMemo(() => collapseUnchanged(diff.blocks), [diff.blocks]);

  async function handleRestore() {
    if (!onRestore) return;
    setRestoring(true);
    try {
      await onRestore();
      onOpenChange(false);
    } finally {
      setRestoring(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl" data-testid="what-changed-dialog">
        <DialogHeader>
          <DialogTitle>{t("whatChangedTitle")}</DialogTitle>
        </DialogHeader>

        <DialogBody className="max-h-[70vh] overflow-y-auto">
          {instruction && (
            <p className="mb-3 text-sm text-muted-foreground" data-testid="what-changed-instruction">
              <span className="font-medium">{t("whatChangedAsked")}</span> {instruction}
            </p>
          )}

          {/* THE HONEST BRANCH. When the model regenerated rather than edited,
              a word diff is noise — so we say so and show both texts whole
              instead of painting the whole thing red and green. This is the
              case Chris was actually worried about. */}
          {diff.rewritten ? (
            <div className="flex flex-col gap-3" data-testid="what-changed-rewritten">
              <p className="rounded-md bg-muted p-2 text-sm text-muted-foreground">
                {t("whatChangedRewritten")}
              </p>
              <Section label={t("whatChangedBefore")}>{before}</Section>
              <Section label={t("whatChangedAfter")}>{after}</Section>
            </div>
          ) : rendered.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="what-changed-nothing">
              {t("whatChangedNothing")}
            </p>
          ) : (
            <div className="flex flex-col gap-2 text-sm leading-relaxed" data-testid="what-changed-diff">
              {rendered.map((item, i) =>
                item.kind === "collapsed" ? (
                  <p key={i} className="text-xs text-muted-foreground" data-testid="what-changed-unchanged">
                    {t("whatChangedUnchanged", { count: item.count })}
                  </p>
                ) : (
                  <Paragraph key={i} block={item.block} />
                ),
              )}
            </div>
          )}
        </DialogBody>

        <DialogFooter>
          {onRestore && (
            <Button
              type="button"
              variant="outline"
              disabled={restoring}
              data-testid="what-changed-restore"
              onClick={handleRestore}
            >
              {restoring ? <Loader2 className="animate-spin" /> : <Undo2 />}
              {restoreLabel}
            </Button>
          )}
          <Button type="button" onClick={() => onOpenChange(false)}>
            {t("whatChangedClose")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <p className="whitespace-pre-wrap rounded-md border border-border p-2 text-sm">{children}</p>
    </div>
  );
}

const ADD = "rounded-sm bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
const DEL = "rounded-sm bg-destructive/15 text-destructive line-through";

function Paragraph({ block }: { block: DiffBlock }) {
  if (block.kind === "add") {
    return (
      <p className={cn("whitespace-pre-wrap px-1", ADD)} data-testid="diff-add">
        {block.text}
      </p>
    );
  }
  if (block.kind === "del") {
    return (
      <p className={cn("whitespace-pre-wrap px-1", DEL)} data-testid="diff-del">
        {block.text}
      </p>
    );
  }
  if (block.kind === "same") {
    return <p className="whitespace-pre-wrap px-1 text-muted-foreground">{block.text}</p>;
  }
  return (
    <p className="whitespace-pre-wrap px-1" data-testid="diff-edit">
      {block.pieces.map((piece, i) =>
        piece.type === "same" ? (
          <span key={i}>{piece.text}</span>
        ) : (
          <span key={i} className={piece.type === "add" ? ADD : DEL} data-testid={`diff-${piece.type}`}>
            {piece.text}
          </span>
        ),
      )}
    </p>
  );
}

type Rendered = { kind: "block"; block: DiffBlock } | { kind: "collapsed"; count: number };

/** Runs of untouched paragraphs collapse to one line.
 *
 * Without this the panel re-prints the entire lesson to show that one sentence
 * moved, which is the "homework" failure `refine.py` warned about arriving by a
 * different door. What he opened this for is what MOVED. */
function collapseUnchanged(blocks: DiffBlock[]): Rendered[] {
  const out: Rendered[] = [];
  let run = 0;
  for (const block of blocks) {
    if (block.kind === "same") {
      run++;
      continue;
    }
    if (run > 0) {
      out.push({ kind: "collapsed", count: run });
      run = 0;
    }
    out.push({ kind: "block", block });
  }
  if (run > 0) out.push({ kind: "collapsed", count: run });
  // All-unchanged reads better as one honest sentence than as "N paragraphs
  // unchanged" with nothing beside it.
  if (out.length === 1 && out[0].kind === "collapsed") return [];
  return out;
}
