"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Plus } from "lucide-react";
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
import { ApiError, createStudent } from "@/lib/api";

/** "Add student" trigger + dialog form. Self-contained (owns its own field
 * state, submit/error state, and open state), mirroring `components/
 * knowledge/add-source.tsx` — the only difference is the fields live inside
 * a shadcn Dialog instead of an always-visible Card. Calls `onAdded` after
 * a successful `POST /students` so the parent page can refresh its list. */
export function AddStudentDialog({ onAdded }: { onAdded: () => void }) {
  const t = useTranslations("students.addDialog");

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [level, setLevel] = useState("");
  const [instrument, setInstrument] = useState("");
  const [language, setLanguage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setName("");
    setLevel("");
    setInstrument("");
    setLanguage("");
    setError(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await createStudent({
        name,
        level: level || undefined,
        instrument: instrument || undefined,
        preferred_language: language || undefined,
      });
      reset();
      setOpen(false);
      onAdded();
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
        if (submitting) return; // never let the dialog vanish mid-request
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button data-testid="students-add-button" />}>
        <Plus />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent data-testid="add-student-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="add-student-name">{t("name")}</Label>
            <Input
              id="add-student-name"
              data-testid="add-student-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("namePlaceholder")}
              disabled={submitting}
              required
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-student-level">{t("level")}</Label>
              <Input
                id="add-student-level"
                data-testid="add-student-level"
                value={level}
                onChange={(e) => setLevel(e.target.value)}
                placeholder={t("levelPlaceholder")}
                disabled={submitting}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-student-instrument">{t("instrument")}</Label>
              <Input
                id="add-student-instrument"
                data-testid="add-student-instrument"
                value={instrument}
                onChange={(e) => setInstrument(e.target.value)}
                placeholder={t("instrumentPlaceholder")}
                disabled={submitting}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-student-language">{t("language")}</Label>
              <Input
                id="add-student-language"
                data-testid="add-student-language"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                placeholder={t("languagePlaceholder")}
                disabled={submitting}
              />
            </div>
          </div>

          {error && (
            <p role="alert" data-testid="add-student-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting} data-testid="add-student-submit">
              {submitting ? t("submitting") : t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
