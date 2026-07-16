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
  /** How many lessons the rendered tree shows as `queued`. Its ONE job is to be an
   * effect dependency: when it RISES (an AI-added module landed, a lesson was
   * added, Deepen re-queued one), a poll loop that had parked itself on `done`
   * re-arms — every enqueue path revives the bar through the same signal, the
   * tree itself. */
  queuedInTree: number;
  /** Called when a poll shows more lessons finished than the board is showing — it
   * refetches the tree. Returns whether the refetch actually landed: the poll only
   * advances its baseline on success, so a refetch that failed is retried on the
   * next tick instead of being silently skipped forever (which mattered most on
   * the FINAL tick — `done` used to park the loop with the board still stale). */
  onLessonReady: () => Promise<boolean> | boolean;
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
export function DraftProgressBar({ rootId, readyInTree, queuedInTree, onLessonReady }: DraftProgressBarProps) {
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
      // The baseline only advances when the board ACTUALLY refetched. A failed
      // refetch keeps the old baseline, so the very next tick tries again — and
      // `synced=false` below refuses to park, so there IS a next tick even when
      // this poll also said `done`.
      let synced = true;
      if (next.ready > shown) synced = (await onLessonReady()) !== false;
      if (synced) lastReady.current = next.ready;
      return { next, synced };
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
      const result = await poll();
      if (!alive) return;
      // Park only when nothing is in flight AND the board is showing it all.
      if (result && result.next.done && result.synced) return;
      timer = setTimeout(tick, POLL_INTERVAL_MS);
    }
    tick();

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
    // `resuming` and `queuedInTree` are in the deps ON PURPOSE: pressing Resume,
    // an AI-generated module landing, a hand-added lesson, or a Deepen click all
    // put lessons back in flight after this loop may have parked itself on
    // `done` — each one changes a dep and re-arms the poll. Without them the bar
    // would sit frozen while the lessons were actually being written.
  }, [poll, resuming, queuedInTree]);

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
