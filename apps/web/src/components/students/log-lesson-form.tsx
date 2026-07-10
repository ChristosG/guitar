"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, logLesson, type LessonLogOut } from "@/lib/api";

interface LogLessonFormProps {
  studentId: string;
  onLogged: (log: LessonLogOut) => void;
}

/** A compact quick-log form for "what happened in this session". The one
 * field this app has no better picker for yet is the session/block id:
 * there is no `GET /blocks?student_id=` route (see this task's report), so
 * it's a plain text field, not a dropdown — the tutor pastes the block id
 * of the session being logged. Plan 6 Task 5 ("Today/Prep") is where a
 * proper "next session" picker is planned to land (per the plan's own
 * Task 5 description), so this stays intentionally minimal here rather than
 * building a bespoke block-tree picker a few days ahead of that task.
 *
 * `taught` defaults to `true` (a toggle button, same boolean-toggle pattern
 * `curriculum/attach-artifact-dialog.tsx`'s "ground" toggle uses — this app
 * has no checkbox primitive) since the common case for a quick log is
 * confirming a session that actually happened; the tutor flips it off for a
 * no-show/cancellation. */
export function LogLessonForm({ studentId, onLogged }: LogLessonFormProps) {
  const t = useTranslations("students.detail.logLessonForm");

  const [blockId, setBlockId] = useState("");
  const [date, setDate] = useState("");
  const [taught, setTaught] = useState(true);
  const [notes, setNotes] = useState("");
  const [homework, setHomework] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setBlockId("");
    setDate("");
    setTaught(true);
    setNotes("");
    setHomework("");
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const log = await logLesson(studentId, {
        sessionBlockId: blockId,
        date: date || null,
        taught,
        notes: notes || null,
        homework: homework || null,
      });
      onLogged(log);
      reset();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card data-testid="log-lesson-form">
      <CardHeader>
        <CardTitle>{t("heading")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="log-lesson-block-id">{t("blockIdLabel")}</Label>
              <Input
                id="log-lesson-block-id"
                data-testid="log-lesson-block-id"
                value={blockId}
                onChange={(e) => setBlockId(e.target.value)}
                placeholder={t("blockIdPlaceholder")}
                required
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="log-lesson-date">{t("dateLabel")}</Label>
                <Input
                  id="log-lesson-date"
                  data-testid="log-lesson-date"
                  type="date"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                />
              </div>
              <div className="flex flex-col justify-end">
                <Button
                  type="button"
                  variant={taught ? "default" : "outline"}
                  size="sm"
                  aria-pressed={taught}
                  data-testid="log-lesson-taught"
                  className="self-start"
                  onClick={() => setTaught((v) => !v)}
                >
                  {taught ? t("taughtOn") : t("taughtOff")}
                </Button>
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="log-lesson-notes">{t("notesLabel")}</Label>
              <Textarea
                id="log-lesson-notes"
                data-testid="log-lesson-notes"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder={t("notesPlaceholder")}
                rows={2}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="log-lesson-homework">{t("homeworkLabel")}</Label>
              <Textarea
                id="log-lesson-homework"
                data-testid="log-lesson-homework"
                value={homework}
                onChange={(e) => setHomework(e.target.value)}
                placeholder={t("homeworkPlaceholder")}
                rows={2}
              />
            </div>
          </fieldset>

          {error && (
            <p role="alert" data-testid="log-lesson-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <Button type="submit" disabled={submitting} data-testid="log-lesson-submit" className="self-start">
            {submitting && <Loader2 className="animate-spin" />}
            {submitting ? t("submitting") : t("submit")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
