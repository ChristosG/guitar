"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { Eye, EyeOff, Guitar, Loader2 } from "lucide-react";

import { login } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LocaleToggle } from "@/components/locale-toggle";
import { ThemeToggle } from "@/components/theme-toggle";

/** The first screen the tutor ever sees, and the only one he sees before he is
 * allowed anywhere. It lives OUTSIDE the `(cockpit)` route group deliberately:
 * the AppShell's nav rail links to eight pages he cannot open yet, and painting
 * a full cockpit around a locked door is a worse experience than a door.
 *
 * ONE FIELD, ONE SENTENCE ON FAILURE. There is no username (there is one user),
 * no "remember me" (the cookie is fourteen days), no password strength meter, no
 * account recovery. A wrong password produces exactly one calm line of Greek —
 * never a status code, never "401", never the word "unauthorized". `login()` in
 * `lib/api.ts` returns `{authenticated: false}` rather than throwing, precisely so
 * this component has no error object to accidentally render.
 */
export default function LoginPage() {
  const t = useTranslations("login");
  const locale = useLocale();
  const router = useRouter();

  const [password, setPassword] = useState("");
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<"wrongPassword" | "networkError" | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const state = await login(password);
      if (!state.authenticated) {
        setError("wrongPassword");
        setPassword("");
        return;
      }
      // Read at click time from `window.location` rather than `useSearchParams()`
      // — the hook forces this page under a Suspense boundary during static
      // generation, for a value nothing renders.
      //
      // `next` comes from `proxy.ts`/the 401 interceptor, so it is always an
      // in-app path — but it arrives through the query string, and a query string
      // is user input. Anything that isn't a local path is discarded rather than
      // followed (an open redirect on a login page is the classic way to make a
      // phishing link look legitimate).
      const next = new URLSearchParams(window.location.search).get("next");
      const dest = next?.startsWith("/") && !next.startsWith("//") ? next : `/${locale}/today`;
      router.push(dest);
      router.refresh();
    } catch {
      setError("networkError");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh flex-col bg-background text-foreground">
      <div className="flex items-center justify-end gap-2 p-4">
        <LocaleToggle />
        <ThemeToggle />
      </div>

      <main className="flex flex-1 items-center justify-center px-4 pb-24">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex flex-col items-center gap-3 text-center">
            <span className="flex size-12 items-center justify-center rounded-2xl bg-primary text-primary-foreground">
              <Guitar className="size-6" />
            </span>
            <h1 className="font-heading text-xl font-semibold">{t("title")}</h1>
            <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
          </div>

          <form onSubmit={onSubmit} className="flex flex-col gap-3" data-testid="login-form">
            <Label htmlFor="password" className="text-sm font-medium">
              {t("passwordLabel")}
            </Label>
            <div className="relative">
              <Input
                id="password"
                name="password"
                data-testid="login-password"
                type={reveal ? "text" : "password"}
                autoFocus
                autoComplete="current-password"
                placeholder={t("passwordPlaceholder")}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={error === "wrongPassword" || undefined}
                className="h-10 pr-10"
              />
              <button
                type="button"
                onClick={() => setReveal((v) => !v)}
                aria-label={reveal ? t("hide") : t("show")}
                className="absolute top-1/2 right-2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                {reveal ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>

            {error && (
              <p
                role="alert"
                data-testid="login-error"
                className="text-sm text-destructive"
              >
                {t(error)}
              </p>
            )}

            <Button
              type="submit"
              size="lg"
              disabled={busy || password.length === 0}
              data-testid="login-submit"
              className="mt-2 h-10 w-full"
            >
              {busy && <Loader2 className="size-4 animate-spin" />}
              {busy ? t("submitting") : t("submit")}
            </Button>
          </form>
        </div>
      </main>
    </div>
  );
}
