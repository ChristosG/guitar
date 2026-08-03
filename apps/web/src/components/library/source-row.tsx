"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import {
  AlertTriangle,
  CheckCircle2,
  Combine,
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
import { compileSource, renameSource, type SourceOut, type SourceProgressOut } from "@/lib/api";
import { jobErrorText } from "@/lib/job-errors";

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
  /** Re-read the whole book with the vision model (`POST .../reocr`) —
   * DISTINCT from `onRetry`, which re-runs whatever the source's own ingest
   * was. Separate because their costs are nothing alike: retry re-fetches a
   * URL, this queues hours of model time against the tutor's subscription cap
   * (see `reocrSource`), so it asks first and says what it will cost. */
  onReocr: (id: string) => void;
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
  onReocr,
  onDelete,
  onMove,
  onChanged,
}: SourceRowProps) {
  const t = useTranslations("library");
  const confirm = useConfirm();
  const [renaming, setRenaming] = useState(false);
  const [compiling, setCompiling] = useState(false);

  // `ocr_active` (a server fact — an in-flight GenerationJob) is the authority on
  // "is this book being read right now", not the presence of a poll result.
  const reading = progress?.active ?? source.ocr_active ?? false;
  // The LATEST run's failure, localized — suppressed while a run is live
  // (stale news beside a spinner) and absent when the latest run succeeded
  // (the API only decorates a FAILED latest job).
  const tJobErrors = useTranslations("jobErrors");
  const lastOcrError =
    !reading && source.last_ocr_error_kind
      ? jobErrorText(
          { error: source.last_ocr_error ?? null, error_kind: source.last_ocr_error_kind },
          tJobErrors,
        )
      : null;
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
  // Added alongside the `source_status_from_page_counts` fix (`b886bd5`) that
  // made `pending > 0` force `"partial"` even at `ready === 0`. This is what
  // lets the row tell "a few pages failed" apart from "nobody has pressed
  // read yet" — see the `isPartial` branch below.
  const pending = progress?.pending ?? source.pages_pending ?? 0;
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

  /** Re-reading a book is one vision call per unread page, at ~40 seconds each,
   * against the tutor's subscription cap. On his real library that is 888 pages
   * and 8-12 HOURS — the copy used to say "several minutes", which was true of
   * the 77-page book it was written for and is now off by two orders of
   * magnitude.
   *
   * The server makes it SAFE (only unread pages are picked up, a second job
   * can't start while one is running, and it resumes where it stopped) — but it
   * cannot make it free, so this asks first and says what it will actually cost.
   * It is not destructive, so the dialog is not styled as such. */
  async function requestReocr() {
    const ok = await confirm({
      title: t("confirmReocr.title", { title: source.title }),
      body: t("confirmReocr.body", { pages: total || 0 }),
      confirmLabel: t("confirmReocr.confirm"),
    });
    if (ok) onReocr(source.id);
  }

  /** Read this book INTO the concept canon (Part B, C7 → `POST .../compile`).
   * Self-contained like the rename dialog (which owns its own PATCH) rather than
   * routed through the parent, since the whole library page does not otherwise
   * know about the canon. THE MONEY GUARD is server-side: an already-compiled
   * book starts no job and re-spends nothing (`compileSource`'s docstring), and a
   * second press returns the same running job — but the FIRST read of a book is
   * real model time against the tutor's subscription, so this asks first and says
   * so. A failed/absent key surfaces through the app-shell's global banner, not
   * here. On success we just refresh; the row then shows "reading into the
   * canon…" from the server's own `compile.status`. */
  async function requestCompile() {
    const ok = await confirm({
      title: t("compile.confirmTitle", { title: source.title }),
      body: t("compile.confirmBody"),
      confirmLabel: t("compile.confirmButton"),
    });
    if (!ok) return;
    setCompiling(true);
    try {
      await compileSource(source.id);
      onChanged();
    } catch {
      // Quiet: the one actionable failure (no LLM key) is already shown by the
      // shell's global banner on every page; a compile that could not start has
      // written nothing and can simply be pressed again.
    } finally {
      setCompiling(false);
    }
  }

  /** Recompile a book ALREADY in the canon, on purpose (`?force=true`). Unlike the
   * first compile, this bypasses the server's money guard: it re-reads the WHOLE
   * book with Claude (real subscription/model time) and REPLACES this book's canon
   * concepts. That is a deliberate spend the tutor initiates — never a silent one —
   * so it asks first and the modal spells out both the cost and the replacement.
   * Same quiet-failure handling as `requestCompile`. */
  async function requestRecompile() {
    const ok = await confirm({
      title: t("compile.confirmRecompileTitle", { title: source.title }),
      body: t("compile.confirmRecompileBody"),
      confirmLabel: t("compile.recompile"),
    });
    if (!ok) return;
    setCompiling(true);
    try {
      await compileSource(source.id, { force: true });
      onChanged();
    } catch {
      // Quiet: see requestCompile — the one actionable failure is the shell's
      // global banner, and a recompile that could not start has changed nothing.
    } finally {
      setCompiling(false);
    }
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
        ) : isPartial && pending === 0 ? (
          // AMBER, not green. 71 of 77 pages read is not "Ready" — and it is not
          // broken either. The failed pages have their own retry, which re-reads
          // ONLY them (`ocr_source` never re-reads a page that is already ready).
          //
          // Gated on `pending === 0` on purpose: this is the ONLY shape where the
          // remaining work is cheap and bounded (a handful of already-attempted
          // pages), which is what earns a bare, no-confirmation button. See the
          // branch below for the other shape `isPartial` now covers.
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
            {lastOcrError && (
              <span data-testid={`ocr-last-error-${source.id}`} className="w-full text-xs text-destructive">
                {lastOcrError}
              </span>
            )}
          </div>
        ) : isPartial ? (
          // `pending > 0` — the footgun `b886bd5` opened. Before that fix,
          // `"partial"` was reachable only via `char_count > 0 AND failed > 0`,
          // which REQUIRES `ready > 0` (some real progress already made), so
          // "retry the stragglers, no confirmation" was a fair default: cheap
          // and bounded. `b886bd5` made `"partial"` truthfully cover any source
          // with unread pages too — including `ready=0, failed=0, pending=total`,
          // a book that has NEVER been opened. Routing that shape through the
          // SAME no-confirmation button above would have made one misread click
          // start the full 8-12 hour read. So instead this shape reuses
          // `requestReocr` — THE SAME confirming dialog `reocr` already uses,
          // stating the page count and the hours-long cost before it starts.
          //
          // Two sub-shapes share this branch:
          //   - `ready === 0`: nothing has been read at all. Reads as an
          //     invitation ("not read yet"), not an error — no AlertTriangle,
          //     no amber/destructive color.
          //   - `ready > 0`: an interrupted run (e.g. parked by a rate limit
          //     mid-book). This is genuinely a RESUME, not "a few pages failed"
          //     (the unread pages are `pending`, not `failed`) and not a fresh
          //     book either — but its cost is the same open question as a fresh
          //     book's (an unknown, possibly large number of pages still
          //     unread), so it earns the same confirmation. Only the copy
          //     differs, to say how far the book already got.
          <div
            data-testid={`status-unread-${source.id}`}
            className="flex flex-wrap items-center gap-2 text-sky-600 dark:text-sky-400"
          >
            <span className="flex items-center gap-1.5 text-sm font-medium">
              <RefreshCw className="size-3.5 shrink-0" />
              {ready > 0
                ? t("status.resuming", { ready, total, pending })
                : t("status.unread", { total })}
            </span>
            <Button
              type="button"
              size="xs"
              variant="outline"
              disabled={retrying}
              data-testid={`start-reading-${source.id}`}
              onClick={requestReocr}
            >
              {retrying ? t("retrying") : ready > 0 ? t("continueReading") : t("startReading")}
            </Button>
            {/* The last run's verdict, when it FAILED — without this line an
                auth-parked read (key revoked / out of credit at page 40) looked
                like a healthy resumable book, and every Continue press failed
                again in silence. `jobErrors` copy is the same localized
                taxonomy every other failed-job surface renders. */}
            {lastOcrError && (
              <span data-testid={`ocr-last-error-${source.id}`} className="w-full text-xs text-destructive">
                {lastOcrError}
              </span>
            )}
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

        {/* CONCEPT-CANON compile status, beside the OCR status (Part B, C7).
            Rendered ONLY when the API actually reports the field: `undefined`
            means an API build that predates C7 (the field is decorated from
            `book_compile`, so an older API simply omits it) — showing "not
            compiled · Compile" on every book in that window would be a lie, so
            the row looks exactly as it did before until the field arrives. Once
            it does: `ready` links to the canon with its concept count, `running`
            spins, `failed` offers a retry, and `null` (never compiled) offers the
            Compile button the ethos asks for. Hidden while the book is being OCR'd
            (compile runs after a book is readable). */}
        {!reading && source.compile !== undefined && (
          source.compile_active ? (
            /* An in-flight compile JOB, decorated like `ocr_active` — checked
               FIRST because `book_compile` itself only flips to "running"
               after the worker has assembled the whole book's context
               (seconds on a big book), and until this flag the row showed
               "not compiled · [Compile]" (or "ready · Recompile") for the
               opening stretch of a paid compile the tutor just started. */
            <span
              data-testid={`compile-running-${source.id}`}
              className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground"
            >
              <Loader2 className="size-3 shrink-0 animate-spin" />
              {t("compile.running")}
            </span>
          ) : source.compile?.status === "ready" ? (
            <span className="mt-0.5 flex flex-wrap items-center gap-2">
              <Link
                href={`/${locale}/canon`}
                data-testid={`compile-ready-${source.id}`}
                className="flex w-fit items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
              >
                <Combine className="size-3 shrink-0 text-primary" />
                {source.compile.concept_count != null
                  ? t("compile.ready", { count: source.compile.concept_count })
                  : t("compile.readyNoCount")}
              </Link>
              {/* Recompile ON PURPOSE (force). Distinct from the first Compile: it
                  re-spends real model time and REPLACES this book's canon concepts,
                  so it is a small outline button behind a confirm, not a bare link. */}
              <Button
                type="button"
                size="xs"
                variant="outline"
                disabled={compiling}
                data-testid={`recompile-${source.id}`}
                onClick={requestRecompile}
              >
                {compiling ? t("compile.compiling") : t("compile.recompile")}
              </Button>
            </span>
          ) : source.compile?.status === "running" ? (
            <span
              data-testid={`compile-running-${source.id}`}
              className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground"
            >
              <Loader2 className="size-3 shrink-0 animate-spin" />
              {t("compile.running")}
            </span>
          ) : source.compile?.status === "failed" ? (
            <span
              data-testid={`compile-failed-${source.id}`}
              className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-destructive"
            >
              <span className="flex items-center gap-1.5">
                <AlertTriangle className="size-3 shrink-0" />
                {t("compile.failed")}
              </span>
              <Button
                type="button"
                size="xs"
                variant="outline"
                disabled={compiling}
                data-testid={`compile-${source.id}`}
                onClick={requestCompile}
              >
                {compiling ? t("compile.compiling") : t("compile.retry")}
              </Button>
            </span>
          ) : isReadable && source.compile === null ? (
            <span
              data-testid={`compile-none-${source.id}`}
              className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
            >
              {t("compile.none")}
              <Button
                type="button"
                size="xs"
                variant="outline"
                disabled={compiling}
                data-testid={`compile-${source.id}`}
                onClick={requestCompile}
              >
                {compiling ? t("compile.compiling") : t("compile.start")}
              </Button>
            </span>
          ) : null
        )}
      </div>

      {/* The controls are ONE flex item, not four siblings: every one of them is
          shrink-0, so as siblings on a phone they crushed the `flex-1` title
          column to nothing and then wrapped one by one — a row three lines tall
          with an unreadable title. Grouped, they wrap BELOW the title as a unit
          on mobile (`w-full`) and sit inline to the right from `sm` up. */}
      <div className="flex w-full min-w-0 items-center gap-2 sm:ml-auto sm:w-auto sm:gap-3">
        <select
          aria-label={t("moveTo")}
          data-testid={`move-${source.id}`}
          disabled={moving}
          value={source.collection_id ?? ""}
          onChange={(e) => onMove(source.id, e.target.value || null)}
          className="h-7 min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-xs text-muted-foreground outline-none disabled:opacity-50 sm:max-w-48 sm:flex-none"
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

        {/* Re-read with Claude (`POST .../reocr`). The OCR job has always been
            re-runnable server-side and no UI ever offered it on a HEALTHY book — so
            a book that OCR'd badly (a bad scan, a model hiccup, or a text layer
            inherited from someone else's Tesseract) could only be fixed by deleting
            and re-uploading it. Hidden while a job is running: the server would just
            hand back the same job, but a button that looks like it does nothing is
            worse than no button. */}
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
      </div>

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
