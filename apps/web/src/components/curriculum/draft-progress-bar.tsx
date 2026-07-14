"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, RotateCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ApiError, getCurriculumProgress, resumeCurriculumDraft, type DraftProgress } from "@/lib/api";
import { cn } from "@/lib/utils";

const POLL_INTERVAL_MS = 2000;

interface DraftProgressBarProps {
  rootId: string;
  /** How many lessons the tree the board is CURRENTLY RENDERING already shows as
   * drafted. It is the baseline the first poll is compared against, and without it
   * the board goes stale in a way that is easy to miss and impossible to explain:
   * open a curriculum whose lessons all finished a minute ago, the first poll says
   * "4 of 4 ready", there is no previous count to be greater than, so no refetch
   * fires — and every row still reads "queued" under a full progress bar. */
  readyInTree: number;
  /** Called when a poll shows more lessons finished than the board is showing — it
   * refetches the tree, and the tutor watches module 1 fill in while module 5 is
   * still being written. */
  onLessonReady: () => void;
}

/** THE FLAGSHIP PROOF, RENDERED: he can read module 1 while module 5 is still
 * writing.
 *
 * Polls `GET /curricula/{root}/progress` — a GROUP BY over the lesson blocks, not
 * a counter on the job row (the blocks are what he is looking at, so the blocks
 * are what we count; a counter would disagree with the tree the first time he
 * deleted a lesson mid-draft).
 *
 * IT STOPS POLLING WHEN THERE IS NOTHING LEFT TO WATCH, and that is not an
 * optimization — a poll that never stops is a request every 2 seconds, forever, on
 * a board he left open, against a connection pool the draft workers are also using.
 * `done` (nothing queued, nothing drafting) parks it.
 *
 * A `failed` or still-`queued` lesson gets a RESUME button rather than a retry
 * daemon. `POST /curricula/{root}/draft` is a REQUEST, and a request is the only
 * thing in this app that can schedule a background task — there is no worker
 * process, and Stage 6 deliberately did not add one. It also covers the rate-limit
 * case (a 429'd lesson is `queued`, never `failed`), a lesson he added to the
 * outline after the fact, and a lesson he wants deepened: all the same state, all
 * one button.
 */
export function DraftProgressBar({ rootId, readyInTree, onLessonReady }: DraftProgressBarProps) {
  const t = useTranslations("curricula.progress");

  const [progress, setProgress] = useState<DraftProgress | null>(null);
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The last `ready` count we told the board about. A ref, not state: it must not
  // itself trigger a render, and it must survive the poll's closure without
  // re-arming the effect. It starts at what the BOARD is showing, not at zero.
  const lastReady = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      const next = await getCurriculumProgress(rootId);
      setProgress(next);
      setError(null);
      const shown = lastReady.current ?? readyInTree;
      if (next.ready > shown) onLessonReady();
      lastReady.current = next.ready;
      return next;
    } catch (err) {
      // A failed poll is not a failed draft. Say nothing loud, keep polling — the
      // lessons are being written by a background task that does not care whether
      // this browser tab can reach the API.
      setError(err instanceof ApiError ? err.detail : t("pollError"));
      return null;
    }
  }, [rootId, readyInTree, onLessonReady, t]);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function tick() {
      const next = await poll();
      if (!alive) return;
      // Park when there is nothing in flight. Resume re-arms it below.
      if (next && next.done) return;
      timer = setTimeout(tick, POLL_INTERVAL_MS);
    }
    tick();

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // `resuming` is in the deps ON PURPOSE: pressing Resume flips it true then
    // false, which re-runs this effect and re-arms a poll loop that had parked
    // itself on `done`. Without it the bar would sit at "12 of 20" showing nothing
    // while the lessons he just re-enqueued were actually being written.
  }, [poll, resuming]);

  if (!progress || progress.total === 0) return null;

  const { total, ready, drafting, queued, failed, done } = progress;
  const pct = Math.round((ready / total) * 100);
  const canResume = queued > 0 || failed > 0;

  async function handleResume() {
    setResuming(true);
    setError(null);
    try {
      await resumeCurriculumDraft(rootId);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("resumeError"));
    } finally {
      setResuming(false);
    }
  }

  return (
    <section
      data-testid="draft-progress"
      data-done={done ? "true" : "false"}
      aria-live="polite"
      className="flex flex-col gap-2.5 rounded-2xl border border-border bg-card px-4 py-3.5 ring-1 ring-foreground/5"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="flex items-center gap-2 text-sm font-medium">
          {!done && <Loader2 className="size-4 shrink-0 animate-spin text-primary" aria-hidden />}
          <span data-testid="draft-progress-count">{t("count", { ready, total })}</span>
        </span>

        <span className="text-xs text-muted-foreground" data-testid="draft-progress-states">
          {t("states", { drafting, queued, failed })}
        </span>

        {canResume && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="ml-auto"
            data-testid="draft-resume"
            disabled={resuming}
            onClick={handleResume}
          >
            {resuming ? <Loader2 className="animate-spin" /> : <RotateCw />}
            {t("resume")}
          </Button>
        )}
      </div>

      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="presentation">
        <div
          data-testid="draft-progress-fill"
          className={cn(
            "h-full rounded-full transition-[width] duration-500 ease-out",
            failed > 0 ? "bg-amber-500" : "bg-primary",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>

      {failed > 0 && (
        <p className="text-xs text-amber-600 dark:text-amber-400" data-testid="draft-progress-failed">
          {t("failedHint", { failed })}
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-destructive" data-testid="draft-progress-error">
          {error}
        </p>
      )}
    </section>
  );
}
