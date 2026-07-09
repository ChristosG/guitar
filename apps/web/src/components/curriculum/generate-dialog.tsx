"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
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
import { ApiError, getCurriculum, getJob, startCurriculumGeneration, type BlockNode } from "@/lib/api";

interface GenerateDialogProps {
  /** Current UI locale, used only to pre-fill the "language" field — the
   * generated course's language is independently editable, since a tutor
   * may want content in a language other than the cockpit's own UI. */
  locale: string;
  onGenerated: (tree: BlockNode) => void;
}

/** Poll cadence + cap while a curriculum generation job is in flight (Plan 8
 * Task 4). 150 * 2s ≈ 5 minutes — comfortably above the 49-179s/call the API
 * measured for the underlying generation. Exceeding the cap doesn't cancel
 * the job (it keeps running server-side — see
 * `app.jobs.runner.run_curriculum_job`); it just stops this dialog's own
 * wait and tells the tutor to check back later (see `stillGenerating`
 * below). */
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 150;

/** The "Generate curriculum" trigger + dialog. `POST /curricula/generate`
 * now enqueues a background job and returns a 202 almost immediately (Plan 8
 * Task 3) — the underlying generation is still the same SLOW guided-JSON
 * LLM call measured at 49-179s/call (see `lib/api.ts`'s docstring on
 * `startCurriculumGeneration`), it just runs off the request path now. This
 * dialog polls `GET /jobs/{job_id}` every ~2s until the job reaches a
 * terminal status, then fetches the tree via `getCurriculum` — so, same as
 * before this task, it's still the one dialog in the app that deliberately:
 *  - blocks its own dismissal (`onOpenChange` ignores close attempts) while
 *    a submission is in flight (now: enqueue + the whole poll loop), so it's
 *    never orphaned by a stray Escape/backdrop click,
 *  - disables every field (a `<fieldset disabled>`, which natively cascades
 *    to every input/button inside it) instead of just the submit button,
 *  - shows a persistent `role="status"` banner with the exact
 *    "this can take a minute" copy, not just a spinner, so a 1-2 minute
 *    wait never reads as a frozen tab.
 */
export function GenerateDialog({ locale, onGenerated }: GenerateDialogProps) {
  const t = useTranslations("curricula.generateDialog");

  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [domain, setDomain] = useState("");
  const [level, setLevel] = useState("");
  const [language, setLanguage] = useState(locale);
  const [targetHours, setTargetHours] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setTitle("");
    setDomain("");
    setLevel("");
    setLanguage(locale);
    setTargetHours("");
    setError(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const hours = Number(targetHours);
      const { job_id } = await startCurriculumGeneration({
        title,
        language,
        profile: level ? { level } : {},
        domain: domain || undefined,
        target_minutes_total: targetHours && hours > 0 ? Math.round(hours * 60) : undefined,
      });

      // Poll until the job reaches a terminal status or we hit the cap —
      // see the `POLL_INTERVAL_MS`/`MAX_POLLS` docstring above.
      let job = await getJob(job_id);
      let polls = 1;
      while (job.status !== "succeeded" && job.status !== "failed" && polls < MAX_POLLS) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        job = await getJob(job_id);
        polls++;
      }

      if (job.status === "succeeded") {
        const tree = await getCurriculum(job.result_root_id!);
        onGenerated(tree);
        reset();
        setOpen(false);
      } else if (job.status === "failed") {
        setError(job.error ?? t("error"));
      } else {
        // Cap exceeded — the job keeps running server-side; give up waiting
        // and tell the tutor to check back rather than polling forever.
        setError(t("stillGenerating"));
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (submitting) return; // never let this vanish mid-generation
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button data-testid="curricula-generate-button" />}>
        <Sparkles />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent data-testid="generate-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="generate-title">{t("title")}</Label>
              <Input
                id="generate-title"
                data-testid="generate-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={t("titlePlaceholder")}
                required
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="generate-domain">{t("domain")}</Label>
                <Input
                  id="generate-domain"
                  data-testid="generate-domain"
                  value={domain}
                  onChange={(e) => setDomain(e.target.value)}
                  placeholder={t("domainPlaceholder")}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="generate-level">{t("level")}</Label>
                <Input
                  id="generate-level"
                  data-testid="generate-level"
                  value={level}
                  onChange={(e) => setLevel(e.target.value)}
                  placeholder={t("levelPlaceholder")}
                />
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="generate-language">{t("language")}</Label>
                <Input
                  id="generate-language"
                  data-testid="generate-language"
                  value={language}
                  onChange={(e) => setLanguage(e.target.value)}
                  required
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="generate-hours">{t("targetHours")}</Label>
                <Input
                  id="generate-hours"
                  data-testid="generate-hours"
                  type="number"
                  min={0}
                  step="0.5"
                  value={targetHours}
                  onChange={(e) => setTargetHours(e.target.value)}
                  placeholder={t("targetHoursPlaceholder")}
                />
              </div>
            </div>
          </fieldset>

          {submitting && (
            <div
              role="status"
              data-testid="generate-loading"
              className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground"
            >
              <Loader2 className="size-4 shrink-0 animate-spin" />
              {t("generating")}
            </div>
          )}

          {error && (
            <p role="alert" data-testid="generate-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting} data-testid="generate-submit">
              {submitting && <Loader2 className="animate-spin" />}
              {t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
