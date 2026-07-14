"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import {
  Calendar,
  GraduationCap,
  Guitar,
  LayoutGrid,
  Library,
  ListTree,
  LogOut,
  Menu,
  MessageCircle,
  Settings,
  StickyNote,
  TriangleAlert,
  Users,
  X,
} from "lucide-react";
import { getAuthState, getSettings, logout } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ThemeToggle } from "@/components/theme-toggle";
import { LocaleToggle } from "@/components/locale-toggle";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { segment: "today", icon: Calendar },
  { segment: "students", icon: Users },
  { segment: "curricula", icon: GraduationCap },
  { segment: "library", icon: Library },
  { segment: "lessons", icon: ListTree },
  { segment: "notes", icon: StickyNote },
  { segment: "artifacts", icon: LayoutGrid },
  { segment: "chat", icon: MessageCircle },
] as const;

/** The cockpit's persistent chrome: a left nav rail (studio logo, the
 * section links, GR/EN + theme toggles) plus a slim top bar (current page
 * title, settings) over a scrollable content area. Every `(cockpit)/*` page
 * renders inside this via `(cockpit)/layout.tsx`.
 *
 * A client component (not the server `layout.tsx` itself) because active-
 * route highlighting and the current page title both need `usePathname()`,
 * and the mobile nav drawer needs local `useState` — see `library/
 * page.tsx`'s docstring for why client components are the norm here
 * whenever a page/shell needs the browser's own state or the API base
 * (CORS: the browser is the caller, not a Next.js server).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const t = useTranslations("nav");
  const tApp = useTranslations("app");
  const tSettings = useTranslations("settings");
  const locale = useLocale();
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // `null` while unknown — the banner must not flash on every page load and then
  // vanish. It appears only once the API has actually said "no key".
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [authEnabled, setAuthEnabled] = useState(false);

  useEffect(() => {
    // Both probes fail silently. Neither the banner nor the sign-out button is
    // worth a red error on a page whose real content loaded fine — and if the
    // API is unreachable, every page under this shell is already saying so.
    void getSettings()
      .then((s) => setConfigured(s.configured))
      .catch(() => {});
    void getAuthState()
      .then((a) => setAuthEnabled(a.auth_enabled))
      .catch(() => {});
  }, [pathname]);

  const segments = pathname.split("/").filter(Boolean);
  const activeSegment = segments[1] ?? "today";
  const pageTitle = t.has(activeSegment) ? t(activeSegment) : t("today");

  async function onSignOut() {
    try {
      await logout();
    } catch {
      // A failed logout still means "get me out of here" — the cookie is either
      // already gone or about to be rejected. Navigating is the honest response.
    }
    router.push(`/${locale}/login`);
    router.refresh();
  }

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
          <div className="flex items-center gap-1">
            {authEnabled && (
              <Button
                variant="ghost"
                size="icon"
                aria-label={t("signOut")}
                data-testid="sign-out"
                onClick={onSignOut}
              >
                <LogOut className="size-4" />
              </Button>
            )}
            <ThemeToggle />
          </div>
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
          {/* THE DISABLED SEARCH STUB IS GONE (Stage 8). It sat here, greyed out,
              promising "coming soon" on every screen of the app. Search now exists
              — over the hybrid index, on the Library page, where the books are
              (`components/library/library-search.tsx`) — and a dead box in the top
              bar of every OTHER page is worse than no box at all. Do not put it
              back as a global palette: the chat agent is already grounded search
              with citations, and the one thing the tutor could not do was find a
              chapter in his own book. That is a Library problem, so it lives in
              the Library. */}
          <Link
            href={`/${locale}/settings`}
            aria-label={t("settings")}
            aria-current={activeSegment === "settings" ? "page" : undefined}
            data-testid="nav-settings"
            className={cn(
              "ml-auto flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground sm:ml-2",
              activeSegment === "settings" && "bg-muted text-foreground",
            )}
          >
            <Settings className="size-4" />
          </Link>
        </header>

        {/* The app cannot write a single word without a key, and the tutor has no
            way to know that from any other screen — every button simply 409s. One
            banner, one link, on every page, until it is fixed. */}
        {configured === false && activeSegment !== "settings" && (
          <div
            data-testid="not-configured-banner"
            className="flex flex-wrap items-center gap-2 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm md:px-6"
          >
            <TriangleAlert className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span>{tSettings("notConfiguredBanner")}</span>
            <Link
              href={`/${locale}/settings`}
              className="font-medium underline underline-offset-4"
            >
              {tSettings("notConfiguredCta")}
            </Link>
          </div>
        )}

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-6xl p-4 md:p-8">{children}</div>
        </main>
      </div>
    </div>
  );
}
