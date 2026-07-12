"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { useTranslations } from "next-intl";
import { CheckCircle2, Loader2, Quote } from "lucide-react";
import { Button } from "@/components/ui/button";
import { authorFromSelection } from "@/lib/api";

/** How long the "saved" confirmation stays up after a successful author-
 * from-selection call before the floating bar quietly retracts — long
 * enough to read, short enough not to linger over a calm reading surface. */
const CONFIRMATION_MS = 4000;

interface SelectionActionProps {
  sourceId: string;
  pageNo: number;
  /** The text pane's own DOM node (`ReaderPane`'s `page-text`, via the
   * reader page's shared ref) — the ONLY thing that makes this component
   * show its button for a given `window.getSelection()`; a selection
   * elsewhere on the page (the scan's alt text, the page-indicator, nav
   * chrome) is deliberately ignored. */
  textRef: RefObject<HTMLDivElement | null>;
}

type Status = "idle" | "submitting" | "done" | "error";

/** The Reader's one action: "select a passage, author a lesson from it"
 * (spec — the passage IS how a lesson gets grounded/cited later). Listens
 * for `selectionchange` on `document` (not a `mouseup` handler on the pane
 * — `selectionchange` also fires for keyboard/touch selection, and the
 * reader.spec.ts test drives it by dispatching this event directly) and
 * shows a floating bar only while the live selection is non-empty AND
 * lives inside `textRef`.
 *
 * The reader page renders this with `key={pageNo}` — turning the page must
 * never leave a stale selection or an in-flight confirmation on screen, and
 * remounting a fresh instance resets all of this component's local state
 * for free. That's also *why* there's no `pageNo`-watching `useEffect` here
 * calling `setState` to reset things by hand (`react-hooks/set-state-in-
 * effect` flags exactly that "reset on prop change" shape — see
 * `chat-panel.tsx`'s `startSession` comment for the same rule elsewhere in
 * this codebase); the `key` remount is the idiomatic fix instead.
 *
 * Deliberately NOT an OCR editor (spec D5) — this only ever reads
 * `selection.toString()` and posts it verbatim; there is no way to change
 * what gets sent. */
export function SelectionAction({ sourceId, pageNo, textRef }: SelectionActionProps) {
  const t = useTranslations("library.reader");
  const [selectedText, setSelectedText] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [sourceTitle, setSourceTitle] = useState<string | null>(null);
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Mirrors `status` for the `selectionchange` listener below, which is
  // subscribed once (empty-ish dep array) rather than resubscribed on every
  // status flip — a plain closure over `status` would go stale the moment
  // this effect stops re-running, so the listener reads this ref instead.
  const statusRef = useRef<Status>("idle");
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const clearDismissTimer = useCallback(() => {
    if (dismissTimer.current) {
      clearTimeout(dismissTimer.current);
      dismissTimer.current = null;
    }
  }, []);

  useEffect(() => {
    function handleSelectionChange() {
      // A confirmation already up (`status === "done"`) is left alone here —
      // it retracts on its own timer below, not because the browser
      // selection happened to collapse (e.g. from the click itself).
      if (statusRef.current === "submitting" || statusRef.current === "done") return;

      const selection = window.getSelection();
      const text = selection?.toString().trim() ?? "";
      const pane = textRef.current;
      const insidePane =
        !!pane &&
        !!selection &&
        selection.rangeCount > 0 &&
        pane.contains(selection.anchorNode) &&
        pane.contains(selection.focusNode);

      if (text && insidePane) {
        setSelectedText(text);
        setStatus("idle");
      } else {
        setSelectedText(null);
      }
    }

    document.addEventListener("selectionchange", handleSelectionChange);
    return () => document.removeEventListener("selectionchange", handleSelectionChange);
  }, [textRef]);

  useEffect(() => clearDismissTimer, [clearDismissTimer]);

  async function handleAuthor() {
    if (!selectedText) return;
    setStatus("submitting");
    try {
      const result = await authorFromSelection(sourceId, pageNo, selectedText);
      setSourceTitle(result.source_title);
      setStatus("done");
      dismissTimer.current = setTimeout(() => {
        setStatus("idle");
        setSelectedText(null);
      }, CONFIRMATION_MS);
    } catch {
      setStatus("error");
    }
  }

  if (!selectedText) return null;

  return (
    <div
      data-testid="selection-action"
      className="fixed inset-x-0 bottom-6 z-20 flex justify-center px-4"
    >
      <div className="flex max-w-lg items-center gap-3 rounded-full border border-border bg-popover px-4 py-2 text-popover-foreground shadow-lg">
        {status === "done" ? (
          <span className="flex items-center gap-2 text-sm text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="size-4 shrink-0" />
            {t("authorSaved", { source: sourceTitle ?? "" })}
          </span>
        ) : (
          <>
            <Quote className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="hidden truncate text-sm text-muted-foreground sm:inline">
              &ldquo;{selectedText.length > 60 ? `${selectedText.slice(0, 60)}…` : selectedText}&rdquo;
            </span>
            <Button
              type="button"
              size="sm"
              data-testid="author-lesson"
              disabled={status === "submitting"}
              onClick={handleAuthor}
            >
              {status === "submitting" && <Loader2 className="size-3.5 animate-spin" />}
              {t("authorLesson")}
            </Button>
            {status === "error" && (
              <span role="alert" className="text-sm text-destructive">
                {t("authorError")}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  );
}
