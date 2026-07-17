"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { BookOpen, Loader2, Search, Split, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConceptCard } from "@/components/canon/concept-card";
import {
  ApiError,
  browseCanon,
  searchConcepts,
  type CanonOverview,
  type ConceptHit,
} from "@/lib/api";

/** THE CANON, VISIBLE AT LAST. Chris, three times: *"that would also be nice to
 * see somewhere in the library, i mean the canon generations"* / *"i dont see any
 * canon component, or text anywhere."* This is that surface.
 *
 * It does two things, over the same `ConceptHit` shape:
 *   - BROWSE — the compiled canon, most-divergent first (`GET /canon/concepts`),
 *     with a one-tap "only where my books disagree" filter, because the
 *     disagreements are the reason he bought ten books.
 *   - SEARCH — "what do my books say about X?" (`POST /knowledge/concepts/search`,
 *     C8), the same cross-book answer the chat tool gives, on a page he can scroll.
 *
 * Beginner-honest states throughout (settings ethos): a canon still being read
 * shows progress; an empty one says how to fill it (compile a book in the
 * Library); nothing shows a stack trace, a status code, or an English string.
 * A client component for the same reason as every other cockpit page (it calls
 * the API straight from the browser — see `library/page.tsx`).
 */
export default function CanonPage() {
  const t = useTranslations("canon");
  const locale = useLocale();

  const [overview, setOverview] = useState<CanonOverview | null>(null);
  const [loading, setLoading] = useState(true);
  // `notReady` collapses two honest "nothing to read yet" cases into one calm
  // screen: the browse endpoint isn't deployed on this API yet (404 — the canon
  // page can ship slightly ahead of the API that serves it), OR it is deployed
  // but no book has been compiled. Both mean "there's no canon to look at yet".
  const [notReady, setNotReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [divergentOnly, setDivergentOnly] = useState(false);

  // Search sub-state. `hits === null` means "not searching, show the browse
  // list"; a non-null array (even empty) means "show search results".
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<ConceptHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searched, setSearched] = useState("");
  // Only the last submitted query may write results (out-of-order responses).
  const latest = useRef(0);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setNotReady(false);
    return browseCanon()
      .then((data) => setOverview(data))
      .catch((err) => {
        // A 404 means the browse route isn't live on this API build yet — not an
        // error the tutor did anything about, so it reads as "being built", not red.
        if (err instanceof ApiError && err.status === 404) setNotReady(true);
        else setError(err instanceof ApiError ? err.detail : t("error"));
      })
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSearch(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    const seq = ++latest.current;
    setSearching(true);
    setSearchError(null);
    try {
      const { hits: found } = await searchConcepts(q, 12);
      if (seq !== latest.current) return;
      setHits(found);
      setSearched(q);
    } catch (err) {
      if (seq !== latest.current) return;
      setSearchError(err instanceof ApiError ? err.detail : t("searchError"));
      setHits(null);
    } finally {
      if (seq === latest.current) setSearching(false);
    }
  }

  function clearSearch() {
    latest.current++;
    setQuery("");
    setHits(null);
    setSearchError(null);
    setSearching(false);
  }

  const concepts = overview?.concepts ?? [];
  const browseList = divergentOnly ? concepts.filter((c) => c.divergence) : concepts;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="canon-heading">
          {t("heading")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      {loading && (
        <p className="text-sm text-muted-foreground" data-testid="canon-loading">
          {t("loading")}
        </p>
      )}

      {!loading && error && (
        <p role="alert" data-testid="canon-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {/* Nothing to look at yet — either the route isn't deployed on this API
          build, or no book has been compiled. Calm, and it says what to do. */}
      {!loading && !error && (notReady || overview?.total_concepts === 0) && (
        <div
          data-testid="canon-empty"
          className="flex flex-col items-start gap-3 rounded-xl border border-dashed border-border bg-muted/20 p-6"
        >
          <div className="flex items-center gap-2 text-sm font-medium">
            <BookOpen className="size-4 text-muted-foreground" />
            {overview && overview.books_compiling > 0 ? t("compilingTitle") : t("emptyTitle")}
          </div>
          <p className="max-w-prose text-sm text-muted-foreground">
            {overview && overview.books_compiling > 0
              ? t("compilingBody", { count: overview.books_compiling })
              : t("emptyBody")}
          </p>
          <Link
            href={`/${locale}/library`}
            className="text-sm font-medium text-primary underline underline-offset-4"
          >
            {t("goToLibrary")}
          </Link>
        </div>
      )}

      {!loading && !error && !notReady && overview && overview.total_concepts > 0 && (
        <>
          {/* The honest header: how much of his shelf is in the canon, how many
              concepts came out, and — the headline number — on how many they
              disagree. */}
          <div
            data-testid="canon-overview"
            className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-card px-4 py-3 text-sm"
          >
            <span className="font-medium">
              {t("booksSummary", { count: overview.books_compiled })}
            </span>
            <span className="text-muted-foreground">
              {t("conceptsSummary", { count: overview.total_concepts })}
            </span>
            <span className="inline-flex items-center gap-1 font-medium text-amber-700 dark:text-amber-400">
              <Split className="size-4" />
              {t("divergencesSummary", { count: overview.divergence_count })}
            </span>
          </div>

          {overview.books_compiling > 0 && (
            <p
              data-testid="canon-compiling-note"
              className="flex items-center gap-1.5 text-sm text-muted-foreground"
            >
              <Loader2 className="size-3.5 shrink-0 animate-spin" />
              {t("compilingNote", { count: overview.books_compiling })}
            </p>
          )}

          {/* SEARCH: "what do my books say about X?" — the same cross-book answer
              the chat tool gives, on a page he can scroll (C8's route). */}
          <form onSubmit={handleSearch} className="flex items-center gap-2">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("searchPlaceholder")}
                aria-label={t("searchPlaceholder")}
                data-testid="canon-search-input"
                className="pl-8"
              />
            </div>
            <Button type="submit" disabled={searching || !query.trim()} data-testid="canon-search-submit">
              {searching && <Loader2 className="size-4 animate-spin" />}
              {searching ? t("searching") : t("searchSubmit")}
            </Button>
            {(hits !== null || searchError) && (
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={t("clearSearch")}
                data-testid="canon-search-clear"
                onClick={clearSearch}
              >
                <X />
              </Button>
            )}
          </form>

          {searchError && (
            <p role="alert" data-testid="canon-search-error" className="text-sm text-destructive">
              {searchError}
            </p>
          )}

          {/* SEARCH RESULTS take over the list when a search is active. */}
          {hits !== null ? (
            hits.length === 0 && !searchError ? (
              <p data-testid="canon-search-empty" className="text-sm text-muted-foreground">
                {t("searchNoResults", { query: searched })}
              </p>
            ) : (
              <div data-testid="canon-search-results" className="flex flex-col gap-3">
                {hits.map((hit) => (
                  <ConceptCard key={hit.concept_id} hit={hit} locale={locale} />
                ))}
              </div>
            )
          ) : (
            <>
              {/* One tap to keep only the disagreements — the reason for ten books. */}
              {overview.divergence_count > 0 && (
                <button
                  type="button"
                  data-testid="canon-divergent-toggle"
                  aria-pressed={divergentOnly}
                  onClick={() => setDivergentOnly((v) => !v)}
                  className={
                    "inline-flex w-fit items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-medium transition-colors " +
                    (divergentOnly
                      ? "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400"
                      : "border-border text-muted-foreground hover:bg-muted/50")
                  }
                >
                  <Split className="size-3.5" />
                  {t("onlyDivergences")}
                </button>
              )}

              <div data-testid="canon-list" className="flex flex-col gap-3">
                {browseList.map((hit) => (
                  <ConceptCard key={hit.concept_id} hit={hit} locale={locale} />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
