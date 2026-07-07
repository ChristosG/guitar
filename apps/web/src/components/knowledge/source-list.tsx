"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/knowledge/status-badge";
import type { SourceOut } from "@/lib/api";

interface SourceListProps {
  sources: SourceOut[];
  loading: boolean;
  error: string | null;
  deletingId: string | null;
  onDelete: (id: string) => void;
}

/** Renders the list of ingested sources — title, type, status badge, domain,
 * char_count and a delete button per row. Pure presentational component: all
 * fetching/mutation lives in the parent page. */
export function SourceList({ sources, loading, error, deletingId, onDelete }: SourceListProps) {
  const t = useTranslations("knowledge.sources");

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("heading")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2" data-testid="source-list">
        {loading && <p className="text-sm text-muted-foreground">{t("loading")}</p>}
        {error && (
          <p role="alert" data-testid="sources-error" className="text-sm text-destructive">
            {error}
          </p>
        )}
        {!loading && !error && sources.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="source-list-empty">
            {t("empty")}
          </p>
        )}
        {sources.map((s) => (
          <div
            key={s.id}
            data-testid="source-item"
            className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"
          >
            <div className="flex flex-col gap-1">
              <div className="flex flex-wrap items-center gap-2">
                <span data-testid="source-title" className="font-medium">
                  {s.title}
                </span>
                <StatusBadge status={s.status} />
              </div>
              <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                <span data-testid="source-type">{s.type}</span>
                {s.domain && <span data-testid="source-domain">{s.domain}</span>}
                {s.char_count != null && (
                  <span data-testid="source-char-count">{t("chars", { count: s.char_count })}</span>
                )}
              </div>
              {s.status === "failed" && s.error && (
                <span className="text-xs text-destructive">{s.error}</span>
              )}
            </div>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              data-testid="source-delete"
              disabled={deletingId === s.id}
              onClick={() => onDelete(s.id)}
            >
              {deletingId === s.id ? t("deleting") : t("delete")}
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
