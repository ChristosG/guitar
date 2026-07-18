"use client";

import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { LayoutGrid } from "lucide-react";
import type { AssignmentSummary } from "@/lib/api";

interface AssignmentsListProps {
  assignments: AssignmentSummary[];
}

/** Lists a student's assigned curricula: title + a link straight to that
 * curriculum's board. `AssignmentSummary.curriculum_block_id` is the
 * TEMPLATE's id (see `lib/api.ts`'s docstring), and Unit A gave every
 * template its own detail route (`/curricula/[rootId]`) — so this can link
 * directly to it instead of sending the tutor to the index to pick it out of
 * the list by hand. */
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
            href={`/${locale}/curricula/${a.curriculum_block_id}`}
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
