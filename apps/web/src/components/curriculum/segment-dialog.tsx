"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Scissors } from "lucide-react";
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
import { ApiError, segmentBlock, type BlockNode } from "@/lib/api";

interface SegmentDialogProps {
  blockId: string;
  blockTitle: string;
  onSegmented: (delivery: BlockNode) => void;
}

/** The per-course "Segment into sessions" action: a small dialog asking for
 * session length + cadence (a proper form, not a native `window.prompt` —
 * doesn't fit this app's shadcn styling bar) → `POST /blocks/{id}/segment`
 * → hands the resulting delivery_root tree to the parent BlockCard, which
 * renders it inline (see block-card.tsx's `handleSegmented`). */
export function SegmentDialog({ blockId, blockTitle, onSegmented }: SegmentDialogProps) {
  const t = useTranslations("curricula.segmentDialog");
  const tTree = useTranslations("curricula.tree");

  const [open, setOpen] = useState(false);
  const [sessionMinutes, setSessionMinutes] = useState("30");
  const [cadence, setCadence] = useState("1");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const delivery = await segmentBlock(blockId, {
        session_minutes: Number(sessionMinutes),
        cadence_per_week: Number(cadence),
      });
      onSegmented(delivery);
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
        render={<Button type="button" variant="outline" size="xs" data-testid="block-card-segment" />}
      >
        <Scissors />
        {tTree("segmentAction")}
      </DialogTrigger>
      <DialogContent data-testid="segment-dialog">
        <DialogHeader>
          <DialogTitle>{t("heading")}</DialogTitle>
          <DialogDescription>{t("description", { title: blockTitle })}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="segment-minutes">{t("sessionMinutes")}</Label>
              <Input
                id="segment-minutes"
                data-testid="segment-minutes"
                type="number"
                min={1}
                value={sessionMinutes}
                onChange={(e) => setSessionMinutes(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="segment-cadence">{t("cadence")}</Label>
              <Input
                id="segment-cadence"
                data-testid="segment-cadence"
                type="number"
                min={1}
                value={cadence}
                onChange={(e) => setCadence(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
          </div>

          {error && (
            <p role="alert" data-testid="segment-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" disabled={submitting} />}>
              {t("cancel")}
            </DialogClose>
            <Button type="submit" disabled={submitting} data-testid="segment-submit">
              {submitting ? t("segmenting") : t("submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
