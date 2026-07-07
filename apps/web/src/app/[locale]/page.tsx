import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { ThemeToggle } from "@/components/theme-toggle";

export default function Home() {
  const t = useTranslations("app");
  const locale = useLocale();
  return (
    <main className="min-h-screen flex flex-col items-center justify-center gap-6">
      <h1 data-testid="app-title" className="text-3xl font-semibold">
        {t("title")} 🎸
      </h1>
      <Link href={`/${locale}/knowledge`} data-testid="knowledge-nav-link" className="text-sm underline">
        {t("knowledgeNav")}
      </Link>
      <ThemeToggle />
    </main>
  );
}
