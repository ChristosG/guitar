import type { ComponentType } from "react";
import { useTranslations } from "next-intl";
import { Cable, Flame, Guitar as GuitarIcon, Hand, Headphones, Volume2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import type { ToneRecipeSpec } from "./types";

function Row({
  icon: Icon,
  label,
  value,
  testId,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  testId: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{label}</span>
        <span data-testid={testId} className="text-sm leading-snug text-foreground">
          {value}
        </span>
      </div>
    </div>
  );
}

/** The "money artifact": a styled tone-recipe card — artist/song header,
 * then labeled Guitar/Amp/Drive/Chain/Hands rows, then a Listen list.
 * `drive`/`hands` and the whole Listen footer are omitted (not shown empty)
 * when the spec doesn't have them, since only guitar/amp/chain are
 * required (see types.ts). */
export function ToneRecipeCard({ spec }: { spec: ToneRecipeSpec }) {
  const t = useTranslations("artifacts.toneRecipe");
  const title = spec.song ?? spec.artist ?? t("fallbackTitle");

  return (
    <Card data-testid="tone-recipe-card" className="w-full max-w-xl">
      <CardHeader className="gap-1.5 border-b pb-4">
        {spec.artist && spec.song && (
          <CardDescription
            data-testid="tone-recipe-artist"
            className="text-xs font-semibold tracking-wide text-muted-foreground uppercase"
          >
            {spec.artist}
          </CardDescription>
        )}
        <CardTitle data-testid="tone-recipe-title" className="text-xl">
          {title}
        </CardTitle>
      </CardHeader>

      <CardContent className="flex flex-col gap-3.5 pt-4">
        <Row icon={GuitarIcon} label={t("guitar")} value={spec.guitar} testId="tone-recipe-guitar" />
        <Row icon={Volume2} label={t("amp")} value={spec.amp} testId="tone-recipe-amp" />
        {spec.drive && <Row icon={Flame} label={t("drive")} value={spec.drive} testId="tone-recipe-drive" />}
        <Row icon={Cable} label={t("chain")} value={spec.chain} testId="tone-recipe-chain" />
        {spec.hands && <Row icon={Hand} label={t("hands")} value={spec.hands} testId="tone-recipe-hands" />}
      </CardContent>

      {spec.listen.length > 0 && (
        <CardFooter className="flex-col items-start gap-2 pb-4!">
          <span className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            <Headphones className="size-3.5" />
            {t("listen")}
          </span>
          <ul data-testid="tone-recipe-listen" className="flex flex-wrap gap-1.5">
            {spec.listen.map((item) => (
              <li key={item}>
                <Badge variant="secondary">{item}</Badge>
              </li>
            ))}
          </ul>
        </CardFooter>
      )}
    </Card>
  );
}
