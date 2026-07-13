"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Loader2, Quote } from "lucide-react";
import { Button } from "@/components/ui/button";
import { authorFromSelection, getJob } from "@/lib/api";

/** Poll cadence + cap while a lesson-drafting job is in flight — same
 * `POLL_INTERVAL_MS`/2s cadence and `MAX_POLLS`/150-poll (~5 min) cap as
 * `components/curriculum/generate-dialog.tsx`'s identical loop (Plan 10
 * Task 5 reuses that pattern verbatim rather than inventing a second one):
 * `draft_lesson_from_selection` (`app.lessons.draft`) is, like curriculum
 * generation, a blocking guided-JSON LLM call typically taking 30-60s, comfortably
 * inside this cap. */
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 150;

interface SelectionActionProps {
  sourceId: string;
}

type Status = "idle" | "drafting" | "error";

interface Selection {
  text: string;
  pageFrom: number;
  pageTo: number;
}

/** Walks up from a `window.getSelection()` endpoint (`anchorNode`/
 * `focusNode`) to the nearest ancestor carrying `data-reader-text-page`
 * (set by `ReaderPane` on its text pane, one per page) and returns that
 * page's number, or `null` if the node isn't inside any page's text pane at
 * all (the scan's alt text, the page-marker headings, the page-indicator
 * badge, nav chrome — none of those carry the attribute, so a selection
 * landing there is correctly treated as "not a real passage selection"). */
function readerPageOf(node: Node | null): number | null {
  let el: Element | null = node instanceof Element ? node : node?.parentElement ?? null;
  while (el) {
    const raw = el.getAttribute("data-reader-text-page");
    if (raw != null) return Number(raw);
    el = el.parentElement;
  }
  return null;
}

/** The Reader's one action: "select a passage, author a lesson from it"
 * (spec — the passage IS how a lesson gets grounded/cited later). Listens
 * for `selectionchange` on `document` (not a `mouseup` handler on any one
 * pane — `selectionchange` also fires for keyboard/touch selection, and
 * `reader.spec.ts` drives it by dispatching this event directly) and shows
 * a floating bar only while the live selection is non-empty AND both its
 * ends land inside SOME page's text pane.
 *
 * G4 (Plan 12 Task 4) — THE POINT OF THIS REWRITE: the continuous-scroll
 * Reader stacks every page's text pane in the same document, so a selection
 * can start in one page's pane and end in another's. `readerPageOf` reads
 * off each end independently; `page_from`/`page_to` is
 * `[min, max]` of the two, so it's correct regardless of which direction
 * the tutor dragged (top-to-bottom or bottom-to-top). A selection that
 * never leaves one page still resolves to `page_from === page_to` — the
 * exact single-page shape this component always sent, unchanged.
 *
 * There is no more `key={pageNo}` remount to reset state on a page turn —
 * continuous scroll has no "current page" to key by. Local state is reset
 * directly instead (a fresh `selectionchange` firing empty, or navigating
 * away on success).
 *
 * ASYNC since Plan 10 Task 1/5: `authorFromSelection` enqueues a
 * `GenerationJob(kind="lesson")` and returns 202 almost immediately — this
 * polls `getJob` (same cadence/cap as `generate-dialog.tsx`'s curriculum
 * poll, see `POLL_INTERVAL_MS`/`MAX_POLLS` above) behind an honest "drafting"
 * spinner (a REAL LLM call, ~30-60s, never a frozen-looking button), and on
 * success navigates straight into the new lesson's editor
 * (`/[locale]/lessons/{result_root_id}`) — there is nothing left to confirm
 * in place once the lesson exists. A failed job surfaces the server's own
 * error message and offers Retry, re-running `handleAuthor` against the
 * SAME still-selected passage rather than making the tutor re-select it.
 *
 * Deliberately NOT an OCR editor (spec D5) — this only ever reads
 * `selection.toString()` and posts it verbatim; there is no way to change
 * what gets sent. */
