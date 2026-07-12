"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { FolderPlus, Loader2 } from "lucide-react";
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
import { ApiError, createCollection } from "@/lib/api";

/** The "New collection" trigger + dialog — a single field, on purpose (design
 * brief: "few controls, nothing to fill in"). A collection is just a name;
 * everything else about it (which sources live in it) is set from the
 * source rows themselves via their "file under" picker, never from here. */
export function NewCollectionDialog({ onCreated }: { onCreated: () => void }) {
  const t = useTranslations("library.collectionDialog");

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await createCollection(name);
      setName("");
      setOpen(false);
      onCreated();
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
        if (submitting) return;
        setOpen(next);
        if (!next) {
          setName("");
          setError(null);
        }
      }}
    >
      <DialogTrigger render={<Button variant="outline" data-testid="new-collection-trigger" />}>
        <FolderPlus />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent data-testid="new-collection-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-1.5">
            <Label htmlFor="new-collection-name">{t("nameLabel")}</Label>
            <Input
              id="new-collection-name"
              data-testid="new-collection-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("namePlaceholder")}
              required
            />
          </fieldset>

          {error && (
            <p role="alert" data-testid="new-collection-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting} data-testid="new-collection-submit">
              {submitting && <Loader2 className="animate-spin" />}
              {submitting ? t("submitting") : t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
