"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AddStudentDialog } from "@/components/students/add-student-dialog";
import { StudentCard } from "@/components/students/student-card";
import { ApiError, deleteStudent, listStudents, type StudentOut } from "@/lib/api";

// Client component for the same reason as `knowledge/page.tsx`: it talks to
// the API straight from the browser (the app owns CORS specifically for
// this), which is also what makes it visible to Playwright's `page.route`.
export default function StudentsPage() {
  const t = useTranslations("students");

  const [students, setStudents] = useState<StudentOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Same .then/.catch/.finally shape as knowledge/page.tsx's fetchSources,
  // for the same reason: every setState call stays lexically inside a
  // callback rather than a bare statement, which is what react-hooks/
  // set-state-in-effect actually checks for.
  const fetchStudents = useCallback(() => {
    return listStudents()
      .then((data) => setStudents(data))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    fetchStudents();
  }, [fetchStudents]);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    return fetchStudents();
  }, [fetchStudents]);

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteStudent(id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("deleteError"));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold" data-testid="students-heading">
            {t("heading")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subheading")}</p>
        </div>
        <AddStudentDialog onAdded={refresh} />
      </div>

      {loading && <p className="text-sm text-muted-foreground">{t("loading")}</p>}
      {error && (
        <p role="alert" data-testid="students-error" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {!loading && !error && students.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="students-empty">
          {t("empty")}
        </p>
      )}

      {students.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="student-list">
          {students.map((student) => (
            <StudentCard
              key={student.id}
              student={student}
              deleting={deletingId === student.id}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}
