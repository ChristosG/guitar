"use client";

import { useTranslations } from "next-intl";
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
 * parent page, same split as `components/knowledge/source-list.tsx`. */
export function StudentCard({ student, deleting, onDelete }: StudentCardProps) {
  const t = useTranslations("students");

  return (
    <Card data-testid="student-item">
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <CardTitle data-testid="student-name">{student.name}</CardTitle>
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
