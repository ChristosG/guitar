import { useTranslations } from "next-intl";
import { ThemeToggle } from "@/components/theme-toggle";

export default function Home() {
  const t = useTranslations("app");
  return (
    <main className="min-h-screen flex flex-col items-center justify-center gap-6">
      <h1 data-testid="app-title" className="text-3xl font-semibold">
        {t("title")} 🎸
      </h1>
      <ThemeToggle />
    </main>
  );
}
