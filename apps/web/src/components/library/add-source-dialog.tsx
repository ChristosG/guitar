"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Plus } from "lucide-react";
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
import { Textarea } from "@/components/ui/textarea";
import { ApiError, createSource, startOcr, uploadSource } from "@/lib/api";

type Mode = "text" | "url" | "pdf";

const MODES: Mode[] = ["text", "url", "pdf"];

interface AddSourceDialogProps {
  onCreated: () => void;
}

/** The "Add source" trigger + dialog — deliberately just Title + the one
 * field the chosen mode needs (text/url/file). The old Knowledge page's form
 * also had always-visible domain/language fields; dropped here on purpose
 * (design brief: "few controls, nothing to fill in" — both are optional on
 * the API and unused by anything this page renders). A brand-new source
 * always lands Unfiled; filing it into a collection happens afterward via
 * the row's own "file under" picker, not a second field in this dialog. */
export function AddSourceDialog({ onCreated }: AddSourceDialogProps) {
  const t = useTranslations("library.addDialog");

  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("pdf");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setTitle("");
    setText("");
    setUrl("");
    setFile(null);
    setError(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "pdf") {
        if (!file) return; // the `required` file input already guards normal submission
        const created = await uploadSource({ title, file });
        // Kick OCR right after the upload succeeds (a scanned PDF's sync
        // ingest above extracted only whatever native text layer existed —
        // usually little or none — so this is what actually reads the
        // book). Best-effort: the source already exists either way, so a
        // failure here doesn't block adding it — the tutor can Retry it
        // from the row later.
        //
        // Nothing is handed back to the parent any more: the job is a SERVER
        // fact from here on (`SourceOut.ocr_active` + `GET .../progress`), so
        // the `onCreated()` refresh below is all it takes for the new row to
        // start narrating "reading page N of M" — in this tab, in a second tab,
        // and after a reload. It used to hand the parent a job id to watch in
        // React state, which is exactly why an F5 lost the thread.
        try {
          await startOcr(created.id);
        } catch {
          // swallowed — see docstring above
        }
      } else {
        await createSource({
          kind: mode,
          title,
          text: mode === "text" ? text : undefined,
          url: mode === "url" ? url : undefined,
        });
      }
      reset();
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
        if (submitting) return; // never let this vanish mid-upload
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger render={<Button data-testid="add-source-trigger" />}>
        <Plus />
        {t("trigger")}
      </DialogTrigger>
      <DialogContent data-testid="add-source-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex gap-2" role="radiogroup" aria-label={t("heading")}>
              {MODES.map((m) => (
                <Button
                  key={m}
                  type="button"
                  size="sm"
                  variant={mode === m ? "default" : "outline"}
                  aria-pressed={mode === m}
                  data-testid={`add-source-mode-${m}`}
                  onClick={() => setMode(m)}
                >
                  {t(m === "text" ? "modeText" : m === "url" ? "modeUrl" : "modePdf")}
                </Button>
              ))}
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-source-title">{t("titleLabel")}</Label>
              <Input
                id="add-source-title"
                data-testid="add-source-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={t("titlePlaceholder")}
                required
              />
            </div>

            {mode === "text" && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="add-source-text">{t("textLabel")}</Label>
                <Textarea
                  id="add-source-text"
                  data-testid="add-source-text"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder={t("textPlaceholder")}
                  rows={5}
                  required
                />
              </div>
            )}

            {mode === "url" && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="add-source-url">{t("urlLabel")}</Label>
                <Input
                  id="add-source-url"
                  data-testid="add-source-url"
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder={t("urlPlaceholder")}
                  required
                />
              </div>
            )}

            {mode === "pdf" && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="add-source-file">{t("fileLabel")}</Label>
                <Input
                  id="add-source-file"
                  data-testid="add-source-file"
                  type="file"
                  accept="application/pdf"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  required
                />
              </div>
            )}
          </fieldset>

          {error && (
            <p role="alert" data-testid="add-source-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting} data-testid="add-source-submit">
              {submitting && <Loader2 className="animate-spin" />}
              {submitting ? t("submitting") : t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
