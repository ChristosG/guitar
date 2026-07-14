"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Sparkles } from "lucide-react";
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
  getCurriculum,
  getJob,
  isJobAccepted,
  startInterview,
  type BlockNode,
  type InterviewOption,
  type InterviewStateOut,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { InterviewWhoStep } from "./interview-who-step";
import { InterviewDurationStep } from "./interview-duration-step";
import { InterviewSourcesStep } from "./interview-sources-step";
import { InterviewPreviewStep } from "./interview-preview-step";
import { InterviewConfirmStep } from "./interview-confirm-step";

/** Same poll cadence/cap as `generate-dialog.tsx`'s own `POLL_INTERVAL_MS`/
 * `MAX_POLLS` — deliberately not invented afresh (the brief: "reuse the
 * EXISTING job-poll pattern... do NOT invent a second polling mechanism").
 * 150 * 2s ~ 5 minutes, comfortably above the 49-179s/call the API measured
 * for a curriculum generation run. */
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 150;

const STEP_ORDER = ["who", "duration", "sources", "preview", "confirm"] as const;

interface InterviewDialogProps {
  /** Current UI locale — pre-fills nothing here (unlike `GenerateDialog`'s
   * old `language` field, this interview's "who" step derives language from
   * the chosen/new student instead), but IS what a preview citation link
   * targets (`/${locale}/library/...`). */
  locale: string;
  onGenerated: (tree: BlockNode) => void;
}

/** Minimal step progress trail — 5 quiet segments, not a numbered wizard
 * chrome: reassures the tutor there's a short, fixed number of questions
 * left without adding another box to fill in. */
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

/** THE new entry point for curriculum generation (Plan 12 Task 3, G2's
 * frontend): replaces the old one-shot `GenerateDialog` form. Chris,
 * verbatim, on why: "when i click 'Generate a curriculum' seems like it
 * just uses the llm general knowledge... it would be beneficial here to
 * select things from our library... maybe llm can act as an assistant
 * there bro, guiding him, and asking him questions or corrections
 * throughout the process." The state machine itself lives entirely
 * server-side (`app.curriculum.interview`'s own docstring: the model never
 * tracks `step` — Plan 10/11 both found it "unreliable at id-plumbing"),
 * this dialog is just a thin client that renders whatever step the API
 * says it's on and posts back one answer at a time.
 *
 * `title`/`domain` are collected in an "intro" phase BEFORE the first
 * `POST /curricula/interview` call — the interview's own five steps
 * (`who`/`duration`/`sources`/`preview`/`confirm`) don't include a "what's
 * this course called" step (see `start_interview`'s own docstring on the
 * API side: that's asked once, up front, exactly like the old direct
 * `POST /curricula/generate` form already did).
 *
 * On the final "confirm" step, `answerInterview`'s response is a 202
 * `JobAccepted` rather than another `InterviewStateOut` (`isJobAccepted`
 * discriminates it) — this dialog then polls `GET /jobs/{id}` with the
 * EXACT same cadence/cap `GenerateDialog` used, and only then fetches the
 * generated tree and hands it to `onGenerated`, same handoff contract the
 * old dialog had.
 */
