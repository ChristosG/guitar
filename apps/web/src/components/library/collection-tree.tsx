"use client";

import { useTranslations } from "next-intl";
import { Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm";
import { SourceRow, type OcrProgress } from "@/components/library/source-row";
import type { SourceOut } from "@/lib/api";

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
  ocrProgress: Record<string, OcrProgress>;
  retryingId: string | null;
  deletingId: string | null;
  deletingCollectionId: string | null;
  movingId: string | null;
  onRetry: (id: string) => void;
  onDelete: (id: string) => void;
  onDeleteCollection: (id: string) => void;
  onMove: (id: string, collectionId: string | null) => void;
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
  ocrProgress,
  retryingId,
  deletingId,
  deletingCollectionId,
  movingId,
  onRetry,
  onDelete,
  onDeleteCollection,
  onMove,
}: CollectionTreeProps) {
  const t = useTranslations("library");
  const confirm = useConfirm();

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
                  ocrProgress={ocrProgress[source.id]}
                  retrying={retryingId === source.id}
                  deleting={deletingId === source.id}
                  moving={movingId === source.id}
                  collectionOptions={collectionOptions}
                  onRetry={onRetry}
                  onDelete={onDelete}
                  onMove={onMove}
                />
              ))
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
