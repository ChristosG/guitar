"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { ArrowLeft, History, Loader2, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ApiError,
  answerInterview,
  backInterview,
  getInterview,
  getJob,
  isJobAccepted,
  startInterview,
  type InterviewStateOut,
  type JobOut,
} from "@/lib/api";
import { jobErrorText } from "@/lib/job-errors";
import { cn } from "@/lib/utils";
import { InterviewWhoStep } from "./interview-who-step";
import { InterviewDurationStep } from "./interview-duration-step";
import { InterviewScopeStep } from "./interview-scope-step";
import { InterviewStructureStep } from "./interview-structure-step";
import { InterviewSourcesStep } from "./interview-sources-step";
import { InterviewOutlineStep } from "./interview-outline-step";
import { InterviewConfirmStep } from "./interview-confirm-step";
import { PlanningChat } from "./planning-chat";

/** `app.curriculum.interview.STEP_ORDER`, for the progress trail only — the state
 * machine itself is entirely server-side and this client never decides what comes
 * next. It renders whatever step the API says it is on. */
const STEP_ORDER = ["who", "duration", "scope", "structure", "sources", "outline", "confirm"] as const;

interface InterviewDialogProps {
  /** Called the moment `confirm` returns its 202: the tree ALREADY EXISTS (every
   * lesson `queued`), so the board opens on it instantly and watches the lessons
   * arrive. This is NOT a "generation finished" callback — nothing has been
   * written yet, and that is the entire point. */
  onMaterialized: (rootId: string) => void;
  /** A crash-orphaned interview the page found via `getOpenInterview()`. The
   * server state machine was always refresh-safe — a dead browser only lost
   * the pointer (2026-07-23: a desktop OOM kill closed the wizard mid-flight,
   * with a paid outline sitting finished on the server). When set, a resume
   * chip renders beside the trigger; clicking it re-enters the interview at
   * whatever step the server says it is on. */
  resume?: { interview_id: string; title: string } | null;
  /** The X on the resume chip — the page owns the dismissal (localStorage). */
  onResumeDismissed?: () => void;
}

/** Seven quiet segments, not a numbered wizard chrome. */
function StepTrail({ currentStep }: { currentStep: string }) {
  const currentIndex = STEP_ORDER.indexOf(currentStep as (typeof STEP_ORDER)[number]);
  return (
    <div className="flex items-center gap-1.5" data-testid="interview-step-trail">
      {STEP_ORDER.map((step, i) => (
        <div
          key={step}
          data-testid={`interview-step-dot-${step}`}
          aria-current={step === currentStep ? "step" : undefined}
          className={cn(
            "h-1.5 flex-1 rounded-full transition-colors",
            i <= currentIndex ? "bg-primary" : "bg-muted",
          )}
        />
      ))}
    </div>
  );
}

/** Poll a `GenerationJob` to a terminal state, ~2s cadence (matching the board's
 * progress poll). The outline call reads the WHOLE library and can run ~3 minutes —
 * well past Cloudflare's ~100s edge cap, which is exactly why it is a background job
 * now instead of a blocking request. The deadline is a backstop so a wedged job
 * cannot spin the dialog forever; hitting it throws, and `handleAnswer`'s catch
 * shows the generic answer error. */
async function pollOutlineJob(jobId: string): Promise<JobOut> {
  // 25 min: past the server's own outline allowance (the provider's
  // role="plan" 1200s timeout + queueing) — the SERVER is the one that
  // decides a job failed; this deadline exists only for a job that wedges
  // without ever reaching a terminal status. At the old 6 min the client
  // gave up on outlines the backend went on to finish.
  const DEADLINE_MS = Date.now() + 25 * 60 * 1000;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const job = await getJob(jobId);
    if (job.status === "succeeded" || job.status === "failed") return job;
    if (Date.now() > DEADLINE_MS) throw new ApiError(504, "");
  }
}

