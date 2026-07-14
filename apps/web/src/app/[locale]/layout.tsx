import { NextIntlClientProvider } from "next-intl";
import { getMessages } from "next-intl/server";
import { ThemeProvider } from "@/components/theme-provider";
import { ConfirmProvider } from "@/components/ui/confirm";
import "../globals.css";

export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  const messages = await getMessages();
  return (
    <html lang={locale} suppressHydrationWarning>
      <body>
        <ThemeProvider>
          {/* ConfirmProvider sits INSIDE NextIntlClientProvider (it reads the
              `confirm.*` messages for its default Cancel/Delete labels) and
              wraps every route, cockpit or not — a destructive action is not a
              cockpit-only concept, and mounting it at the route-group level
              would have left `/login` and any future page outside it silently
              un-guarded. */}
          <NextIntlClientProvider messages={messages}>
            <ConfirmProvider>{children}</ConfirmProvider>
          </NextIntlClientProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
