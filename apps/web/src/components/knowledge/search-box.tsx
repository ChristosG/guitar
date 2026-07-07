"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, searchKnowledge, type HitOut } from "@/lib/api";

/** Free-text search box → ranked snippet list. Self-contained: owns its own
 * query/results/error state, independent of the source list and Ask panel. */
export function SearchBox() {
  const t = useTranslations("knowledge.search");

  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<HitOut[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await searchKnowledge({ query });
      setHits(res.hits);
    } catch (err) {
      setHits(null);
      setError(err instanceof ApiError ? err.detail : t("error"));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("heading")}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form onSubmit={handleSubmit} className="flex gap-2">
          <Input
            data-testid="search-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("placeholder")}
          />
          <Button type="submit" disabled={loading} data-testid="search-submit">
            {loading ? t("searching") : t("submit")}
          </Button>
        </form>

        {error && (
          <p role="alert" data-testid="search-error" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {hits && hits.length === 0 && (
          <p className="text-sm text-muted-foreground" data-testid="search-empty">
            {t("empty")}
          </p>
        )}

        {hits && hits.length > 0 && (
          <ul className="flex flex-col gap-2" data-testid="search-results">
            {hits.map((h) => (
              <li
                key={h.chunk_id}
                data-testid="search-result-item"
                className="rounded-lg border border-border p-3 text-sm"
              >
                <div className="flex items-center justify-between gap-2">
                  <span data-testid="search-result-title" className="font-medium">
                    {h.source_title}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {t("score")}: {h.score.toFixed(3)}
                  </span>
                </div>
                <p data-testid="search-result-text" className="mt-1 text-muted-foreground">
                  {h.text}
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
