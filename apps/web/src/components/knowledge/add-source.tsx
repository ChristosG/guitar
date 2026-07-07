"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, createSource, uploadSource } from "@/lib/api";

type Mode = "text" | "url" | "pdf";

const MODES: Mode[] = ["text", "url", "pdf"];

/** The "Add source" form: paste text, point at a URL, or (optionally) upload a
 * PDF. POSTs to the API and calls `onCreated` so the parent can refresh the
 * source list — this component owns no list state itself. */
export function AddSourceForm({ onCreated }: { onCreated: () => void }) {
  const t = useTranslations("knowledge.addSource");

  const [mode, setMode] = useState<Mode>("text");
  const [title, setTitle] = useState("");
  const [domain, setDomain] = useState("");
  const [language, setLanguage] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setTitle("");
    setDomain("");
    setLanguage("");
    setText("");
    setUrl("");
    setFile(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "pdf") {
        if (!file) return; // the `required` file input already guards normal submission
        await uploadSource({
          title,
          domain: domain || undefined,
          language: language || undefined,
          file,
        });
      } else {
        await createSource({
          kind: mode,
          title,
          domain: domain || undefined,
          language: language || undefined,
          text: mode === "text" ? text : undefined,
          url: mode === "url" ? url : undefined,
        });
      }
      reset();
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("heading")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
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
                {t(m === "text" ? "kindText" : m === "url" ? "kindUrl" : "kindPdf")}
              </Button>
            ))}
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-source-title">{t("title")}</Label>
              <Input
                id="add-source-title"
                data-testid="add-source-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={t("titlePlaceholder")}
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-source-domain">{t("domain")}</Label>
              <Input
                id="add-source-domain"
                data-testid="add-source-domain"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                placeholder={t("domainPlaceholder")}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="add-source-language">{t("language")}</Label>
              <Input
                id="add-source-language"
                data-testid="add-source-language"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                placeholder={t("languagePlaceholder")}
              />
            </div>
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

          {error && (
            <p role="alert" data-testid="add-source-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <Button type="submit" disabled={submitting} data-testid="add-source-submit" className="self-start">
            {submitting ? t("submitting") : t("submit")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
