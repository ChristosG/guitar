"use client";

import { useTranslations } from "next-intl";
import type { StudentOut } from "@/lib/api";

interface StudentSelectProps {
  id: string;
  students: StudentOut[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  "data-testid"?: string;
}

/** A native `<select>` for "which student (if any) is this note about" —
 * this app has no separate Select/combobox primitive (see `components/
 * artifacts/generate-form.tsx`'s docstring), which is fine for the 7-member
 * artifact-kind picker's button-toggle-row treatment, but not for a
 * potentially-large, open-ended roster, so this is a plain styled native
 * element instead (classes lifted from `ui/input.tsx` for a consistent
 * look) rather than a bespoke combobox. First option is always "no student"
 * (`value=""`), matching `NoteCreateInput.student_id`'s `null` — this
 * component only ever hands its parent a plain string, so callers convert
 * `""` to `null` themselves at submit time (`note-form.tsx`). */
export function StudentSelect({ id, students, value, onChange, disabled, ...rest }: StudentSelectProps) {
  const t = useTranslations("notes.form");

  return (
    <select
      id={id}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className="h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 text-base outline-none transition-colors focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm dark:bg-input/30"
      {...rest}
    >
      <option value="">{t("studentUnassigned")}</option>
      {students.map((student) => (
        <option key={student.id} value={student.id}>
          {student.name}
        </option>
      ))}
    </select>
  );
}
