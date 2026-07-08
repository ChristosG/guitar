import { useTranslations } from "next-intl";
import { StickyNote } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

/** Stub page, same rationale as `today/page.tsx`: reachable and intentional-
 * looking, no real data model behind it yet. */
export default function NotesPage() {
  const t = useTranslations("notes");

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="notes-heading">
          {t("heading")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      <Card data-testid="notes-empty">
        <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
          <StickyNote className="size-8 text-muted-foreground" />
          <p className="font-medium">{t("emptyTitle")}</p>
          <p className="max-w-sm text-sm text-muted-foreground">{t("emptyBody")}</p>
        </CardContent>
      </Card>
    </div>
  );
}
