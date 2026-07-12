"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, FileText, Link2, Loader2, StickyNote, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { SourceOut } from "@/lib/api";

/** Statuses the honesty requirement (spec D6) applies to: a source that
 * genuinely has nothing readable in it. Rendered LOUD/red/destructive with a
 * Retry button — never a green checkmark. Regression: three real sources
 * once sat "ready" with 0 chars and rendered green for two days, hiding a
 * broken knowledge base from the tutor. */
const BROKEN_STATUSES = new Set(["empty", "failed"]);

/** Belt-and-suspenders on top of `BROKEN_STATUSES`: the backend's own D6 fix
 * (`ingest.py`) only stamps NEW ingests "empty" instead of "ready" when
 * `char_count === 0` — it does not backfill rows written before that fix
 * landed. Verified live against the real API while building this page: three
 * pre-existing sources ("Guitar amplifier (Wikipedia)", "Distortion (music)
 * (Wikipedia)", "Humbucker (Wikipedia)") still report `status: "ready"` with
 * `char_count: 0` today — literally the incident this redesign exists to
 * stop repeating. The honesty requirement ("NEVER show a success affordance
 * for a source with no content") is about the CONTENT, not just the status
 * string, so this component distrusts a "ready" status when `char_count` is
 * exactly 0 and renders it as broken anyway, rather than trusting a label
 * that a backfill migration was never run to correct. */
function isEmptyContent(source: SourceOut): boolean {
  return source.status === "ready" && source.char_count === 0;
}

/** Only these source types have an original to re-fetch (`routers/
 * library.py::retry_source`'s docstring: "pdf" re-runs OCR, "url" re-fetches
 * its stored URL, anything else — "text"/"note"/"image" — 409s). The Retry
 * button is gated on this so this component never fires a doomed request;
 * a non-retryable broken source gets an explanatory line instead. */
function isRetryable(type: string): boolean {
  return type === "pdf" || type === "url";
}

function TypeIcon({ type }: { type: string }) {
  if (type === "pdf") return <FileText className="size-4 shrink-0 text-muted-foreground" />;
  if (type === "url") return <Link2 className="size-4 shrink-0 text-muted-foreground" />;
  return <StickyNote className="size-4 shrink-0 text-muted-foreground" />;
}

export interface OcrProgress {
  ready: number;
  total: number;
}

interface SourceRowProps {
  source: SourceOut;
  locale: string;
  /** Set only while THIS browser tab is actively watching an OCR/retry job
   * it just started (see `library/page.tsx`'s `watchOcr`) — a source's own
   * `status` never becomes "ocr_running" server-side (only `Page.status`
   * does), so this is how the row shows "reading your book" instead of
   * whatever stale terminal status it still has. */
  ocrProgress?: OcrProgress;
  retrying: boolean;
  deleting: boolean;
  moving: boolean;
  collectionOptions: { id: string | null; name: string }[];
  onRetry: (id: string) => void;
  onDelete: (id: string) => void;
  onMove: (id: string, collectionId: string | null) => void;
}

/** One row of the Library: title (a real link into the reader ONLY when the
 * source is genuinely ready to read — nowhere else does this app invite a
 * click into a dead end), an honest status line, a "file under" folder
 * picker, and a remove button. Pure presentational — all fetching/mutation
 * lives in the parent page. */
export function SourceRow({
  source,
  locale,
  ocrProgress,
  retrying,
  deleting,
  moving,
  collectionOptions,
  onRetry,
  onDelete,
  onMove,
}: SourceRowProps) {
  const t = useTranslations("library");
  const contentEmpty = isEmptyContent(source);
  const isBroken = !ocrProgress && (BROKEN_STATUSES.has(source.status) || contentEmpty);
  const isReady = !ocrProgress && source.status === "ready" && !contentEmpty;
  // Display-only status key: a "ready"-but-0-char row (see `isEmptyContent`
  // above) is narrated as "empty" even though the API's own `status` field
  // still (wrongly) says "ready" — never surface that stale label verbatim.
  const displayStatus = contentEmpty ? "empty" : source.status;

  return (
    <div
      data-testid={`source-${source.id}`}
      className="flex flex-wrap items-center gap-3 border-b border-border/60 py-3 last:border-b-0"
    >
      <TypeIcon type={source.type} />

      <div className="min-w-0 flex-1">
        {isReady ? (
          <Link
            href={`/${locale}/library/${source.id}`}
            className="block truncate font-medium hover:underline"
          >
            {source.title}
          </Link>
        ) : (
          <span className="block truncate font-medium">{source.title}</span>
        )}

        {ocrProgress ? (
          <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <Loader2 className="size-3.5 shrink-0 animate-spin" />
            {source.type === "pdf"
              ? t("ocrProgress", { ready: ocrProgress.ready, total: ocrProgress.total })
              : t("status.ocr_running")}
          </span>
        ) : isBroken ? (
          <div className="flex flex-wrap items-center gap-2 text-destructive">
            <span className="flex items-center gap-1.5 text-sm font-medium">
              <AlertTriangle className="size-3.5 shrink-0" />
              {t(`status.${displayStatus}`)}
            </span>
            {isRetryable(source.type) ? (
              <Button
                type="button"
                size="xs"
                variant="destructive"
                disabled={retrying}
                data-testid={`retry-${source.id}`}
                onClick={() => onRetry(source.id)}
              >
                {retrying ? t("retrying") : t("retry")}
              </Button>
            ) : (
              <span className="text-xs text-destructive/80">{t("notRetryable")}</span>
            )}
          </div>
        ) : source.status === "ready" ? (
          <span
            data-testid={`status-ok-${source.id}`}
            className="flex items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400"
          >
            <CheckCircle2 className="size-3.5 shrink-0" />
            {t("status.ready")}
            {source.char_count != null && (
              <span className="text-muted-foreground">
                · {t("charCount", { count: source.char_count })}
              </span>
            )}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <Loader2 className="size-3.5 shrink-0 animate-spin" />
            {t("status.ingesting")}
          </span>
        )}
      </div>

      <select
        aria-label={t("moveTo")}
        data-testid={`move-${source.id}`}
        disabled={moving}
        value={source.collection_id ?? ""}
        onChange={(e) => onMove(source.id, e.target.value || null)}
        className="h-7 shrink-0 rounded-md border border-border bg-background px-2 text-xs text-muted-foreground outline-none disabled:opacity-50"
      >
        {collectionOptions.map((opt) => (
          <option key={opt.id ?? "unfiled"} value={opt.id ?? ""}>
            {opt.name}
          </option>
        ))}
      </select>

      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={t("delete")}
        disabled={deleting}
        data-testid={`delete-${source.id}`}
        onClick={() => onDelete(source.id)}
        className="shrink-0 text-muted-foreground hover:text-destructive"
      >
        <Trash2 />
      </Button>
    </div>
  );
}
