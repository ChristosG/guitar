"use client";

import { useTranslations } from "next-intl";
import { StickyNote } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { NoteCard } from "@/components/notes/note-card";
import type { NoteOut, StudentOut } from "@/lib/api";

interface NoteListProps {
  notes: NoteOut[];
  students: StudentOut[];
  loading: boolean;
  error: string | null;
  deletingId: string | null;
  promotingId: string | null;
  onDelete: (id: string) => void;
  onPromote: (id: string) => void;
  onUpdated: (note: NoteOut) => void;
}

/** Renders the notes list — loading/error/empty states, else one `NoteCard`
 * per note. Pure presentational component: all fetching/mutation lives in
 * the parent page, same split as `components/knowledge/source-list.tsx`.
 * The empty state reuses the original stub page's exact markup/copy (the
 * `notes-empty` testid + `notes.emptyTitle`/`emptyBody` keys predate this
 * task and are kept as-is) so a freshly-seeded cockpit looks unchanged. */
export function NoteList({
  notes,
  students,
  loading,
  error,
  deletingId,
  promotingId,
  onDelete,
  onPromote,
  onUpdated,
}: NoteListProps) {
  const t = useTranslations("notes");

  if (loading) {
    return <p className="text-sm text-muted-foreground">{t("loading")}</p>;
  }

  if (error) {
    return (
      <p role="alert" data-testid="notes-error" className="text-sm text-destructive">
        {error}
      </p>
    );
  }

  if (notes.length === 0) {
    return (
      <Card data-testid="notes-empty">
        <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
          <StickyNote className="size-8 text-muted-foreground" />
          <p className="font-medium">{t("emptyTitle")}</p>
          <p className="max-w-sm text-sm text-muted-foreground">{t("emptyBody")}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="note-list">
      {notes.map((note) => (
        <NoteCard
          key={note.id}
          note={note}
          students={students}
          deleting={deletingId === note.id}
          promoting={promotingId === note.id}
          onDelete={onDelete}
          onPromote={onPromote}
          onUpdated={onUpdated}
        />
      ))}
    </div>
  );
}
