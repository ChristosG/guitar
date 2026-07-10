"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { NoteForm } from "@/components/notes/note-form";
import { NoteList } from "@/components/notes/note-list";
import {
  ApiError,
  deleteNote,
  listNotes,
  listStudents,
  promoteNote,
  type NoteOut,
  type StudentOut,
} from "@/lib/api";

// Client component for the same reason as `knowledge/page.tsx`/`students/
// page.tsx`: it talks to the API straight from the browser (the app owns
// CORS specifically for this), which is also what makes it visible to
// Playwright's `page.route` interception.
export default function NotesPage() {
  const t = useTranslations("notes");

  const [notes, setNotes] = useState<NoteOut[]>([]);
  const [students, setStudents] = useState<StudentOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [promotingId, setPromotingId] = useState<string | null>(null);

  // Same .then/.catch/.finally shape as knowledge/page.tsx's fetchSources,
  // for the same reason: every setState call stays lexically inside a
  // callback rather than a bare statement, which is what react-hooks/
  // set-state-in-effect actually checks for.
  const fetchNotes = useCallback(() => {
    return listNotes()
      .then((data) => setNotes(data))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    fetchNotes();
    // Best-effort: the student picker just falls back to "no student"
    // options if this fails, so a broken /students call shouldn't also
    // block the notes list itself or feed this page's shared `error` state.
    listStudents()
      .then((data) => setStudents(data))
      .catch(() => setStudents([]));
  }, [fetchNotes]);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    return fetchNotes();
  }, [fetchNotes]);

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteNote(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("deleteError"));
    } finally {
      setDeletingId(null);
    }
  }

  async function handlePromote(id: string) {
    setPromotingId(id);
    setError(null);
    try {
      await promoteNote(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("promoteError"));
    } finally {
      setPromotingId(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="notes-heading">
          {t("heading")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      <NoteForm students={students} onSaved={refresh} />

      <NoteList
        notes={notes}
        students={students}
        loading={loading}
        error={error}
        deletingId={deletingId}
        promotingId={promotingId}
        onDelete={handleDelete}
        onPromote={handlePromote}
        onUpdated={refresh}
      />
    </div>
  );
}
