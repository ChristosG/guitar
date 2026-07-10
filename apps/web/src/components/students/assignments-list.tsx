"use client";

import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { LayoutGrid } from "lucide-react";
import type { AssignmentSummary } from "@/lib/api";

interface AssignmentsListProps {
  assignments: AssignmentSummary[];
}

/** Lists a student's assigned curricula: title + a link to the Curricula
 * board. Links to the general `/curricula` page (not a deep link to this
 * specific template) — `AssignmentSummary.curriculum_block_id` is the
 * TEMPLATE's id (see `lib/api.ts`'s docstring), and that template is always
 * listed there (`GET /curricula` only ever omits non-template/non-root
 * blocks), so "open Curricula and pick it from the list" is a real, working
 * path even without a query-string deep link. */
export function AssignmentsList({ assignments }: AssignmentsListProps) {
  const t = useTranslations("students.detail.assignments");
  const locale = useLocale();

  if (assignments.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="student-assignments-empty">
        {t("empty")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="assignment-list">
      {assignments.map((a) => (
        <div
          key={a.assignment_id}
          data-testid="assignment-item"
          className="flex items-center justify-between gap-2 rounded-xl border border-border bg-card p-3 text-sm ring-1 ring-foreground/10"
        >
          <span className="font-medium" data-testid="assignment-title">
            {a.title}
          </span>
          <Link
            href={`/${locale}/curricula`}
            data-testid="assignment-view-link"
            className="flex items-center gap-1.5 text-xs font-medium text-primary hover:underline"
          >
            <LayoutGrid className="size-3.5" />
            {t("viewBoard")}
          </Link>
        </div>
      ))}
    </div>
  );
}
