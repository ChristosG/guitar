"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import {
  AlertTriangle,
  CheckCircle2,
  FileText,
  Link2,
  Loader2,
  Pencil,
  RefreshCw,
  StickyNote,
  Trash2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { RenameDialog } from "@/components/library/rename-dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { renameSource, type SourceOut, type SourceProgressOut } from "@/lib/api";

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

interface SourceRowProps {
  source: SourceOut;
  locale: string;
  /** Live, SERVER-COMPUTED progress for a source whose OCR is running right now
   * (`GET /knowledge/sources/{id}/progress`, polled by the parent page). It used
   * to be tab-local React state, which is why a hard reload during the tutor's
   * 9-minute OCR showed his book as RED "nothing was read" with a Retry button —
   * and why pressing that button started a SECOND job racing the first.
   *
   * Absent on the first render after a reload: `source.ocr_active` (also a server
   * fact) is what carries the row until the first poll lands, so the row is
   * honest immediately, not two seconds later. */
  progress?: SourceProgressOut;
  retrying: boolean;
  deleting: boolean;
  moving: boolean;
  collectionOptions: { id: string | null; name: string }[];
  onRetry: (id: string) => void;
  onDelete: (id: string) => void;
  onMove: (id: string, collectionId: string | null) => void;
  onChanged: () => void;
}

/** One row of the Library: title (a real link into the reader whenever the
 * source is genuinely readable — which includes `partial`: a book with 6 bad
 * pages is still a book), an honest status line, a "file under" folder picker,
 * rename, re-read, and remove. Pure presentational — all fetching/mutation lives
 * in the parent page, except the rename dialog, which owns its own PATCH. */
