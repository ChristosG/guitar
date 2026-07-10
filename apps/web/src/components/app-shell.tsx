"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import {
  Calendar,
  GraduationCap,
  Guitar,
  LayoutGrid,
  Library,
  Menu,
  MessageCircle,
  Search,
  StickyNote,
  Users,
  X,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { ThemeToggle } from "@/components/theme-toggle";
import { LocaleToggle } from "@/components/locale-toggle";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { segment: "today", icon: Calendar },
  { segment: "students", icon: Users },
  { segment: "curricula", icon: GraduationCap },
  { segment: "knowledge", icon: Library },
  { segment: "notes", icon: StickyNote },
  { segment: "artifacts", icon: LayoutGrid },
  { segment: "chat", icon: MessageCircle },
] as const;

/** The cockpit's persistent chrome: a left nav rail (studio logo, the 5
 * section links, GR/EN + theme toggles) plus a slim top bar (current page
 * title, a disabled search stub) over a scrollable content area. Every
 * `(cockpit)/*` page renders inside this via `(cockpit)/layout.tsx`.
 *
 * A client component (not the server `layout.tsx` itself) because active-
 * route highlighting and the current page title both need `usePathname()`,
 * and the mobile nav drawer needs local `useState` — see `knowledge/
 * page.tsx`'s docstring for why client components are the norm here
 * whenever a page/shell needs the browser's own state or the API base
 * (CORS: the browser is the caller, not a Next.js server).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const t = useTranslations("nav");
  const tApp = useTranslations("app");
  const locale = useLocale();
  const pathname = usePathname() ?? "/";
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const segments = pathname.split("/").filter(Boolean);
  const activeSegment = segments[1] ?? "today";
  const pageTitle = t.has(activeSegment) ? t(activeSegment) : t("today");

  const nav = (
    <nav className="flex flex-1 flex-col gap-0.5 px-3" data-testid="app-nav">
      {NAV_ITEMS.map(({ segment, icon: Icon }) => {
        const active = segment === activeSegment;
        return (
          <Link
            key={segment}
            href={`/${locale}/${segment}`}
            aria-current={active ? "page" : undefined}
            data-testid={`nav-${segment}`}
            onClick={() => setMobileNavOpen(false)}
            className={cn(
              "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium text-sidebar-foreground/70 transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
              active && "bg-sidebar-accent text-sidebar-accent-foreground",
            )}
          >
            <Icon className="size-4 shrink-0" />
            {t(segment)}
          </Link>
        );
      })}
    </nav>
  );

  return (
    <div className="flex h-dvh overflow-hidden bg-background text-foreground">
      {/* Mobile backdrop: only mounted while the drawer is open. */}
      {mobileNavOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/30 md:hidden"
          onClick={() => setMobileNavOpen(false)}
          data-testid="mobile-nav-backdrop"
        />
      )}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-transform duration-150 md:static md:z-auto md:translate-x-0",
          mobileNavOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-2 px-4 py-4">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Guitar className="size-4" />
          </span>
          <span data-testid="app-title" className="truncate font-heading text-sm font-semibold">
            {tApp("title")}
          </span>
          <button
            type="button"
            aria-label={t("closeMenu")}
            data-testid="mobile-nav-close"
            className="ml-auto text-sidebar-foreground/70 md:hidden"
            onClick={() => setMobileNavOpen(false)}
          >
            <X className="size-4" />
          </button>
        </div>

        {nav}

        <div className="flex items-center justify-between gap-2 border-t border-sidebar-border px-3 py-3">
          <LocaleToggle />
          <ThemeToggle />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-4 md:px-6">
          <button
            type="button"
            aria-label={t("openMenu")}
            data-testid="mobile-nav-open"
            className="text-muted-foreground md:hidden"
            onClick={() => setMobileNavOpen(true)}
          >
            <Menu className="size-5" />
          </button>
          <h1 data-testid="page-title" className="truncate text-sm font-semibold">
            {pageTitle}
          </h1>
          {/* Hidden below `sm`: on a narrow phone viewport there isn't room
              for a hamburger + title + this stub without squeezing the
              title down to a couple of letters — the title matters more
              than a disabled search box there. */}
          <div className="relative ml-auto hidden w-full max-w-xs sm:block">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              disabled
              placeholder={t("searchPlaceholder")}
              data-testid="topbar-search"
              className="pl-8"
            />
          </div>
        </header>

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-6xl p-4 md:p-8">{children}</div>
        </main>
      </div>
    </div>
  );
}
