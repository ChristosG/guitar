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
import { ApiError, createSource, uploadSource } from "@/lib/api";

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
  // 1 = the classic single-page fetch; >1 = scoped same-site crawl for
  // guides that span linked pages (server-capped at 50 in brain/crawl.py).
  const [crawlPages, setCrawlPages] = useState(1);
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setTitle("");
    setText("");
    setUrl("");
    setCrawlPages(1);
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
        await uploadSource({ title, file });
        // OCR IS DELIBERATELY *NOT* STARTED HERE ANY MORE, and removing this
        // line is the "explicit" half of "explicit, resumable" (Task 9).
        //
        // This used to `await startOcr(created.id)` the moment an upload
        // landed, and for a year that was free and right: OCR was a local Qwen
        // box, so reading a book cost electricity and a few minutes. It is now
        // Claude, through `claude -p`, on the tutor's SUBSCRIPTION — ~40s a page
        // against a 5-hour cap shared with the curriculum draft that is the
        // actual product. Dropping his four books in would have silently started
        // 888 pages = 8-12 hours of model time, with no dialog, no estimate and
        // no way to know it had happened. Wasted or duplicated LLM spend is this
        // plan's top severity class.
        //
        // So the page scans get rendered and the pages get routed (that is what
        // the upload above does, and it is free and fast), and READING the book
        // is a button on the row — one that says what it costs and resumes where
        // it stopped: `SourceRow.requestReocr` -> `POST .../reocr`.
        //
        // A digital PDF is already fully readable at this point: `paginate.py`
        // keeps a real embedded font's text layer for free and marks those pages
        // `ready`. It is only scans that wait for the button, which is exactly
        // the set that costs money.
      } else {
        await createSource({
          kind: mode,
          title,
          text: mode === "text" ? text : undefined,
          url: mode === "url" ? url : undefined,
          crawl_pages: mode === "url" && crawlPages > 1 ? crawlPages : undefined,
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
                <Label htmlFor="add-source-crawl" className="mt-1.5">
                  {t("crawlLabel")}
                </Label>
                <Input
                  id="add-source-crawl"
                  data-testid="add-source-crawl"
                  type="number"
                  min={1}
                  max={50}
                  value={crawlPages}
                  onChange={(e) => setCrawlPages(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
                />
                <p className="text-xs text-muted-foreground">{t("crawlHint")}</p>
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
