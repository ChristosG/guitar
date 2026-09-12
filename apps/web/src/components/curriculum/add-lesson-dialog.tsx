"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Plus, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { jobErrorText } from "@/lib/job-errors";
import { addLesson, generateLesson, getJob } from "@/lib/api";

/** The API's own floor (`LessonGenerateRequest.brief`, `min_length=10`),
 * enforced here so he learns it from the form instead of from a 422. */
const MIN_BRIEF = 10;

// Same cadence as the board's add-module poll. The deadline is longer because
// this job does MORE: a planning call over the whole library, and then the
// chained draft of the lesson it just planted, in the same thread.
const POLL_INTERVAL_MS = 2000;
const POLL_DEADLINE_MS = 10 * 60_000;

const SELECT_CLASS =
  "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 text-base outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:bg-input/30";

function sleep(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

interface AddLessonDialogProps {
  moduleId: string;
  moduleTitle: string;
  /** This module's existing lessons, in board order — the options of «Θέση». */
  siblings: { id: string; title: string }[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Refetch the tree. Called for BOTH doors: the blank lesson and the AI one. */
  onAdded: () => void;
}

/** «Προσθήκη μαθήματος» — the dialog that asks WHAT THE LESSON SHOULD TEACH.
 *
 * The menu item used to plant a box called «Νέο μάθημα» and walk away, which
 * left the tutor with the entire lesson to write. It now opens one question in
 * his own words, hands it to the planner (`POST
 * /blocks/{module}/lessons/generate`), and the lesson arrives titled, aimed and
 * drafted from his library.
 *
 * TWO DOORS, ON PURPOSE. «Κενό μάθημα» keeps the old behaviour verbatim —
 * sometimes he wants a container to fill himself, and taking that away to sell
 * the AI path would be a downgrade.
 *
 * WHY A JOB AND NOT A REQUEST: the planning call reads the whole library
 * (20-60s) and the draft chained behind it is minutes more — this dies at the
 * edge as a sync request. So: 202, poll, and a status line that changes under
 * him («Σχεδιάζω…» → «Γράφεται…») so the wait is legible rather than a spinner.
 *
 * WHAT THIS DIALOG DOES NOT REPORT: the chained draft job's outcome. Once the
 * lesson exists the BOARD owns that story — the refetched tree's `queued` row
 * re-arms the draft progress bar, and the row's own status chip is the truth.
 * The draft job's `error` is a whole-run string (it can carry a root-wide
 * count) and would be a lie about this one lesson.
 */
export function AddLessonDialog({
  moduleId,
  moduleTitle,
  siblings,
  open,
  onOpenChange,
  onAdded,
}: AddLessonDialogProps) {
  const t = useTranslations("curricula.tree.addLessonDialog");
  const tTree = useTranslations("curricula.tree");
  const tJobErrors = useTranslations("jobErrors");

  const [brief, setBrief] = useState("");
  const [title, setTitle] = useState("");
  const [after, setAfter] = useState("");
  /** `null` = the form. The two busy phases are what the status line reads. */
  const [phase, setPhase] = useState<"planning" | "drafting" | "empty" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const busy = phase !== null;
  const trimmedBrief = brief.trim();
  const longEnough = trimmedBrief.length >= MIN_BRIEF;
  // Nothing typed yet is not a mistake — the hint appears once he has started
  // and stopped too early, which is the only moment it explains anything.
  const tooShort = trimmedBrief.length > 0 && !longEnough;

  function reset() {
    setBrief("");
    setTitle("");
    setAfter("");
    setPhase(null);
    setError(null);
  }

  function close() {
    reset();
    onOpenChange(false);
  }

  /** The old behaviour, untouched: a blank lesson called «Νέο μάθημα». */
  async function handleEmpty() {
    setPhase("empty");
    setError(null);
    try {
      await addLesson(moduleId, { title: tTree("newLessonTitle") });
      onAdded();
      close();
    } catch {
      // Never the server's `detail` — it is English prose written for us.
      setError(t("error"));
      setPhase(null);
    }
  }

  async function handleGenerate() {
    setPhase("planning");
    setError(null);
    try {
      const accepted = await generateLesson(moduleId, {
        brief: trimmedBrief,
        title: title.trim() || null,
        after: after || null,
      });
      const deadline = performance.now() + POLL_DEADLINE_MS;
      while (performance.now() < deadline) {
        const job = await getJob(accepted.job_id);
        // The lesson EXISTS from this tick on, and the chained draft has it —
        // so the sentence stops being about planning and starts being about
        // the board, whether or not the job row has flipped to `succeeded`.
        if (job.progress?.phase === "drafting") setPhase("drafting");
        if (job.status === "succeeded") {
          onAdded();
          close();
          return;
        }
        if (job.status === "failed") {
          setError(jobErrorText(job, tJobErrors));
          setPhase(null);
          return;
        }
        await sleep(POLL_INTERVAL_MS);
      }
      // NOT a failure: the job is still running on the server and the lesson
      // will land on the board on its own. Only this dialog stopped waiting.
      setError(t("timeout"));
      setPhase(null);
    } catch {
      // A 409 `llm_not_configured` lands here, before any job exists — and it
      // gets the same Greek sentence as every other failure of this dialog.
      // The server's `detail` is English prose written for us, never for him.
      setError(t("error"));
      setPhase(null);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // A generate in flight owns the dialog: closing it mid-job would leave
        // the poll loop writing into an unmounted form while the lesson lands
        // on the board anyway.
        if (busy) return;
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-lg" data-testid="add-lesson-dialog">
        <DialogHeader>
          <DialogTitle>{t("title", { module: moduleTitle })}</DialogTitle>
        </DialogHeader>

        <DialogBody className="flex flex-col gap-3 overflow-y-auto">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="add-lesson-brief">{t("briefLabel")}</Label>
            <Textarea
              id="add-lesson-brief"
              data-testid="add-lesson-brief"
              rows={8}
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              placeholder={t("briefPlaceholder")}
              disabled={busy}
            />
            {tooShort && (
              <p className="text-xs text-muted-foreground" data-testid="add-lesson-too-short">
                {t("tooShort")}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="add-lesson-title">{t("titleLabel")}</Label>
            <Input
              id="add-lesson-title"
              data-testid="add-lesson-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={busy}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="add-lesson-after">{t("afterLabel")}</Label>
            {/* A plain <select>: this app has no Select component, and one
                native control beats a popup that has to be positioned inside
                an already-portalled dialog. */}
            <select
              id="add-lesson-after"
              data-testid="add-lesson-after"
              className={SELECT_CLASS}
              value={after}
              onChange={(e) => setAfter(e.target.value)}
              disabled={busy}
            >
              <option value="">{t("afterEnd")}</option>
              {siblings.map((s) => (
                <option key={s.id} value={s.id}>
                  {t("afterItem", { title: s.title })}
                </option>
              ))}
            </select>
          </div>

          {phase === "planning" || phase === "drafting" ? (
            <p
              // The one line that CHANGES while he waits (planning → writing),
              // so it is announced rather than silently swapped.
              aria-live="polite"
              className="flex items-center gap-2 text-sm text-muted-foreground"
              data-testid="add-lesson-status"
            >
              <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden />
              {t(phase)}
            </p>
          ) : null}

          {error && (
            <p role="alert" data-testid="add-lesson-error" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </DialogBody>

        {/* NOT a DialogFooter's right-aligned pair: these are two different
            actions, not confirm/cancel, and the AI one is the answer to the
            question above it. */}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            data-testid="add-lesson-empty"
            disabled={busy}
            onClick={handleEmpty}
          >
            {phase === "empty" ? <Loader2 className="animate-spin" /> : <Plus />}
            {t("empty")}
          </Button>
          <Button
            type="button"
            data-testid="add-lesson-generate"
            disabled={busy || !longEnough}
            onClick={handleGenerate}
          >
            {phase === "planning" || phase === "drafting" ? (
              <Loader2 className="animate-spin" />
            ) : (
              <Sparkles />
            )}
            {t("generate")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
