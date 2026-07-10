"use client";

import { useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { StudentSelect } from "@/components/notes/student-select";
import { ApiError, createNote, updateNote, type NoteOut, type StudentOut } from "@/lib/api";

function tagsToText(tags: string[]): string {
  return tags.join(", ");
}

/** Comma-separated free text -> a clean `string[]` (trims each entry, drops
 * empties from stray/trailing commas) — the simplest tag input this app's
 * design system supports without a dedicated tag-chip widget. */
function textToTags(text: string): string[] {
  return text
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

interface NoteFormProps {
  students: StudentOut[];
  /** Present -> edit this note (PATCH, prefilled); absent -> create a new
   * one (POST, starts blank and resets after success). */
  note?: NoteOut;
  onSaved: (note: NoteOut) => void;
  /** Only rendered/used in edit mode. */
  onCancel?: () => void;
}

/** One form for both creating and editing a note — mirrors `components/
 * knowledge/add-source.tsx`'s always-visible-Card convention for create;
 * `note-card.tsx` also mounts this same component (with `note` set) in
 * place of its normal view when a card is put into edit mode, rather than
 * duplicating title/body/tags/student fields a second time.
 *
 * The student picker is optional in the product sense (a note need not be
 * about one student) — never skipped/omitted here, since `listStudents()`
 * is already a one-line fetch and `StudentSelect` needed no new design-
 * system primitive to build (see that component's own docstring). */
export function NoteForm({ students, note, onSaved, onCancel }: NoteFormProps) {
  const t = useTranslations("notes.form");
  const isEdit = note != null;

  const [title, setTitle] = useState(note?.title ?? "");
  const [body, setBody] = useState(note?.body ?? "");
  const [tagsText, setTagsText] = useState(note ? tagsToText(note.tags) : "");
  const [studentId, setStudentId] = useState(note?.student_id ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setTitle("");
    setBody("");
    setTagsText("");
    setStudentId("");
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const payload = {
        title,
        body,
        tags: textToTags(tagsText),
        student_id: studentId || null,
      };
      // Branches on `note` itself (not the `isEdit` bool derived from it)
      // so TS narrows `note` to `NoteOut` inside the `updateNote` call.
      const saved = note ? await updateNote(note.id, payload) : await createNote(payload);
      if (!note) reset();
      onSaved(saved);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t(isEdit ? "editError" : "error"));
    } finally {
      setSubmitting(false);
    }
  }

  // Keeps every <Label htmlFor>/<Input id> pair unique even when a create
  // form and one or more per-card edit forms are all mounted at once.
  // Branches on `note` itself, same narrowing reason as `handleSubmit` above.
  const idPrefix = note ? `note-edit-${note.id}` : "note-create";

  return (
    <Card data-testid={isEdit ? "note-edit-form" : "note-create-form"}>
      <CardHeader>
        <CardTitle>{t(isEdit ? "editHeading" : "heading")}</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <fieldset disabled={submitting} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${idPrefix}-title`}>{t("titleLabel")}</Label>
              <Input
                id={`${idPrefix}-title`}
                data-testid="note-title-input"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={t("titlePlaceholder")}
                required
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${idPrefix}-body`}>{t("bodyLabel")}</Label>
              <Textarea
                id={`${idPrefix}-body`}
                data-testid="note-body-input"
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder={t("bodyPlaceholder")}
                rows={4}
                required
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${idPrefix}-tags`}>{t("tagsLabel")}</Label>
                <Input
                  id={`${idPrefix}-tags`}
                  data-testid="note-tags-input"
                  value={tagsText}
                  onChange={(e) => setTagsText(e.target.value)}
                  placeholder={t("tagsPlaceholder")}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`${idPrefix}-student`}>{t("studentLabel")}</Label>
                <StudentSelect
                  id={`${idPrefix}-student`}
                  data-testid="note-student-select"
                  students={students}
                  value={studentId ?? ""}
                  onChange={setStudentId}
                />
              </div>
            </div>
          </fieldset>

          {error && (
            <p role="alert" data-testid="note-form-error" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <div className="flex gap-2">
            <Button type="submit" disabled={submitting} data-testid="note-form-submit" className="self-start">
              {submitting ? t("submitting") : t(isEdit ? "save" : "submit")}
            </Button>
            {isEdit && (
              <Button
                type="button"
                variant="outline"
                disabled={submitting}
                data-testid="note-form-cancel"
                onClick={onCancel}
              >
                {t("cancel")}
              </Button>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
