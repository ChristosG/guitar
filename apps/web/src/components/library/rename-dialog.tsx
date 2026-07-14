"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api";

interface RenameDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The current name. Seeded into the field as the form's INITIAL state — the
   * form is only mounted while the dialog is open (see below), so every open
   * starts from the name the row is actually showing right now. */
  value: string;
  heading: string;
  description: string;
  onSubmit: (name: string) => Promise<unknown>;
  onRenamed: () => void;
}

/** One dialog, both rename affordances (a source and a folder) — the API has
 * supported `PATCH title`/`PATCH name` since Plan 9 and NOTHING in the UI ever
 * called either, so a book filed under a typo was a typo forever.
 *
 * Controlled by the parent (`open`/`onOpenChange`) rather than owning a
 * `DialogTrigger`, because both callers open it from an icon button that already
 * sits in a row full of other controls.
 *
 * The form body is a separate component mounted ONLY while open, which is what
 * seeds the field from `value` without a `useEffect` that setStates on open — a
 * cascading-render pattern the lint rules reject, and a stale-value bug waiting
 * to happen (the row's title changes under us on every refresh). */
export function RenameDialog(props: RenameDialogProps) {
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent data-testid="rename-dialog">
        {props.open && <RenameForm {...props} />}
      </DialogContent>
    </Dialog>
  );
}

function RenameForm({ value, heading, description, onSubmit, onOpenChange, onRenamed }: RenameDialogProps) {
  const t = useTranslations("library.renameDialog");
  const [name, setName] = useState(value);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const next = name.trim();
    // A no-op rename is not an error and not a request — just close.
    if (!next || next === value) {
      onOpenChange(false);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(next);
      onOpenChange(false);
      onRenamed();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>{heading}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <fieldset disabled={submitting} className="flex flex-col gap-1.5">
          <Label htmlFor="rename-input">{t("label")}</Label>
          <Input
            id="rename-input"
            data-testid="rename-input"
            autoFocus
            required
            maxLength={400}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </fieldset>

        {error && (
          <p role="alert" data-testid="rename-error" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <DialogFooter>
          <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
            {t("cancel")}
          </DialogClose>
          <Button type="submit" disabled={submitting} data-testid="rename-submit">
            {submitting && <Loader2 className="animate-spin" />}
            {submitting ? t("submitting") : t("submit")}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
