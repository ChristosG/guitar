"use client";

import { useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import { Download, Loader2, TriangleAlert, Upload } from "lucide-react";

import { ApiError, backupExportUrl, restoreBackup, serverBackupExportUrl } from "@/lib/api";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm";
import { cn } from "@/lib/utils";

/** BACKUP / RESTORE (workstream A3) — everything the tutor has (database +
 * page scans) as ONE downloadable archive, and the way back from it. The
 * archive is the same format the desktop seed bundle uses, so seed = backup =
 * restore are a single format (`app/routers/backup.py`'s module docstring).
 *
 * EXPORT IS A PLAIN ANCHOR, not a fetch: a backup can be hundreds of MB of
 * scans, and the browser/webview's own download path streams it to disk with
 * its own progress UI (see `backupExportUrl`'s docstring in `lib/api.ts`).
 * Its href is CLIENT state, not a value computed during render — see
 * `exportUrl` below; this is the one place in the app that renders an API URL
 * into HTML instead of merely fetching one.
 *
 * RESTORE IS THE MOST DESTRUCTIVE BUTTON IN THE APP — it replaces ALL data,
 * not one curriculum, so the confirm copy says exactly that, names the chosen
 * file, and the accept button is red (`useConfirm`, same posture as
 * `blueprint-default-card.tsx`'s Restore). On success the whole app reloads:
 * every screen is stale by definition after a restore. Failures arrive as a
 * machine `code` and render as exactly one sentence from `backup.errors.*` in
 * the tutor's language — never a status number. The server's own message
 * (e.g. a `pg_restore` error) renders underneath that sentence, verbatim and
 * in monospace, so a stuck tutor has something to paste into a support
 * message — see `errorDetail` below. */
export function BackupCard() {
  const t = useTranslations("backup");
  const confirm = useConfirm();

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [restoring, setRestoring] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);

  /** WHERE EXPORT ACTUALLY POINTS — a client fact, read after hydration.
   *
   * `backupExportUrl()` reads a RUNTIME fact (the desktop shell's injected port,
   * or the page's own origin). A render does not always happen in a browser:
   * this Card is server-rendered once even though the page is "use client", and
   * on the server neither fact exists, so the URL came out as the only thing
   * `serverApiBase()` can know there — in every deployment we ship, its
   * hardcoded `http://localhost:8791`. React does not repair a mismatched
   * attribute during hydration — it says so and moves on — so that default was
   * final. On the deployed site every visitor's Export button pointed at their
   * OWN localhost; on desktop it pointed at a port the app had not won, so the
   * shell's `on_download` handler, which exists precisely to catch this
   * download, never fired.
   *
   * `useSyncExternalStore` is the hook for exactly this shape — a value React
   * cannot compute the same way on both sides. It renders the SERVER snapshot
   * during SSR *and* during hydration, so the markup matches and React keeps the
   * DOM it already has (the anchor is a real, focusable, clickable link from the
   * very first paint — never a dead `href`-less one), then re-reads the client
   * snapshot once hydration is done and updates the attribute for real.
   *
   * Both snapshots return plain strings, so the identity check React does on
   * every render is a value comparison: in the webapp-on-localhost case the two
   * are equal and nothing re-renders at all, which is why today's behaviour is
   * untouched there. The store never changes after boot — the shell injects
   * before any page script and never revises it — so `subscribe` is a no-op.
   *
   * WHAT THIS DELIBERATELY LEAVES OPEN: between first byte and hydration the
   * anchor carries the SERVER's base — `http://localhost:8791/backup/export` on
   * every deployment that does not set `NEXT_PUBLIC_API_BASE`, which is all of
   * them. A click that lands inside that window follows it: on the deployed site
   * to the VISITOR's own machine, in the desktop app to a port the shell may not
   * have won. Either way the download does not start and nothing says why.
   *
   * It stays open because nothing can close it. What corrects the href is client
   * code, and "before hydration" is precisely "before client code runs", so
   * there is no handler to `preventDefault` in, no state to gate on, no listener
   * attached yet: every such guard would come alive AT hydration — the same
   * moment the href stops being wrong — and would therefore protect exactly
   * nothing. The only lever that acts inside the window is what the server put
   * in the HTML, and both alternatives are worse:
   *
   *   - render no `href` until an effect runs: a styled non-link — unfocusable,
   *     not right-clickable, dead to the keyboard — for the very same window,
   *     and a real regression for everyone who never clicks that fast.
   *     `tests/api-base.spec.ts` pins the server-rendered href precisely because
   *     that shape was tried and rejected.
   *   - server-render `pointer-events-none` and drop it on hydration: no layout
   *     shift, but it swallows the click silently instead of following a wrong
   *     URL — the same "nothing happened, no feedback" outcome it is meant to
   *     prevent — and it does not stop Enter on a focused link anyway.
   *   - point the href at a same-origin route that redirects: the Next server is
   *     never told the API port either (the shell injects it into the WEBVIEW;
   *     `supervisor.rs::start_node` gives the node child PORT, HOSTNAME and
   *     NODE_ENV, and nothing else), so that route would have to guess the one
   *     fact this whole design exists to stop guessing.
   *
   * What makes leaving it acceptable rather than merely unavoidable: the window
   * is the milliseconds between paint and hydration, on a card the tutor has to
   * scroll a Settings page to reach; the failure mode is a download that does
   * not start, never a wrong or destructive action; and the remedy is clicking
   * again, which by then works. */
  const exportUrl = useSyncExternalStore(
    subscribeToNothing,
    backupExportUrl,
    serverBackupExportUrl,
  );

  async function onFilePicked(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Clear the input so picking the SAME file again re-fires onChange — the
    // natural retry motion after a failed restore.
    event.target.value = "";
    if (!file) return;

    const ok = await confirm({
      title: t("restoreConfirmTitle"),
      body: t("restoreConfirmBody", { name: file.name }),
      confirmLabel: t("restoreConfirm"),
      destructive: true,
    });
    if (!ok) return;

    setRestoring(true);
    setErrorCode(null);
    setErrorDetail(null);
    try {
      await restoreBackup(file);
      // Deliberately NOT setRestoring(false) first: the busy state must hold
      // until the reload actually replaces this page with the restored app.
      window.location.reload();
    } catch (e) {
      setErrorCode(codeOf(e, "restore_failed"));
      // `ApiError.detail` is already the server's `detail.message` prose (see
      // `parseError` in `lib/api.ts`) — unlike the Greek sentence above, this
      // is shown VERBATIM, because a tutor stuck on a failed restore needs
      // something to paste into a support message.
      setErrorDetail(e instanceof ApiError ? e.detail : null);
      setRestoring(false);
    }
  }

  return (
    <Card data-testid="backup-card">
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-sm text-muted-foreground">{t("help")}</p>

        <div className="flex flex-wrap items-center gap-2">
          {/* An anchor styled as a button — the whole point (see the card
              docstring), and what the spec asserts the href of. */}
          <a
            href={exportUrl}
            download
            data-testid="backup-export"
            className={cn(buttonVariants({ variant: "default" }), restoring && "pointer-events-none opacity-50")}
          >
            <Download className="size-4" />
            {t("export")}
          </a>

          <Button
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
            disabled={restoring}
            data-testid="backup-restore"
          >
            {restoring ? <Loader2 className="size-4 animate-spin" /> : <Upload className="size-4" />}
            {restoring ? t("restoring") : t("restore")}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".gz,.tgz,.tar,application/gzip,application/x-tar"
            onChange={onFilePicked}
            className="hidden"
            data-testid="backup-file-input"
          />
        </div>

        {errorCode && (
          <div
            role="alert"
            data-testid="backup-error"
            className="flex items-start gap-2 rounded-lg bg-destructive/10 p-3 text-sm text-destructive"
          >
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            <div>
              <span>
                {t.has(`errors.${errorCode}`)
                  ? t(`errors.${errorCode}`)
                  : t("errors.restore_failed")}
              </span>
              {errorDetail && (
                <span
                  data-testid="backup-error-detail"
                  className="block font-mono text-xs text-destructive/80 break-all"
                >
                  {errorDetail}
                </span>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** The API base is decided once, before any page script runs, and never moves
 * afterwards — so there is nothing to subscribe to. Module scope keeps the
 * reference stable across renders, which is what `useSyncExternalStore` needs
 * to avoid resubscribing on every one. */
function subscribeToNothing(): () => void {
  return () => {};
}

/** The server's machine-readable `code`, or a fallback — which Greek sentence
 * renders is decided by THIS, never by `detail` (server prose; mirrors
 * `blueprint-default-card.tsx`'s own `codeOf`). `detail` is still shown, but
 * only as an unbranched-on extra underneath — see `errorDetail`. */
function codeOf(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.code ? e.code : fallback;
}
