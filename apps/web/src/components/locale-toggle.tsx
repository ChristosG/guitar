"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { routing } from "@/i18n/routing";

/** GR/EN switcher: swaps the leading `/en`/`/el` path segment and keeps the
 * rest of the URL (so switching language on `/en/library` lands on
 * `/el/library`, not the home page). Plain `next/link` + manual locale
 * interpolation, same convention as the rest of this app (see
 * `app/[locale]/page.tsx`/`library/page.tsx`) rather than next-intl's
 * `createNavigation` wrapper, which this codebase doesn't otherwise use. */
export function LocaleToggle() {
  const locale = useLocale();
  const pathname = usePathname() ?? "/";
  const t = useTranslations("locale");
  const rest = pathname.replace(/^\/(en|el)(?=\/|$)/, "") || "/";

  return (
    <div className="flex items-center gap-1" role="group" aria-label={t("switchTo")} data-testid="locale-toggle">
      {routing.locales.map((l) => (
        <Link
          key={l}
          href={`/${l}${rest}`}
          aria-current={locale === l ? "true" : undefined}
          data-testid={`locale-toggle-${l}`}
          className={cn(
            buttonVariants({ variant: locale === l ? "default" : "outline", size: "xs" }),
          )}
        >
          {t(l)}
        </Link>
      ))}
    </div>
  );
}
