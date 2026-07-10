"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Link2, Loader2 } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import { ApiError, assignCurriculum, listCurricula, type CurriculumListItem } from "@/lib/api";

interface AssignCurriculumDialogProps {
  studentId: string;
  /** Called after a successful assign — the student-detail page re-fetches
   * `getStudentDetail` (the assign response is the CLONE's tree, not an
   * `AssignmentSummary`, so there's nothing to splice in locally; mirrors
   * `curricula/page.tsx`'s own `refreshTemplates()` "cheap refetch follow-up"
   * precedent for the same reason). */
  onAssigned: () => void;
}

/** The "Assign curriculum" trigger + dialog, reachable from the student
 * detail page: pick a template from `GET /curricula` -> `POST /curricula/
 * {root_id}/assign {student_id}`. The picker fetches lazily on open (not on
 * page mount — this student-detail page has no other use for the curricula
 * list) and uses a native `<select>`, same "open-ended list -> native select,
 * not a button row" call `notes/student-select.tsx` already made for the
 * (similarly open-ended) student picker.
 *
 * The fetch is triggered directly from `onOpenChange` below, NOT a
 * `useEffect` keyed on `open` — this needs a fresh "loading=true, error=null"
 * reset on every reopen (not just once on mount), and `onOpenChange` is a
 * real event-handler callback (Base UI calls it in response to the trigger
 * click), so setState there is unconstrained; the equivalent reset-then-
 * fetch inside a `useEffect` body would call `setState` synchronously as
 * part of the effect's own execution, which is exactly what react-hooks/
 * set-state-in-effect flags (`curricula/page.tsx`'s own `refreshTemplates`
 * sidesteps the identical issue the identical way — a plain callback, not an
 * effect). */
export function AssignCurriculumDialog({ studentId, onAssigned }: AssignCurriculumDialogProps) {
  const t = useTranslations("students.detail.assignDialog");

  const [open, setOpen] = useState(false);
  const [curricula, setCurricula] = useState<CurriculumListItem[]>([]);
  const [curriculaLoading, setCurriculaLoading] = useState(false);
  const [curriculaError, setCurriculaError] = useState<string | null>(null);
  const [rootId, setRootId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function loadCurricula() {
    setCurriculaLoading(true);
    setCurriculaError(null);
    listCurricula()
      .then((data) => {
        setCurricula(data);
        setRootId(data[0]?.id ?? "");
      })
      .catch((err) => setCurriculaError(err instanceof ApiError ? err.detail : t("loadError")))
      .finally(() => setCurriculaLoading(false));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!rootId) return;
    setSubmitting(true);
    setError(null);
    try {
      await assignCurriculum(rootId, studentId);
      setOpen(false);
      onAssigned();
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
        if (submitting) return; // never let this vanish mid-request
        setOpen(next);
        if (next) {
          loadCurricula();
        } else {
          setError(null);
        }
      }}
    >
      <DialogTrigger
        render={<Button type="button" variant="outline" size="sm" data-testid="assign-curriculum-button" />}
      >
        <Link2 />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent data-testid="assign-curriculum-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="assign-curriculum-select">{t("pickLabel")}</Label>
              {curriculaLoading && (
                <p className="text-sm text-muted-foreground" data-testid="assign-curriculum-loading">
                  {t("loading")}
                </p>
              )}
              {curriculaError && (
                <p role="alert" data-testid="assign-curriculum-load-error" className="text-sm text-destructive">
                  {curriculaError}
                </p>
              )}
              {!curriculaLoading && !curriculaError && curricula.length === 0 && (
                <p className="text-sm text-muted-foreground" data-testid="assign-curriculum-empty">
                  {t("empty")}
                </p>
              )}
              {curricula.length > 0 && (
                <select
                  id="assign-curriculum-select"
                  data-testid="assign-curriculum-select"
                  value={rootId}
                  onChange={(e) => setRootId(e.target.value)}
                  className="h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 text-base outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:bg-input/30"
                >
                  {curricula.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.title}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </fieldset>

          {error && (
            <p role="alert" data-testid="assign-curriculum-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting || !rootId} data-testid="assign-curriculum-submit">
              {submitting && <Loader2 className="animate-spin" />}
              {submitting ? t("assigning") : t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