/** The guided curriculum-authoring interview, v2 — who -> duration -> scope ->
 * sources -> OUTLINE -> confirm.
 *
 * Two things changed here, and both are about the tutor's money.
 *
 * THE OUTLINE STEP REPLACED THE PREVIEW STEP. "Preview" showed him what a cosine
 * floor thought his library covered (separation margin on his real corpus: 0.021)
 * and offered him a Proceed button. The outline step shows him a course written by
 * a model that has READ HIS ENTIRE LIBRARY, and lets him rename, add, delete,
 * reorder and re-tier every part of it before a word is drafted. Chris: "the man
 * might want to change something... we have NOTHING of those bro."
 *
 * CONFIRM NO LONGER POLLS A JOB TO COMPLETION. It used to sit on a spinner for the
 * several minutes a whole curriculum takes, and closing the tab lost the thread.
 * Now confirm MATERIALIZES the tree (course -> modules -> lessons, every lesson
 * `queued`) and returns `{job_id, root_id}` — the dialog closes, the board opens on
 * a real curriculum immediately, and the lessons fill in underneath him. There is
 * nothing left to wait for in a dialog.
 */
export function InterviewDialog({ onMaterialized, resume, onResumeDismissed }: InterviewDialogProps) {
  const t = useTranslations("curricula.interview");
  const tJobErrors = useTranslations("jobErrors");

  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<"intro" | "planning" | "interview">("intro");
  const [title, setTitle] = useState("");
  const [interviewId, setInterviewId] = useState<string | null>(null);
  const [state, setState] = useState<InterviewStateOut | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped every time an outline JOB completes. It exists to remount the outline
  // editor: the old key was `modules.length + title`, and BOTH are stable across
  // a regenerate (the count is shape-enforced, the title is what the tutor
  // typed) — so "Rewrite it" ran a paid full-library job and then showed him the
  // outline he had just rejected.
  const [outlineEpoch, setOutlineEpoch] = useState(0);

  function reset() {
    setPhase("intro");
    setTitle("");
    setInterviewId(null);
    setState(null);
    setSubmitting(false);
    setError(null);
    setOutlineEpoch(0);
  }

  // `target` is where a SUCCESSFUL start lands: the primary Start button
  // leaves it at its default ("interview" — today's flow, unchanged), while
  // the secondary «Θέλεις να το συζητήσουμε πρώτα;» button (Part 5) passes
  // "planning" so the SAME `startInterview` call feeds the planning chat
  // instead. `e` is optional because that second button is a plain
  // `type="button"` click, not a form submit.
  async function handleStart(e?: FormEvent, target: "interview" | "planning" = "interview") {
    e?.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await startInterview({ title });
      setInterviewId(result.interview_id);
      setState(result);
      setPhase(target);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("startError"));
    } finally {
      setSubmitting(false);
    }
  }

  /** Re-enter a crash-orphaned interview at whatever step the server says.
   * If the crash happened while an OUTLINE JOB was running, `state.job_id`
   * re-attaches to it: poll to terminal, then re-fetch — the paid outline is
   * recovered, never re-bought. */
  async function handleResume() {
    if (!resume) return;
    setOpen(true);
    setPhase("interview");
    setInterviewId(resume.interview_id);
    setTitle(resume.title);
    setSubmitting(true);
    setError(null);
    try {
      let s = await getInterview(resume.interview_id);
      if (s.step === "outline" && !s.findings?.modules?.length && s.job_id) {
        const job = await getJob(s.job_id);
        if (job.status === "pending" || job.status === "running") {
          const done = await pollOutlineJob(s.job_id);
          s = await getInterview(resume.interview_id);
          if (done.status === "failed") setError(jobErrorText(done, tJobErrors));
        }
      }
      setState(s);
      setOutlineEpoch((n) => n + 1);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("answerError"));
    } finally {
      setSubmitting(false);
    }
  }

  /** One step back — server-side move, answers kept; the revisited step
   * renders from `state.prior`. Changing duration/scope/sources back there
   * invalidates the cached outline server-side (`answer_interview`), so the
   * outline step regenerates only when its inputs actually changed. */
  async function handleBack() {
    if (!interviewId || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const s = await backInterview(interviewId);
      setState(s);
      setOutlineEpoch((n) => n + 1);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("answerError"));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleAnswer(answer: unknown) {
    if (!interviewId) return;
    setSubmitting(true);
    setError(null);
    try {
      let result = await answerInterview(interviewId, answer);

      // THE OUTLINE CALL IS FIRED HERE, AND ONLY HERE. The API advances to the
      // "outline" step with nothing to show — deliberately: generating the outline
      // is the expensive call (the whole library, ~3 min, ~$1.30) and it belongs to
      // the step that DISPLAYS its result, so a tutor who backs out of the sources
      // step and re-picks does not pay for an outline he never saw
      // (`_answer_outline`'s own docstring). Landing on an empty editor and making
      // him press "Rewrite it" to get his first outline would be an ambush; this
      // asks for it the moment he arrives.
      if (!isJobAccepted(result) && result.step === "outline" && !result.findings?.modules?.length) {
        result = await answerInterview(interviewId, { regenerate: true });
      }

      if (isJobAccepted(result)) {
        if (result.root_id) {
          // CONFIRM's 202: the tree exists NOW (every lesson `queued`). Hand the
          // board its root and get out of the way.
          onMaterialized(result.root_id);
          reset();
          setOpen(false);
          return;
        }
        // THE OUTLINE JOB's 202 (no root_id). The full-library call runs OFF the
        // request now — past the ~100s Cloudflare edge cap that used to 524 it and
        // surface "could not save the answer" while the model worked on for nobody.
        // Poll it, then re-fetch the interview so the (now cached) outline is what
        // the editor renders. On failure, still land him on the outline step — an
        // empty editor keeps its Regenerate button — and show the job's own reason
        // (e.g. "open Settings and paste your key"). `submitting` stays true across
        // the poll, so the "working" indicator shows and the dialog can't be closed
        // out from under it.
        const job = await pollOutlineJob(result.job_id);
        setState(await getInterview(interviewId));
        setOutlineEpoch((n) => n + 1);
        if (job.status === "failed") setError(jobErrorText(job, tJobErrors));
        return;
      }
      setState(result);
    } catch (err) {
      // `err.detail` can legitimately be EMPTY (the poll deadline's synthetic
      // 504) — and `{error && ...}` treats "" as nothing to render, which used
      // to make a six-minute timeout end in a silent spinner-stop. Fall back
      // to the generic message whenever there is no real detail.
      const detail = err instanceof ApiError ? err.detail : null;
      setError(detail || t("answerError"));
      // Best-effort re-sync: the server may have advanced the interview (e.g.
      // to the outline step) even though this round-trip died — leaving the
      // dialog on the old step makes the tutor re-answer a question the
      // server considers done, and its re-ask is not even localized.
      if (interviewId) {
        try {
          setState(await getInterview(interviewId));
          setOutlineEpoch((n) => n + 1);
        } catch {
          // The re-sync is a bonus, not a requirement — the error above stands.
        }
      }
    } finally {
      setSubmitting(false);
    }
  }

  const findings = state?.findings ?? null;

  return (
    <>
    {resume && (
      <span className="inline-flex items-center gap-0.5" data-testid="interview-resume-chip">
        <Button type="button" variant="outline" data-testid="interview-resume" onClick={handleResume}>
          <History />
          <span className="max-w-48 truncate">{t("resume", { title: resume.title })}</span>
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={t("resumeDismiss")}
          data-testid="interview-resume-dismiss"
          onClick={onResumeDismissed}
          className="text-muted-foreground hover:text-foreground"
        >
          <X />
        </Button>
      </span>
    )}
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Never let it vanish mid-flight. The outline call reads 90K tokens, takes
        // 30-60 seconds and costs real money; a dialog that closes under a stray
        // click would throw away an outline he has already paid for.
        if (submitting) return;
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button data-testid="curricula-generate-button" />}>
        <Sparkles />
        {t("trigger")}
      </DialogTrigger>
      {/* The outline editor is the tallest thing in this app. `sm:max-w-2xl` gives
          it a column wide enough to actually edit in; `DialogContent` is
          height-capped and `overflow-hidden`, so it scrolls INSIDE the card rather
          than painting over the backdrop.
          The planning phase is the one place that needs more than a cap: its
          chat wrapper (`planning-chat.tsx`) has to scroll INTERNALLY so the
          Skip/Use-this-plan buttons stay pinned below it, and a flex item can't
          `flex-1`/`h-full` against an ancestor whose height is only an intrinsic
          `max-h` — there is no definite size to fill. `h-[85dvh]` here (on TOP of
          the shared `max-h-[85dvh]`, harmlessly redundant) makes the dialog's
          height DEFINITE for that one phase, which is what lets the whole
          `DialogBody` -> `PlanningChat` root -> chat wrapper chain of
          `flex-1`/`min-h-0` actually resolve. Every other phase stays
          content-sized, exactly as before. */}
      <DialogContent
        data-testid="interview-dialog"
        className={cn("sm:max-w-2xl", phase === "planning" && "h-[85dvh]")}
      >
        <DialogHeader>
          <DialogTitle>{t("dialogHeading")}</DialogTitle>
          {phase === "intro" && <DialogDescription>{t("introDescription")}</DialogDescription>}
        </DialogHeader>

        {phase === "intro" && (
          <form onSubmit={handleStart} className="flex flex-col gap-3">
            <fieldset disabled={submitting} className="flex flex-col gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="interview-title">{t("titleLabel")}</Label>
                <Input
                  id="interview-title"
                  data-testid="interview-title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder={t("titlePlaceholder")}
                  required
                />
              </div>
            </fieldset>

            {error && (
              <p role="alert" data-testid="interview-start-error" className="text-sm text-destructive">
                {error}
              </p>
            )}

            <DialogFooter>
              <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
                {t("cancel")}
              </DialogClose>
              <Button type="submit" disabled={submitting} data-testid="interview-start-submit">
                {submitting && <Loader2 className="animate-spin" />}
                {t("startSubmit")}
              </Button>
            </DialogFooter>

            {/* Part 5 — an optional detour, not a fork in the road: same
                `startInterview` call as the primary button above, just landing
                on the planning chat instead of the first server step. Skipping
                this (the primary button) is unchanged — today's flow. */}
            <Button
              type="button"
              variant="outline"
              className="w-full"
              disabled={!title.trim() || submitting}
              data-testid="interview-plan-first"
              onClick={() => handleStart(undefined, "planning")}
            >
              {t("planFirst")}
            </Button>
          </form>
        )}

        {phase === "planning" && interviewId && (
          <DialogBody data-testid="interview-body">
            <PlanningChat interviewId={interviewId} onDone={() => setPhase("interview")} />
          </DialogBody>
        )}

        {phase === "interview" && state && (
          <DialogBody data-testid="interview-body">
            <div className="flex items-center gap-2">
              {STEP_ORDER.indexOf(state.step as (typeof STEP_ORDER)[number]) > 0 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t("back")}
                  data-testid="interview-back"
                  disabled={submitting}
                  onClick={handleBack}
                  className="shrink-0 text-muted-foreground hover:text-foreground"
                >
                  <ArrowLeft />
                </Button>
              )}
              <div className="flex-1">
                <StepTrail currentStep={state.step} />
              </div>
            </div>

            {/* The outline call reads the WHOLE library and takes 30-60 seconds. A
                silently disabled button for a minute reads as a broken app, so it
                says what it is doing. */}
            {submitting && (state.step === "sources" || state.step === "outline") && (
              <p
                role="status"
                data-testid="interview-outline-working"
                className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-xs text-muted-foreground"
              >
                <Loader2 className="size-4 shrink-0 animate-spin" />
                {t("steps.outline.working")}
              </p>
            )}

            {state.step === "who" && (
              <InterviewWhoStep
                key={state.step}
                prior={state.prior}
                options={state.options ?? []}
                levels={findings?.levels ?? []}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "duration" && (
              <InterviewDurationStep
                key={state.step}
                prior={state.prior}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "scope" && (
              <InterviewScopeStep
                key={state.step}
                prior={state.prior}
                options={state.options ?? []}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "structure" && (
              <InterviewStructureStep
                key={state.step}
                prior={state.prior}
                blueprint={findings?.blueprint}
                minutesPerLesson={findings?.minutes_per_lesson}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "sources" && (
              <InterviewSourcesStep
                key={state.step}
                prior={state.prior}
                options={state.options ?? []}
                shape={findings?.shape}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "outline" && (
              <InterviewOutlineStep
                // Keyed on the JOB EPOCH so REGENERATE remounts the editor on the
                // new outline. It was keyed on `modules.length + title` — both
                // stable across a regenerate (the count is shape-enforced, the
                // title is the tutor's own) — so the editor kept its `useState`
                // draft and the paid fresh outline was silently discarded.
                key={`outline-${outlineEpoch}`}
                // `modules: []` is NOT unreachable, and rendering nothing for it was
                // a dead end: if the outline call fails (a bad key, a 429, a
                // truncated response) the API keeps the tutor on this step with
                // `findings` empty. An empty editor still has a Regenerate button
                // and an Add-module button; a blank dialog has neither.
                outline={{ title: findings?.title ?? "", modules: findings?.modules ?? [] }}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "confirm" && (
              <InterviewConfirmStep
                key={state.step}
                findings={findings}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}

            {error && (
              <p role="alert" data-testid="interview-answer-error" className="text-sm text-destructive">
                {error}
              </p>
            )}
          </DialogBody>
        )}
      </DialogContent>
    </Dialog>
    </>
  );
}
