"use client";

import { useTranslations } from "next-intl";
import { Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import type { ArtifactOut } from "@/lib/api";
import { Artifact } from "./artifact";

interface ArtifactListProps {
  artifacts: ArtifactOut[];
  loading: boolean;
  error: string | null;
  deletingId: string | null;
  onDelete: (id: string) => void;
}

/** The Artifacts gallery grid: every persisted artifact, live-rendered via
 * `<Artifact>` (kind's real client renderer, incl. the lazy-mounted `tab`
 * case), with a title strip + delete action above each. Pure presentational
 * component — fetching/mutation state lives in the parent page, same split
 * as `components/knowledge/source-list.tsx`. */
export function ArtifactList({ artifacts, loading, error, deletingId, onDelete }: ArtifactListProps) {
  const t = useTranslations("artifacts");
  const confirm = useConfirm();

  // The confirm lives HERE, not in the page's `handleDelete` — the page only
  // ever receives an `id`, and a dialog that cannot name what it is deleting
  // is the useless "Are you sure?" this whole change exists to avoid. Same
  // reasoning in `source-row`: the guard belongs wherever the item's title
  // is in scope. `onDelete` is only ever reached once the tutor has said
  // yes, so the parent pages need no change at all.
  async function requestDelete(artifact: ArtifactOut) {
    const ok = await confirm({
      title: t("confirmDelete.title", { title: artifact.title }),
      body: t("confirmDelete.body"),
      confirmLabel: t("confirmDelete.confirm"),
      destructive: true,
    });
    if (ok) onDelete(artifact.id);
  }

  return (
    <div className="flex flex-col gap-3">
      <h2 className="text-sm font-medium text-muted-foreground">{t("galleryHeading")}</h2>

      {loading && <p className="text-sm text-muted-foreground">{t("loading")}</p>}
      {error && (
        <p role="alert" data-testid="artifacts-error" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && !error && artifacts.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="artifacts-empty">
          {t("empty")}
        </p>
      )}

      {artifacts.length > 0 && (
        <div className="grid gap-6 sm:grid-cols-2" data-testid="artifact-list">
          {artifacts.map((artifact) => (
            <div key={artifact.id} data-testid="artifact-item" className="flex flex-col items-start gap-2">
              <div className="flex w-full items-center justify-between gap-2">
                <span className="truncate text-sm font-medium" data-testid="artifact-item-title">
                  {artifact.title}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  data-testid="artifact-delete"
                  disabled={deletingId === artifact.id}
                  onClick={() => requestDelete(artifact)}
                  aria-label={t("delete")}
                >
                  {deletingId === artifact.id ? <Loader2 className="animate-spin" /> : <Trash2 />}
                </Button>
              </div>
              <Artifact kind={artifact.kind} spec={artifact.spec} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
