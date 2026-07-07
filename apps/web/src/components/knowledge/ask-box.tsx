"use client";

import { useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, askKnowledge, type HitOut } from "@/lib/api";

/** Grounded Q&A box: a query + an answer-locale toggle → the answer text plus
 * its numbered citations (matching the `[n]` markers the API embeds in the
 * answer). Defaults the locale toggle to the page's current locale, but lets
 * the caller ask in the other language too (demonstrates the cross-lingual
 * retrieval — the source language need not match the answer language). */
export function AskBox() {
  const t = useTranslations("knowledge.ask");
  const pageLocale = useLocale();

  const [query, setQuery] = useState("");
  const [locale, setLocale] = useState(pageLocale);
  const [answer, setAnswer] = useState<string | null>(null);
  const [citations, setCitations] = useState<HitOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await askKnowledge({ query, locale });
      setAnswer(res.text);
      setCitations(res.citations);
    } catch (err) {
      setAnswer(null);
      setCitations([]);
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
        <form onSubmit={handleSubmit} className="flex flex-col gap-2">
          <div className="flex gap-2">
            <Input
              data-testid="ask-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("placeholder")}
            />
            <Button type="submit" disabled={loading} data-testid="ask-submit">
              {loading ? t("asking") : t("submit")}
            </Button>
          </div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <span>{t("locale")}:</span>
            <Button
              type="button"
              size="xs"
              variant={locale === "en" ? "default" : "outline"}
              aria-pressed={locale === "en"}
              data-testid="ask-locale-en"
              onClick={() => setLocale("en")}
            >
              EN
            </Button>
            <Button
              type="button"
              size="xs"
              variant={locale === "el" ? "default" : "outline"}
              aria-pressed={locale === "el"}
              data-testid="ask-locale-el"
              onClick={() => setLocale("el")}
            >
              EL
            </Button>
          </div>
        </form>

        {error && (
          <p role="alert" data-testid="ask-error" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {!error && !answer && (
          <p className="text-sm text-muted-foreground" data-testid="ask-empty">
            {t("empty")}
          </p>
        )}

        {answer && (
          <div className="flex flex-col gap-2">
            <div>
              <h3 className="text-sm font-medium">{t("answerHeading")}</h3>
              <p data-testid="ask-answer" className="text-sm whitespace-pre-wrap">
                {answer}
              </p>
            </div>
            {citations.length > 0 && (
              <div>
                <h3 className="text-sm font-medium">{t("citationsHeading")}</h3>
                <ol className="flex flex-col gap-1" data-testid="ask-citations">
                  {citations.map((c, i) => (
                    <li
                      key={c.chunk_id}
                      data-testid="ask-citation-item"
                      className="text-xs text-muted-foreground"
                    >
                      [{i + 1}] <span data-testid="ask-citation-source">{c.source_title}</span> —{" "}
                      {c.text}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