export function SelectionAction({ sourceId }: SelectionActionProps) {
  const t = useTranslations("library.reader");
  const locale = useLocale();
  const router = useRouter();
  const [selection, setSelection] = useState<Selection | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  // Mirrors `status` for the `selectionchange` listener below, which is
  // subscribed once (empty-ish dep array) rather than resubscribed on every
  // status flip — a plain closure over `status` would go stale the moment
  // this effect stops re-running, so the listener reads this ref instead.
  const statusRef = useRef<Status>("idle");
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  useEffect(() => {
    function handleSelectionChange() {
      // A draft already in flight is left alone here — a `selectionchange`
      // the click itself causes (or a stray one mid-poll) must never abort
      // or reset the in-flight job.
      if (statusRef.current === "drafting") return;

      const live = window.getSelection();
      const text = live?.toString().trim() ?? "";
      const anchorPage = readerPageOf(live?.anchorNode ?? null);
      const focusPage = readerPageOf(live?.focusNode ?? null);
      const validRange =
        !!live && live.rangeCount > 0 && anchorPage != null && focusPage != null;

      if (text && validRange) {
        setSelection({
          text,
          pageFrom: Math.min(anchorPage!, focusPage!),
          pageTo: Math.max(anchorPage!, focusPage!),
        });
        setStatus("idle");
        setError(null);
      } else {
        setSelection(null);
      }
    }

    document.addEventListener("selectionchange", handleSelectionChange);
    return () => document.removeEventListener("selectionchange", handleSelectionChange);
  }, []);

  const handleAuthor = useCallback(async () => {
    if (!selection) return;
    setStatus("drafting");
    setError(null);
    try {
      const { job_id } = await authorFromSelection(
        sourceId, selection.pageFrom, selection.pageTo, selection.text,
      );

      // Poll until the job reaches a terminal status or we hit the cap —
      // see the `POLL_INTERVAL_MS`/`MAX_POLLS` docstring above. Same loop
      // shape as `generate-dialog.tsx`'s curriculum poll.
      let job = await getJob(job_id);
      let polls = 1;
      while (job.status !== "succeeded" && job.status !== "failed" && polls < MAX_POLLS) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        job = await getJob(job_id);
        polls++;
      }

      if (job.status === "succeeded" && job.result_root_id) {
        router.push(`/${locale}/lessons/${job.result_root_id}`);
        // Deliberately no local state reset here: this component is about
        // to be unmounted by the navigation above, so there is nothing left
        // to show "done" in — unlike the old synchronous stub, there is no
        // page to stay on.
      } else if (job.status === "failed") {
        setStatus("error");
        setError(job.error ?? t("authorError"));
      } else {
        // Cap exceeded — the job keeps running server-side (same posture as
        // `generate-dialog.tsx`'s `stillGenerating`); give up waiting here
        // rather than polling forever.
        setStatus("error");
        setError(t("authorStillDrafting"));
      }
    } catch {
      setStatus("error");
      setError(t("authorError"));
    }
  }, [selection, sourceId, router, locale, t]);

  if (!selection) return null;
  const spansPages = selection.pageFrom !== selection.pageTo;

  return (
    <div
      data-testid="selection-action"
      className="fixed inset-x-0 bottom-6 z-20 flex justify-center px-4"
    >
      <div className="flex max-w-lg flex-col gap-2 rounded-2xl border border-border bg-popover px-4 py-2 text-popover-foreground shadow-lg">
        <div className="flex items-center gap-3">
          <Quote className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="hidden min-w-0 flex-1 flex-col sm:flex">
            <span className="truncate text-sm text-muted-foreground">
              &ldquo;{selection.text.length > 60 ? `${selection.text.slice(0, 60)}…` : selection.text}&rdquo;
            </span>
            {spansPages && (
              <span data-testid="selection-range" className="text-xs text-muted-foreground/70">
                {t("selectionSpans", { from: selection.pageFrom, to: selection.pageTo })}
              </span>
            )}
          </span>
          <Button
            type="button"
            size="sm"
            data-testid="author-lesson"
            disabled={status === "drafting"}
            onClick={handleAuthor}
          >
            {status === "drafting" && <Loader2 className="size-3.5 animate-spin" />}
            {t("authorLesson")}
          </Button>
        </div>

        {status === "drafting" && (
          <span
            role="status"
            data-testid="author-drafting"
            className="flex items-center gap-2 text-xs text-muted-foreground"
          >
            <Loader2 className="size-3 shrink-0 animate-spin" />
            {t("authorDrafting")}
          </span>
        )}

        {status === "error" && (
          <div className="flex items-center gap-2">
            <span role="alert" data-testid="author-error" className="text-sm text-destructive">
              {error}
            </span>
            <Button type="button" size="sm" variant="outline" data-testid="author-retry" onClick={handleAuthor}>
              {t("authorRetry")}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
