import { redirect } from "next/navigation";

/** The cockpit has no real "home" — `/[locale]` just lands you on
 * Curricula, the app's primary screen. In normal operation `proxy.ts`'s own
 * `localeRootRedirect` intercepts this exact path first (see its docstring
 * for why: a `no-store`-correctness fix, not a style choice) and this
 * component never actually runs — it's kept anyway as the literal route
 * Next.js expects here and as a correctness fallback for any request that
 * ever reaches this segment without going through that middleware step. */
export default async function RootLocalePage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  redirect(`/${locale}/curricula`);
}