export function InterviewDialog({ locale, onGenerated }: InterviewDialogProps) {
  const t = useTranslations("curricula.interview");

  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<"intro" | "interview" | "job">("intro");
  const [title, setTitle] = useState("");
  const [domain, setDomain] = useState("");
  const [interviewId, setInterviewId] = useState<string | null>(null);
  const [state, setState] = useState<InterviewStateOut | null>(null);
  // Title -> option, built from the "sources" step's own options the moment
  // the tutor answers it — the ONLY place `source_id` is ever seen on the
  // wire for a source (the later "preview" findings only carry
  // `source_title` — see `InterviewPassage`'s docstring in `lib/api.ts`).
  // This is what lets a preview citation deep-link into the Reader.
  const [sourceCatalog, setSourceCatalog] = useState<Map<string, InterviewOption>>(new Map());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobError, setJobError] = useState<string | null>(null);

  function reset() {
    setPhase("intro");
    setTitle("");
    setDomain("");
    setInterviewId(null);
    setState(null);
    setSourceCatalog(new Map());
    setSubmitting(false);
    setError(null);
    setJobError(null);
  }

  async function handleStart(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await startInterview({ title, domain: domain || undefined });
      setInterviewId(result.interview_id);
      setState(result);
      setPhase("interview");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("startError"));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleAnswer(answer: unknown) {
    if (!interviewId) return;
    setSubmitting(true);
    setError(null);
    // Capture the sources catalog on the way OUT of the "sources" step —
    // `state` here is still that step's own response (options = every
    // library source with its real id), read before it's replaced below.
    if (state?.step === "sources" && state.options) {
      setSourceCatalog(new Map(state.options.map((o) => [o.label, o])));
    }
    try {
      const result = await answerInterview(interviewId, answer);
      if (isJobAccepted(result)) {
        setPhase("job");
        await pollJob(result.job_id);
        return;
      }
      setState(result);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("answerError"));
    } finally {
      setSubmitting(false);
    }
  }

  async function pollJob(jobId: string) {
    try {
      let job = await getJob(jobId);
      let polls = 1;
      while (job.status !== "succeeded" && job.status !== "failed" && polls < MAX_POLLS) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        job = await getJob(jobId);
        polls++;
      }
      if (job.status === "succeeded") {
        const tree = await getCurriculum(job.result_root_id!);
        onGenerated(tree);
        reset();
        setOpen(false);
      } else if (job.status === "failed") {
        setJobError(job.error ?? t("steps.confirm.error"));
      } else {
        setJobError(t("steps.confirm.stillGenerating"));
      }
    } catch (err) {
      setJobError(err instanceof ApiError ? err.detail : t("steps.confirm.error"));
    }
  }

  const busy = submitting || phase === "job";

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (busy) return; // never let this vanish mid-flight — same posture as GenerateDialog
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button data-testid="curricula-generate-button" />}>
        <Sparkles />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent data-testid="interview-dialog" className="sm:max-w-lg">
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
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="interview-domain">{t("domainLabel")}</Label>
                <Input
                  id="interview-domain"
                  data-testid="interview-domain"
                  value={domain}
                  onChange={(e) => setDomain(e.target.value)}
                  placeholder={t("domainPlaceholder")}
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
          </form>
        )}

        {/* DialogBody, not a bare <div>: `DialogContent` is now height-capped
            and `overflow-hidden`, so the tallest step in the app (the preview
            step's findings list, or a library with 30 sources) has to scroll
            INSIDE the card. Before this it just painted over the backdrop. */}
        {phase === "interview" && state && (
          <DialogBody data-testid="interview-body">
            <StepTrail currentStep={state.step} />

            {state.step === "who" && (
              <InterviewWhoStep
                key={state.step}
                options={state.options ?? []}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "duration" && (
              <InterviewDurationStep
                key={state.step}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "sources" && (
              <InterviewSourcesStep
                key={state.step}
                options={state.options ?? []}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "preview" && state.findings && (
              <InterviewPreviewStep
                key={state.step}
                findings={state.findings}
                sourceCatalog={sourceCatalog}
                locale={locale}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
            {state.step === "confirm" && (
              <InterviewConfirmStep
                key={state.step}
                findings={state.findings}
                submitting={submitting}
                error={state.error}
                onSubmit={handleAnswer}
              />
            )}
          </DialogBody>
        )}

        {phase === "job" && (
          <div className="flex flex-col gap-3">
            {!jobError ? (
              <div
                role="status"
                data-testid="interview-job-loading"
                className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground"
              >
                <Loader2 className="size-4 shrink-0 animate-spin" />
                {t("steps.confirm.generating")}
              </div>
            ) : (
              <>
                <p role="alert" data-testid="interview-job-error" className="text-sm text-destructive">
                  {jobError}
                </p>
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={reset} data-testid="interview-start-over">
                    {t("steps.confirm.startOver")}
                  </Button>
                </DialogFooter>
              </>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
