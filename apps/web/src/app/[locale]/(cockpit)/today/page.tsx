import { useTranslations } from "next-intl";
import { Calendar } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** A plain Server Component: purely static copy, no client state or API
 * calls, so it stays server-rendered (`useTranslations` from "next-intl"
 * works here too — see the package's `react-server` entry point — unlike
 * the Knowledge/Students/Curricula pages, which are client components
 * because they call the API straight from the browser). Stub for now: the
 * real lesson-prep flow (scheduling, per-student plan) is a later plan;
 * this just proves the route is reachable and looks intentional. */
export default function TodayPage() {
  const t = useTranslations("today");

  const placeholders = [t("card1Title"), t("card2Title")];

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="today-heading">
          {t("heading")}
        </h1>
        <p className="text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {placeholders.map((title) => (
          <Card key={title} data-testid="today-placeholder-card">
            <CardHeader>
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="flex items-center gap-2">
                  <Calendar className="size-4 text-muted-foreground" />
                  {title}
                </CardTitle>
                <Badge variant="secondary">{t("previewBadge")}</Badge>
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-1 text-sm text-muted-foreground">
              <span>{t("placeholderStudent")}</span>
              <span>{t("placeholderTime")}</span>
              <p className="mt-2">{t("placeholderNote")}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
