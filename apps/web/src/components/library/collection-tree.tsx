"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Pencil, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { RenameDialog } from "@/components/library/rename-dialog";
import { SourceRow } from "@/components/library/source-row";
import { renameCollection, type SourceOut, type SourceProgressOut } from "@/lib/api";

/** One collection's worth of sources, already grouped by the parent page
 * (`library/page.tsx`'s `useMemo`d `groups`) — this component only renders,
 * it does no grouping itself. `key`/`testId` are precomputed there too:
 * `testId` is `collection-${name}` for a real `Collection`, or the fixed
 * literal `collection-unfiled` for the synthetic null-FK bucket (spec: the
 * Unfiled testid is NOT translated/derived from a name, so it stays stable
 * across locales). */
export interface SourceGroup {
  key: string;
  testId: string;
  name: string;
  /** The real `Collection.id`, or `null` for the synthetic Unfiled bucket —
   * which is not a row and therefore cannot be deleted. This field is the
   * only thing that tells the two apart here. */
  collectionId: string | null;
  sources: SourceOut[];
}

interface CollectionTreeProps {
  groups: SourceGroup[];
  locale: string;
  collectionOptions: { id: string | null; name: string }[];
  /** Server-computed OCR progress, keyed by source id — only ever populated for
   * sources the API reports as `ocr_active` (see `library/page.tsx`). */
  progress: Record<string, SourceProgressOut>;
  retryingId: string | null;
  deletingId: string | null;
  deletingCollectionId: string | null;
  movingId: string | null;
  onRetry: (id: string) => void;
  onDelete: (id: string) => void;
  onDeleteCollection: (id: string) => void;
  onMove: (id: string, collectionId: string | null) => void;
  /** Re-read the list after a mutation this component owns (a rename). */
  onChanged: () => void;
}

/** All collections rendered flat and always-expanded — deliberately no
 * accordion/collapse here (design brief: "calm, obvious, few controls,
 * nothing to fill in"). A tutor checking "is my stuff okay" should see every
 * status at a glance, with zero clicks to reveal it; for the realistic
 * number of collections/sources this app expects, an always-open list reads
 * calmer than a stack of boxes to open. */
export function CollectionTree({
  groups,
  locale,
  collectionOptions,
  progress,
  retryingId,
  deletingId,
  deletingCollectionId,
  movingId,
  onRetry,
  onDelete,
  onDeleteCollection,
  onMove,
  onChanged,
}: CollectionTreeProps) {
  const t = useTranslations("library");
  const confirm = useConfirm();
  // Which folder's rename dialog is open, by id. One dialog per group, mounted
  // inside the group's own header — a single shared dialog would need the group
  // hoisted into state anyway, and this keeps the value seeding trivial.
  const [renamingId, setRenamingId] = useState<string | null>(null);

  /** Deleting a folder is the one destructive action here that does NOT lose
   * anything: the FK is `SET NULL`, so its sources land in Unfiled (spec D7).
   * The dialog says so in as many words — a warning that overstates the
   * damage trains the tutor to click through warnings. */
  async function requestDeleteCollection(group: SourceGroup) {
    if (!group.collectionId) return; // Unfiled isn't a row; the button isn't rendered for it
    const ok = await confirm({
      title: t("confirmDeleteCollection.title", { name: group.name }),
      body: t("confirmDeleteCollection.body", { count: group.sources.length }),
      confirmLabel: t("confirmDeleteCollection.confirm"),
      destructive: true,
    });
    if (ok) onDeleteCollection(group.collectionId);
  }

  return (
    <div className="flex flex-col gap-6">
      {groups.map((group) => (
        <section key={group.key} data-testid={group.testId} className="flex flex-col">
          <div className="mb-1 flex items-center justify-between gap-2">
            <h2 className="min-w-0 truncate text-sm font-semibold text-foreground">{group.name}</h2>
            <div className="flex shrink-0 items-center gap-1.5">
              <span className="text-xs text-muted-foreground">
                {t("sourceCount", { count: group.sources.length })}
              </span>
              {group.collectionId && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={t("renameCollection")}
                  data-testid={`rename-collection-${group.collectionId}`}
                  onClick={() => setRenamingId(group.collectionId)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <Pencil />
                </Button>
              )}
              {group.collectionId && (
                <RenameDialog
                  open={renamingId === group.collectionId}
                  onOpenChange={(open) => setRenamingId(open ? group.collectionId : null)}
                  value={group.name}
                  heading={t("renameDialog.collectionHeading")}
                  description={t("renameDialog.collectionDescription")}
                  onSubmit={(name) => renameCollection(group.collectionId!, name)}
                  onRenamed={onChanged}
                />
              )}
              {group.collectionId && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={t("deleteCollection")}
                  data-testid={`delete-collection-${group.collectionId}`}
                  disabled={deletingCollectionId === group.collectionId}
                  onClick={() => requestDeleteCollection(group)}
                  className="text-muted-foreground hover:text-destructive"
                >
                  {deletingCollectionId === group.collectionId ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Trash2 />
                  )}
                </Button>
              )}
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card px-4">
            {group.sources.length === 0 ? (
              <p className="py-3 text-sm text-muted-foreground">{t("collectionEmpty")}</p>
            ) : (
              group.sources.map((source) => (
                <SourceRow
                  key={source.id}
                  source={source}
                  locale={locale}
                  progress={progress[source.id]}
                  retrying={retryingId === source.id}
                  deleting={deletingId === source.id}
                  moving={movingId === source.id}
                  collectionOptions={collectionOptions}
                  onRetry={onRetry}
                  onDelete={onDelete}
                  onMove={onMove}
                  onChanged={onChanged}
                />
              ))
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
