"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { ArrowLeft, GraduationCap, Guitar, Languages } from "lucide-react";
import { AssignCurriculumDialog } from "@/components/students/assign-curriculum-dialog";
import { AssignmentsList } from "@/components/students/assignments-list";
import { LessonLogList } from "@/components/students/lesson-log-list";
import { LogLessonForm } from "@/components/students/log-lesson-form";
import { ProgressList } from "@/components/students/progress-list";
import {
  ApiError,
  getStudentDetail,
  type LessonLogOut,
  type ProgressOut,
  type StudentDetailOut,
} from "@/lib/api";

// Client component for the same reason as `students/page.tsx`/`curricula/
// page.tsx`: it talks to the API straight from the browser. A plain "use
// client" default-export page (not an async server component awaiting
// `params`, unlike e.g. `app/[locale]/layout.tsx`) — `useParams()` is the
// documented way a Client Component page reads its own dynamic segment
// without fighting the (now-Promise) `params` prop, and keeps this file
// shaped exactly like every other cockpit page.tsx (fetch/mutation state
// owned right here; `components/students/*` holds only presentational/
// dialog pieces, same split those files' own docstrings describe).
export default function StudentDetailPage() {
  const t = useTranslations("students.detail");
  const tStudents = useTranslations("students");
  const locale = useLocale();
  const params = useParams<{ id: string }>();
  const studentId = params.id;

  const [detail, setDetail] = useState<StudentDetailOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Same .then/.catch/.finally shape as e.g. `students/page.tsx`'s own
  // `fetchStudents`, for the same reason (every setState call stays
  // lexically inside a callback rather than a bare statement).
  const fetchDetail = useCallback(() => {
    return getStudentDetail(studentId)
      .then((data) => setDetail(data))
      .catch((err) => setError(err instanceof ApiError ? err.detail : t("error")))
      .finally(() => setLoading(false));
  }, [studentId, t]);

  useEffect(() => {
    fetchDetail();
  }, [fetchDetail]);

  // The one mutation whose full resulting shape ISN'T already at hand
  // (`assignCurriculum`'s response is the clone's tree, not an
  // `AssignmentSummary`) — a plain refetch, same "cheap follow-up" call
  // `curricula/page.tsx`'s `refreshTemplates()` makes for its own analogous
  // gap. See `AssignCurriculumDialog`'s own docstring.
  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    return fetchDetail();
  }, [fetchDetail]);

  // Progress/LessonLog mutations DO return the full row, so these splice
  // the response straight into local state instead of refetching — same
  // "optimistic append/replace from the mutation's own response" precedent
  // `curriculum/block-card.tsx`'s `handleArtifactAttached`/`handleSegmented`
  // already establish.
  function handleProgressChanged(updated: ProgressOut) {
    setDetail((prev) =>
      prev
        ? {
            ...prev,
            progress: prev.progress.some((p) => p.id === updated.id)
              ? prev.progress.map((p) => (p.id === updated.id ? updated : p))
              : [updated, ...prev.progress],
          }
        : prev,
    );
  }

  function handleLessonLogged(log: LessonLogOut) {
    setDetail((prev) => (prev ? { ...prev, recent_lessons: [log, ...prev.recent_lessons] } : prev));
  }

  return (
    <div className="flex flex-col gap-6">
      <Link
        href={`/${locale}/students`}
        data-testid="student-detail-back"
        className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        {t("back")}
      </Link>

      {loading && (
        <p className="text-sm text-muted-foreground" data-testid="student-detail-loading">
          {t("loading")}
        </p>
      )}
      {error && (
        <p role="alert" data-testid="student-detail-error" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {detail && (
        <>
          <div>
            <h1 className="text-2xl font-semibold" data-testid="student-detail-heading">
              {detail.student.name}
            </h1>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1.5 text-sm text-muted-foreground">
              <span className="flex items-center gap-1.5" data-testid="student-level">
                <GraduationCap className="size-3.5" />
                {detail.student.level ?? tStudents("unset")}
              </span>
              <span className="flex items-center gap-1.5" data-testid="student-instrument">
                <Guitar className="size-3.5" />
                {detail.student.instrument ?? tStudents("unset")}
              </span>
              <span className="flex items-center gap-1.5" data-testid="student-language">
                <Languages className="size-3.5" />
                {detail.student.preferred_language}
              </span>
            </div>
          </div>

          <section className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-medium text-muted-foreground" data-testid="student-assignments-heading">
                {t("assignments.heading")}
              </h2>
              <AssignCurriculumDialog studentId={studentId} onAssigned={refresh} />
            </div>
            <AssignmentsList assignments={detail.assignments} />
          </section>

          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-muted-foreground" data-testid="student-progress-heading">
              {t("progress.heading")}
            </h2>
            <ProgressList studentId={studentId} progress={detail.progress} onChanged={handleProgressChanged} />
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="text-sm font-medium text-muted-foreground" data-testid="student-lessons-heading">
              {t("lessons.heading")}
            </h2>
            <LessonLogList lessons={detail.recent_lessons} />
            <LogLessonForm studentId={studentId} onLogged={handleLessonLogged} />
          </section>
        </>
      )}
    </div>
  );
}
