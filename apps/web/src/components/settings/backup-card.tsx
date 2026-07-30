"use client";

import { useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Download, Loader2, TriangleAlert, Upload } from "lucide-react";

import { ApiError, backupExportUrl, restoreBackup } from "@/lib/api";
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
 *
 * RESTORE IS THE MOST DESTRUCTIVE BUTTON IN THE APP — it replaces ALL data,
 * not one curriculum, so the confirm copy says exactly that, names the chosen
 * file, and the accept button is red (`useConfirm`, same posture as
 * `blueprint-default-card.tsx`'s Restore). On success the whole app reloads:
 * every screen is stale by definition after a restore. Failures arrive as a
 * machine `code` and render as exactly one sentence from `backup.errors.*` in
 * the tutor's language — never a status number, never server prose. */
export function BackupCard() {
  const t = useTranslations("backup");
  const confirm = useConfirm();

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [restoring, setRestoring] = useState(false);
  const [errorCode, setErrorCode] = useState<string | null>(null);

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
    try {
      await restoreBackup(file);
      // Deliberately NOT setRestoring(false) first: the busy state must hold
      // until the reload actually replaces this page with the restored app.
      window.location.reload();
    } catch (e) {
      setErrorCode(codeOf(e, "restore_failed"));
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
            href={backupExportUrl()}
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
            accept=".gz,.tgz,application/gzip"
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
            <span>
              {t.has(`errors.${errorCode}`)
                ? t(`errors.${errorCode}`)
                : t("errors.restore_failed")}
            </span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** The server's machine-readable `code`, or a fallback — never `detail`, which
 * is server prose the tutor must never actually read (mirrors
 * `blueprint-default-card.tsx`'s own `codeOf`). */
function codeOf(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.code ? e.code : fallback;
}
