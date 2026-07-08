import type { ReactNode } from "react";
import { AppShell } from "@/components/app-shell";

/** Wraps every cockpit page (Today/Students/Curricula/Knowledge/Notes) in
 * the persistent nav shell. A route group (`(cockpit)`, parens excluded
 * from the URL) rather than putting `<AppShell>` in `app/[locale]/layout.tsx`
 * itself, so the bare `/[locale]` redirect in `page.tsx` stays outside the
 * shell — there's nothing to show chrome around while that redirect fires. */
export default function CockpitLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
