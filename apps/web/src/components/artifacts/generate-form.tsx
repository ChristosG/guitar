"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Library, Loader2, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, generateArtifact, type ArtifactOut } from "@/lib/api";
import { ARTIFACT_KIND_LABEL_KEY, ARTIFACT_KINDS } from "./kinds";
import type { ArtifactKind } from "./types";

interface GenerateArtifactFormProps {
  onGenerated: (artifact: ArtifactOut) => void;
}

/**
 * The Artifacts gallery's "create" form: pick one of the 7 kinds
 * (`add-source.tsx`'s mode-picker convention — a `role="radiogroup"` row of
 * toggle Buttons, since this app has no separate Select primitive), describe
 * it in free text, optionally ground it in the Knowledge Brain, Generate.
 *
 * Unlike `curriculum/generate-dialog.tsx`'s blocking modal (a curriculum
 * generation is a nested tree behind a 49-179s guided_json call — see that
 * component's own docstring), a single artifact spec is small and fast,
 * typically seconds (`lib/api.ts`'s docstring on `generateArtifact`), so
 * this is a plain always-visible card with a simple non-blocking spinner,
 * not a dismissal-blocking dialog — there's much less to protect against.
 *
 * The "ground in Brain" toggle is offered for all 7 kinds, not just tone/
 * gear ones — mirroring `generate_artifact`'s own documented stance
 * (`app.artifacts.generate`): grounding is most *useful* for tone recipes/
 * gear cards, but hiding the control for other kinds would be a surprising,
 * hardcoded restriction the backend itself deliberately avoids.
 */
export function GenerateArtifactForm({ onGenerated }: GenerateArtifactFormProps) {
  const t = useTranslations("artifacts.generateForm");
  const tKinds = useTranslations("artifacts.sections");

  const [kind, setKind] = useState<ArtifactKind>("chord_diagram");
  const [prompt, setPrompt] = useState("");
  const [ground, setGround] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const artifact = await generateArtifact({ kind, prompt, ground });
      onGenerated(artifact);
      setPrompt("");
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card data-testid="artifact-generate-form">
      <CardHeader>
        <CardTitle>{t("heading")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>{t("kindLabel")}</Label>
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={t("kindLabel")}>
                {ARTIFACT_KINDS.map((k) => (
                  <Button
                    key={k}
                    type="button"
                    size="sm"
                    variant={kind === k ? "default" : "outline"}
                    aria-pressed={kind === k}
                    data-testid={`artifact-kind-${k}`}
                    onClick={() => setKind(k)}
                  >
                    {tKinds(ARTIFACT_KIND_LABEL_KEY[k])}
                  </Button>
                ))}
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="artifact-generate-prompt">{t("promptLabel")}</Label>
              <Input
                id="artifact-generate-prompt"
                data-testid="artifact-generate-prompt"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={t("promptPlaceholder")}
                required
              />
            </div>

            <Button
              type="button"
              size="sm"
              variant={ground ? "default" : "outline"}
              aria-pressed={ground}
              data-testid="artifact-generate-ground"
              className="self-start"
              onClick={() => setGround((g) => !g)}
            >
              <Library />
              {t("groundToggle")}
            </Button>
          </fieldset>

          {submitting && (
            <div
              role="status"
              data-testid="artifact-generate-loading"
              className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground"
            >
              <Loader2 className="size-4 shrink-0 animate-spin" />
              {t("generating")}
            </div>
          )}

          {error && (
            <p role="alert" data-testid="artifact-generate-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <Button type="submit" disabled={submitting} data-testid="artifact-generate-submit" className="self-start">
            {submitting ? <Loader2 className="animate-spin" /> : <Sparkles />}
            {t("submit")}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