export function SourceRow({
  source,
  locale,
  progress,
  retrying,
  deleting,
  moving,
  collectionOptions,
  onRetry,
  onDelete,
  onMove,
  onChanged,
}: SourceRowProps) {
  const t = useTranslations("library");
  const confirm = useConfirm();
  const [renaming, setRenaming] = useState(false);

  // `ocr_active` (a server fact — an in-flight GenerationJob) is the authority on
  // "is this book being read right now", not the presence of a poll result.
  const reading = progress?.active ?? source.ocr_active ?? false;
  const contentEmpty = isEmptyContent(source);
  const isPartial = !reading && source.status === "partial";
  const isBroken = !reading && (BROKEN_STATUSES.has(source.status) || contentEmpty);
  // A partial book OPENS. Gating the Reader link on the literal string "ready"
  // would have made the new status mean "unopenable", which is not what it means.
  const isReadable = !reading && (source.status === "ready" || isPartial) && !contentEmpty;
  const displayStatus = contentEmpty ? "empty" : source.status;

  const total = progress?.total ?? source.pages_total ?? 0;
  const ready = progress?.ready ?? source.pages_ready ?? 0;
  const failed = progress?.failed ?? source.pages_failed ?? 0;
  // Before the first poll lands there is no `current_page`; `ready + 1` is the
  // page it is almost certainly on, and it is never worse than showing nothing.
  const currentPage = progress?.current_page ?? Math.min(ready + 1, total || 1);

  // `DELETE /knowledge/sources/{id}` cascades to every Page and Chunk (both
  // `ON DELETE CASCADE`) and now deletes the page scans off disk too — re-adding
  // the book means re-running OCR over 77 pages against a paid vision model. The
  // dialog quotes the indexed char count so the tutor can see the difference
  // between dropping an empty Wikipedia stub and dropping the book the whole
  // library is built on.
  async function requestDelete() {
    const ok = await confirm({
      title: t("confirmDelete.title", { title: source.title }),
      body: t("confirmDelete.body", { chars: source.char_count ?? 0 }),
      confirmLabel: t("confirmDelete.confirm"),
      destructive: true,
    });
    if (ok) onDelete(source.id);
  }

  /** Re-reading a healthy 77-page book is 77 vision calls against a paid model.
   * The server makes it SAFE (only unread pages are picked up, and a second job
   * can't start while one is running) — but it cannot make it free, so this asks
   * first. It is not destructive, so the dialog is not styled as such. */
  async function requestReocr() {
    const ok = await confirm({
      title: t("confirmReocr.title", { title: source.title }),
      body: t("confirmReocr.body", { pages: total || 0 }),
      confirmLabel: t("confirmReocr.confirm"),
    });
    if (ok) onRetry(source.id);
  }

  return (
    <div
      data-testid={`source-${source.id}`}
      className="flex flex-wrap items-center gap-3 border-b border-border/60 py-3 last:border-b-0"
    >
      <TypeIcon type={source.type} />

      <div className="min-w-0 flex-1">
        {/* A truncated title is unreadable text, and a URL-typed source's
            title is routinely a long link. The tooltip is the only way to
            read one without opening it — hover OR keyboard focus. */}
        <Tooltip>
          <TooltipTrigger
            render={
              isReadable ? (
                <Link href={`/${locale}/library/${source.id}`} className="block truncate font-medium hover:underline" />
              ) : (
                <span className="block truncate font-medium" />
              )
            }
          >
            {source.title}
          </TooltipTrigger>
          <TooltipContent>{source.title}</TooltipContent>
        </Tooltip>

        {reading ? (
          <span
            data-testid={`ocr-progress-${source.id}`}
            className="flex items-center gap-1.5 text-sm text-muted-foreground"
          >
            <Loader2 className="size-3.5 shrink-0 animate-spin" />
            {total > 0
              ? t("ocrProgress", { page: currentPage, total })
              : t("status.ocr_running")}
          </span>
        ) : isPartial ? (
          // AMBER, not green. 71 of 77 pages read is not "Ready" — and it is not
          // broken either. The failed pages have their own retry, which re-reads
          // ONLY them (`ocr_source` never re-reads a page that is already ready).
          <div
            data-testid={`status-partial-${source.id}`}
            className="flex flex-wrap items-center gap-2 text-amber-600 dark:text-amber-500"
          >
            <span className="flex items-center gap-1.5 text-sm font-medium">
              <AlertTriangle className="size-3.5 shrink-0" />
              {t("status.partial", { ready, total, failed })}
            </span>
            <Button
              type="button"
              size="xs"
              variant="outline"
              disabled={retrying}
              data-testid={`retry-${source.id}`}
              onClick={() => onRetry(source.id)}
            >
              {retrying ? t("retrying") : t("retryFailedPages")}
            </Button>
          </div>
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
            className="flex flex-wrap items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400"
          >
            <CheckCircle2 className="size-3.5 shrink-0" />
            {t("status.ready")}
            {total > 0 && (
              <span className="text-muted-foreground">· {t("pageCount", { count: total })}</span>
            )}
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
        aria-label={t("rename")}
        data-testid={`rename-${source.id}`}
        onClick={() => setRenaming(true)}
        className="shrink-0 text-muted-foreground hover:text-foreground"
      >
        <Pencil />
      </Button>

      {/* Re-read: the OCR job has always been re-runnable server-side and no UI
          ever offered it on a HEALTHY book — so a book that OCR'd badly (a bad
          scan, a model hiccup) could only be fixed by deleting and re-uploading
          it. Hidden while a job is running: the server would just hand back the
          same job, but a button that looks like it does nothing is worse than no
          button. */}
      {source.type === "pdf" && !reading && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={t("reocr")}
          disabled={retrying}
          data-testid={`reocr-${source.id}`}
          onClick={requestReocr}
          className="shrink-0 text-muted-foreground hover:text-foreground"
        >
          <RefreshCw />
        </Button>
      )}

      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={t("delete")}
        disabled={deleting}
        data-testid={`delete-${source.id}`}
        onClick={requestDelete}
        className="shrink-0 text-muted-foreground hover:text-destructive"
      >
        <Trash2 />
      </Button>

      <RenameDialog
        open={renaming}
        onOpenChange={setRenaming}
        value={source.title}
        heading={t("renameDialog.sourceHeading")}
        description={t("renameDialog.sourceDescription")}
        onSubmit={(title) => renameSource(source.id, title)}
        onRenamed={onChanged}
      />
    </div>
  );
}
