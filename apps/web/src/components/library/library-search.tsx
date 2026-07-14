"use client";

import { useRef, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { Loader2, Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, searchKnowledge, type HitOut } from "@/lib/api";

/** How many hits to ask for. Deliberately small: this is "find me the Tube
 * Screamer chapter", not a research tool — the eighth-best passage in a 408-chunk
 * library is noise, and a long list makes the one good answer harder to see. */
const K = 6;

/** A snippet, not the whole chunk: a chunk runs to ~1,200 characters and a wall
 * of OCR'd text in a dropdown is unreadable. The window is centred on the first
 * occurrence of a query term when there is one — the whole point of a lexical arm
 * is that the words the tutor typed are IN the passage, and a snippet that cut
 * them off would hide the very evidence the hybrid index exists to surface. */
const SNIPPET_CHARS = 240;

function snippet(text: string, query: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= SNIPPET_CHARS) return flat;

  const terms = query
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((w) => w.length > 2);
  const haystack = flat.toLowerCase();
  const hit = terms.map((w) => haystack.indexOf(w)).filter((i) => i >= 0);
  const centre = hit.length > 0 ? Math.min(...hit) : 0;

  const start = Math.max(0, centre - SNIPPET_CHARS / 3);
  const end = Math.min(flat.length, start + SNIPPET_CHARS);
  return (start > 0 ? "… " : "") + flat.slice(start, end).trim() + (end < flat.length ? " …" : "");
}

/** ASK YOUR LIBRARY (Stage 8) — a search box ON THE LIBRARY PAGE, over the hybrid
 * (e5 dense + BM25, RRF-fused) index.
 *
 * `searchKnowledge` has existed in `lib/api.ts` since the Knowledge Brain shipped
 * and was called by NOTHING; the app shell had a DISABLED search stub in the top
 * bar (removed with this component — an absent box is more honest than a dead
 * one). The tutor could not find the Tube Screamer chapter in his own 77-page book
 * except by scrolling it.
 *
 * WHY IT'S WORTH ANYTHING: the dense arm alone is confidently wrong here. "Tube
 * Screamer" appears verbatim in 7 chunks of his book; e5's top hit for that exact
 * query scores 0.844 and contains NONE of them — it returns something that sounds
 * like pedal talk. BM25 cannot fail that way, and the fused index puts a chunk
 * that actually says the words at rank #1 (see `app/brain/retrieve.py`'s measured
 * table). So every hit here carries its source, its page, and a snippet — and
 * clicking it OPENS THE READER AT THAT PAGE, where the scan is. A citation you
 * can't open is just a claim.
 *
 * Deliberately NOT a Cmd+K global palette and NOT a second "Ask" mode: the chat
 * agent already IS grounded search with citations. This is the "where is it in my
 * book" affordance, and it lives where the books are. */
export function LibrarySearch({ locale }: { locale: string }) {
  const t = useTranslations("library.search");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<HitOut[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState("");
  // Only the LAST submitted query may write results: two searches in flight (he
  // typed, waited, typed again) can land out of order, and an older, slower
  // response would otherwise overwrite the newer one's hits.
  const latest = useRef(0);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    const seq = ++latest.current;
    setSearching(true);
    setError(null);
    try {
      const { hits: found } = await searchKnowledge({ query: q, k: K });
      if (seq !== latest.current) return;
      setHits(found);
      setSearched(q);
    } catch (err) {
      if (seq !== latest.current) return;
      setError(err instanceof ApiError ? err.detail : t("error"));
      setHits(null);
    } finally {
      if (seq === latest.current) setSearching(false);
    }
  }

  function clear() {
    latest.current++;          // invalidate anything still in flight
    setQuery("");
    setHits(null);
    setError(null);
    setSearching(false);
  }

  return (
    <div className="flex flex-col gap-3">
      <form onSubmit={handleSubmit} className="flex items-center gap-2">
        <div className="relative min-w-0 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("placeholder")}
            aria-label={t("placeholder")}
            data-testid="library-search-input"
            className="pl-8"
          />
        </div>
        <Button type="submit" disabled={searching || !query.trim()} data-testid="library-search-submit">
          {searching && <Loader2 className="size-4 animate-spin" />}
          {searching ? t("searching") : t("submit")}
        </Button>
        {(hits !== null || error) && (
          <Button type="button" variant="ghost" size="icon-sm" aria-label={t("clear")}
                  data-testid="library-search-clear" onClick={clear}>
            <X />
          </Button>
        )}
      </form>

      {error && (
        <p role="alert" data-testid="library-search-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {hits !== null && hits.length === 0 && !error && (
        <p data-testid="library-search-empty" className="text-sm text-muted-foreground">
          {t("noResults", { query: searched })}
        </p>
      )}

      {hits !== null && hits.length > 0 && (
        <ul data-testid="library-search-results" className="flex flex-col gap-2">
          {hits.map((hit) => (
            <li key={hit.chunk_id}>
              {/* The whole hit is the link. A citation that can't be opened is a
                  claim; `?page=N` is the Reader's deep-link contract (its own
                  docstring pins it) and the page marker exists the instant the
                  manifest lands, so this scrolls to the right page immediately. */}
              <Link
                href={
                  hit.page != null
                    ? `/${locale}/library/${hit.source_id}?page=${hit.page}`
                    : `/${locale}/library/${hit.source_id}`
                }
                data-testid={`search-hit-${hit.chunk_id}`}
                className="block rounded-lg border border-border bg-card px-3 py-2 transition-colors hover:border-foreground/30 hover:bg-muted/40"
              >
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="min-w-0 truncate text-sm font-medium">{hit.source_title}</span>
                  {hit.page != null && (
                    <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {t("page", { page: hit.page })}
                    </span>
                  )}
                </div>
                <p className="mt-1 line-clamp-3 text-sm text-muted-foreground">
                  {snippet(hit.text, searched)}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
