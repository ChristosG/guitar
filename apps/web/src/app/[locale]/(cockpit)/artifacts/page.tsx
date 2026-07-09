"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ArtifactList } from "@/components/artifacts/artifact-list";
import { GenerateArtifactForm } from "@/components/artifacts/generate-form";
import { ApiError, deleteArtifact, listArtifacts, type ArtifactOut } from "@/lib/api";

// Client component for the same reason as knowledge/students/curricula
// pages: it calls the API straight from the browser (see lib/api.ts's
// docstring on why — the app owns CORS specifically so the browser, not the
// Next.js server, is the caller), which is also what makes it visible to
// Playwright's `page.route`. Replaces Plan 4 Task 2/3's static in-file demo
// harness (fixed specs, no API calls, a Server Component) with the real
// gallery: a create form -> `generate` with a loading state -> live render,
// plus the persisted list from `listArtifacts()`.
export default function ArtifactsPage() {
  const t = useTranslations("artifacts");

  const [artifacts, setArtifacts] = useState<ArtifactOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Same .then/.catch/.finally shape as knowledge/page.tsx's fetchSources,
  // for the same reason: every setState call stays lexically inside a
  // callback rather than a bare statement (react-hooks/set-state-in-effect).
  const fetchArtifacts = useCallback(() => {
    return listArtifacts()
      .then((data) => setArtifacts(data))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    fetchArtifacts();
  }, [fetchArtifacts]);

  // The generate form already awaited the POST itself and only calls this on
  // success — prepend the returned (already-persisted) artifact directly
  // instead of refetching the whole list, mirroring how curricula/page.tsx's
  // handleGenerated renders its response immediately rather than waiting on
  // a follow-up GET.
  function handleGenerated(artifact: ArtifactOut) {
    setArtifacts((prev) => [artifact, ...prev]);
  }

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteArtifact(id);
      setArtifacts((prev) => prev.filter((a) => a.id !== id));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("deleteError"));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="artifacts-heading">
          {t("heading")}
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      <GenerateArtifactForm onGenerated={handleGenerated} />

      <ArtifactList
        artifacts={artifacts}
        loading={loading}
        error={error}
        deletingId={deletingId}
        onDelete={handleDelete}
      />
    </div>
  );
}
