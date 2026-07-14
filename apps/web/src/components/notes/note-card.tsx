"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Library, Loader2, Pencil, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm";
import { NoteForm } from "@/components/notes/note-form";
import type { NoteOut, StudentOut } from "@/lib/api";

interface NoteCardProps {
  note: NoteOut;
  students: StudentOut[];
  deleting: boolean;
  promoting: boolean;
  onDelete: (id: string) => void;
  onPromote: (id: string) => void;
  onUpdated: (note: NoteOut) => void;
}

/** One note: title/body/tags/linked-student, and edit/delete/promote
 * actions. Pure presentational + its own local `editing` toggle — mutation
 * *requests* still live in the parent page (delete/promote), same split as
 * `components/knowledge/source-list.tsx`; edit is the one exception,
 * self-contained here (like `curriculum/block-card.tsx`'s inline title
 * edit) since only this card needs to know it's mid-edit.
 *
 * Promote is one-way: once `promoted_to_knowledge` is true the action is
 * replaced by a static badge, never re-shown — mirrors the API's own
 * refuse-a-second-promote posture (`routers/notes.py`, 409) by making a
 * second call unreachable from this UI rather than merely disabled. */
export function NoteCard({ note, students, deleting, promoting, onDelete, onPromote, onUpdated }: NoteCardProps) {
  const t = useTranslations("notes");
  const confirm = useConfirm();
  const [editing, setEditing] = useState(false);

  // A promoted note has a COPY in the library that `DELETE /notes/{id}` does
  // not touch (`routers/notes.py` deletes the Note row only). Saying so is
  // the difference between "I'm deleting a duplicate" and "I'm deleting the
  // only copy" — two different decisions, one button.
  async function requestDelete() {
    const ok = await confirm({
      title: t("confirmDelete.title", { title: note.title }),
      body: note.promoted_to_knowledge
        ? `${t("confirmDelete.body")} ${t("confirmDelete.promotedNote")}`
        : t("confirmDelete.body"),
      confirmLabel: t("confirmDelete.confirm"),
      destructive: true,
    });
    if (ok) onDelete(note.id);
  }

  if (editing) {
    return (
      <NoteForm
        students={students}
        note={note}
        onSaved={(updated) => {
          setEditing(false);
          onUpdated(updated);
        }}
        onCancel={() => setEditing(false)}
      />
    );
  }

  const student = note.student_id ? students.find((s) => s.id === note.student_id) : undefined;

  return (
    <Card data-testid="note-item" data-note-id={note.id}>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle data-testid="note-title">{note.title}</CardTitle>
          <div className="flex items-center gap-1.5">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              data-testid="note-edit"
              onClick={() => setEditing(true)}
              aria-label={t("edit")}
            >
              <Pencil />
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="icon-sm"
              data-testid="note-delete"
              disabled={deleting}
              onClick={requestDelete}
              aria-label={t("delete")}
            >
              {deleting ? <Loader2 className="animate-spin" /> : <Trash2 />}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p data-testid="note-body" className="text-sm whitespace-pre-wrap text-muted-foreground">
          {note.body}
        </p>

        {(note.tags.length > 0 || student) && (
          <div className="flex flex-wrap items-center gap-1.5">
            {note.tags.map((tag, i) => (
              <Badge key={`${tag}-${i}`} variant="secondary" data-testid="note-tag">
                {tag}
              </Badge>
            ))}
            {student && (
              <Badge variant="outline" data-testid="note-student">
                {student.name}
              </Badge>
            )}
          </div>
        )}

        <div>
          {note.promoted_to_knowledge ? (
            <Badge data-testid="note-promoted-badge">
              <Library className="size-3" />
              {t("promoted")}
            </Badge>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              data-testid="note-promote"
              disabled={promoting}
              onClick={() => onPromote(note.id)}
            >
              {promoting ? <Loader2 className="animate-spin" /> : <Library />}
              {promoting ? t("promoting") : t("promote")}
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
