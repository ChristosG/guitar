"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AddSourceForm } from "@/components/knowledge/add-source";
import { AskBox } from "@/components/knowledge/ask-box";
import { SearchBox } from "@/components/knowledge/search-box";
import { SourceList } from "@/components/knowledge/source-list";
import { ApiError, deleteSource, listSources, type SourceOut } from "@/lib/api";

// The whole page is a client component: every panel below talks to the API
// directly from the browser (see lib/api.ts's docstring on why — the app
// owns CORS specifically so the browser, not the Next.js server, is the
// caller). Fetching from a Server Component here would bypass Playwright's
// `page.route` interception entirely (it would be a Node-side request, not a
// page network request), which is why this stays client-rendered.
export default function KnowledgePage() {
  const t = useTranslations("knowledge");

  const [sources, setSources] = useState<SourceOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Fetch + terminal state only, written as an explicit .then/.catch/.finally
  // chain (not async/await) so every setState call is lexically inside a
  // callback, not a bare statement in the function body — react-hooks/
  // set-state-in-effect flags the latter even when it runs after an `await`.
  // Split out from `refresh` below so the mount effect can call this
  // directly: the initial useState values above already read "loading, no
  // error, empty list", so mount has nothing to reset synchronously; only a
  // post-mount refresh (from an event handler) does.
  const fetchSources = useCallback(() => {
    return listSources()
      .then((data) => setSources(data))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("sources.error")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    fetchSources();
  }, [fetchSources]);

  // Used after a user action (add/delete) — always called from an event
  // handler, so resetting loading/error synchronously here is unproblematic.
  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    return fetchSources();
  }, [fetchSources]);

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteSource(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("sources.deleteError"));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <h1 data-testid="knowledge-title" className="text-2xl font-semibold">
        {t("title")}
      </h1>

      <AddSourceForm onCreated={refresh} />
      <SourceList
        sources={sources}
        loading={loading}
        error={error}
        deletingId={deletingId}
        onDelete={handleDelete}
      />
      <SearchBox />
      <AskBox />
    </div>
  );
}
