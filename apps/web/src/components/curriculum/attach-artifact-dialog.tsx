"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Library, LayoutGrid, Loader2 } from "lucide-react";
import { ARTIFACT_KIND_LABEL_KEY, ARTIFACT_KINDS } from "@/components/artifacts/kinds";
import type { ArtifactKind } from "@/components/artifacts/types";
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
import { ApiError, generateArtifact, type ArtifactOut } from "@/lib/api";

interface AttachArtifactDialogProps {
  blockId: string;
  blockTitle: string;
  onAttached: (artifact: ArtifactOut) => void;
}

/** The per-segment "Add artifact" action — same small-dialog-over-native-
 * prompt shape as `segment-dialog.tsx` (pick kind + prompt, not a
 * `window.prompt`) → `generateArtifact({kind, prompt, blockId})` → hands the
 * resulting Artifact to the parent BlockCard, which renders it inline right
 * below (see `block-card.tsx`'s `handleArtifactAttached`), the same "action
 * dialog, inline result" split that component's own `SegmentDialog` +
 * delivery-session rendering already establishes. `LayoutGrid` is the same
 * icon `app-shell.tsx` uses for the "Artifacts" nav item, tying this action
 * visually to that section. */
export function AttachArtifactDialog({ blockId, blockTitle, onAttached }: AttachArtifactDialogProps) {
  const t = useTranslations("curricula.attachArtifactDialog");
  const tTree = useTranslations("curricula.tree");
  const tKinds = useTranslations("artifacts.sections");

  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<ArtifactKind>("chord_diagram");
  const [prompt, setPrompt] = useState("");
  const [ground, setGround] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setKind("chord_diagram");
    setPrompt("");
    setGround(false);
    setError(null);
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const artifact = await generateArtifact({ kind, prompt, blockId, ground });
      onAttached(artifact);
      reset();
      setOpen(false);
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
        if (!next) setError(null);
      }}
    >
      <DialogTrigger
        render={<Button type="button" variant="outline" size="xs" data-testid="block-card-attach-artifact" />}
      >
        <LayoutGrid />
        {tTree("attachArtifactAction")}
      </DialogTrigger>
      <DialogContent data-testid="attach-artifact-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description", { title: blockTitle })}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>{t("kindLabel")}</Label>
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-label={t("kindLabel")}>
                {ARTIFACT_KINDS.map((k) => (
                  <Button
                    key={k}
                    type="button"
                    size="xs"
                    variant={kind === k ? "default" : "outline"}
                    aria-pressed={kind === k}
                    data-testid={`attach-artifact-kind-${k}`}
                    onClick={() => setKind(k)}
                  >
                    {tKinds(ARTIFACT_KIND_LABEL_KEY[k])}
                  </Button>
                ))}
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="attach-artifact-prompt">{t("promptLabel")}</Label>
              <Input
                id="attach-artifact-prompt"
                data-testid="attach-artifact-prompt"
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
              data-testid="attach-artifact-ground"
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
              data-testid="attach-artifact-loading"
              className="flex items-center gap-2 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm text-muted-foreground"
            >
              <Loader2 className="size-4 shrink-0 animate-spin" />
              {t("generating")}
            </div>
          )}

          {error && (
            <p role="alert" data-testid="attach-artifact-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting} data-testid="attach-artifact-submit">
              {submitting ? t("generating") : t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
