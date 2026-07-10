"use client";

import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { GraduationCap, Guitar, Languages, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { StudentOut } from "@/lib/api";

interface StudentCardProps {
  student: StudentOut;
  deleting: boolean;
  onDelete: (id: string) => void;
}

/** One roster entry: name + level/instrument/language, and a delete action.
 * Pure presentational component — fetching/mutation state lives in the
 * parent page, same split as `components/knowledge/source-list.tsx`. The
 * name links to `/students/{id}` (Plan 6 Task 4's student-detail page) —
 * just the name text, not the whole card, so the delete button (a real
 * nested `<button>`) never ends up inside an `<a>`. */
export function StudentCard({ student, deleting, onDelete }: StudentCardProps) {
  const t = useTranslations("students");
  const locale = useLocale();

  return (
    <Card data-testid="student-item">
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle>
            <Link
              href={`/${locale}/students/${student.id}`}
              data-testid="student-name"
              className="hover:underline"
            >
              {student.name}
            </Link>
          </CardTitle>
          <Button
            type="button"
            variant="destructive"
            size="icon-sm"
            data-testid="student-delete"
            disabled={deleting}
            onClick={() => onDelete(student.id)}
            aria-label={t("delete")}
          >
            <Trash2 />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-x-4 gap-y-1.5 text-sm text-muted-foreground">
        <span className="flex items-center gap-1.5" data-testid="student-level">
          <GraduationCap className="size-3.5" />
          {student.level ?? t("unset")}
        </span>
        <span className="flex items-center gap-1.5" data-testid="student-instrument">
          <Guitar className="size-3.5" />
          {student.instrument ?? t("unset")}
        </span>
        <span className="flex items-center gap-1.5" data-testid="student-language">
          <Languages className="size-3.5" />
          {student.preferred_language}
        </span>
      </CardContent>
    </Card>
  );
}
