"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { GitCompare, Loader2, Undo2, Wand2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { WhatChanged } from "@/components/curriculum/what-changed";
import { ApiError, refineBlock, undoRefine, type BlockNode } from "@/lib/api";

interface ExtendWithChatProps {
  blockId: string;
  /** True once this block has a `meta.prev_body` stashed — i.e. there is
   * something to undo. Comes off the tree, so it survives a reload: the Undo
   * button is still there tomorrow morning. */
  canUndo: boolean;
  /** The two sides of the diff, both straight off the tree. `prevBody` is the
   * same `meta.prev_body` that drives `canUndo`, so the chip and the Undo
   * button appear and disappear together — which is correct: they are two
   * answers to the same question, "what did the AI just do here". */
  prevBody?: string | null;
  body?: string | null;
  /** `meta.refine_instruction` — what he typed to cause this. */
  instruction?: string | null;
  onRefined: (block: BlockNode) => void;
}

/** "CHANGE THIS AND GIVE MORE DETAIL ABOUT THE AMP" — and it does that.
 *
 * Chris asked for exactly this button. Shipped as a PATCH with an Undo rather than
 * an accept/reject diff table, for two reasons that are practical rather than
 * philosophical: a diff over prose he has never read is not a review surface, it is
 * homework; and "stream it into a live preview" sounds free and is not — this is a
 * schema-constrained call, so what would stream is raw JSON, and a live preview of
 * raw JSON is worse than a spinner.
 *
 * The instruction box is collapsed behind its own button. Twenty lessons times four
 * segments is eighty always-open textareas, and the board would read as a form.
 */
export function ExtendWithChat({
  blockId,
  canUndo,
  prevBody,
  body,
  instruction: pastInstruction,
  onRefined,
}: ExtendWithChatProps) {
  const t = useTranslations("curricula.extend");

  const [open, setOpen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [diffOpen, setDiffOpen] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const text = instruction.trim();
    if (!text || busy) return;

    setBusy(true);
    setError(null);
    try {
      const block = await refineBlock(blockId, text);
      onRefined(block);
      setInstruction("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setBusy(false);
    }
  }

  async function handleUndo() {
    setBusy(true);
    setError(null);
    try {
      onRefined(await undoRefine(blockId));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("undoError"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          data-testid="extend-toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <Wand2 />
          {t("trigger")}
        </Button>

        {/* «Τι άλλαξε;» sits BEFORE Undo on purpose: reading what happened is
            the step that decides whether to undo, so the order matches the
            decision. Same `canUndo` gate — both are answers to "what did the
            AI just do here", and neither is meaningful without a stashed
            previous body. */}
        {canUndo && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-testid="what-changed-trigger"
            disabled={busy}
            onClick={() => setDiffOpen(true)}
          >
            <GitCompare />
            {t("whatChanged")}
          </Button>
        )}

        {canUndo && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            data-testid="extend-undo"
            disabled={busy}
            onClick={handleUndo}
          >
            {busy ? <Loader2 className="animate-spin" /> : <Undo2 />}
            {t("undo")}
          </Button>
        )}
      </div>

      {canUndo && (
        <WhatChanged
          open={diffOpen}
          onOpenChange={setDiffOpen}
          before={prevBody ?? ""}
          after={body ?? ""}
          instruction={pastInstruction}
        />
      )}

      {open && (
        <form onSubmit={handleSubmit} className="flex flex-col gap-2" data-testid="extend-form">
          <Textarea
            autoFocus
            rows={2}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder={t("placeholder")}
            data-testid="extend-instruction"
            disabled={busy}
          />
          <div className="flex items-center gap-2 self-end">
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setOpen(false)}>
              {t("cancel")}
            </Button>
            <Button type="submit" size="sm" disabled={busy || !instruction.trim()} data-testid="extend-submit">
              {busy && <Loader2 className="animate-spin" />}
              {t("submit")}
            </Button>
          </div>
        </form>
      )}

      {busy && !open && (
        <p className="text-xs text-muted-foreground" data-testid="extend-busy">
          {t("working")}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-destructive" data-testid="extend-error">
          {error}
        </p>
      )}
    </div>
  );
}
