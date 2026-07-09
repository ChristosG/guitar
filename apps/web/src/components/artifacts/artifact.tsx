import { useTranslations } from "next-intl";
import { Card, CardContent } from "@/components/ui/card";
import { AmpDials } from "./amp-dials";
import { ChordDiagram } from "./chord-diagram";
import { ScaleDiagram } from "./scale-diagram";
import { SignalChain } from "./signal-chain";
import { TabView } from "./tab-view";
import { ToneRecipeCard } from "./tone-recipe-card";
import type { AmpSettingsSpec, ChordDiagramSpec, ScaleDiagramSpec, SignalChainSpec, TabSpec, ToneRecipeSpec } from "./types";

function UnsupportedArtifact({ kind }: { kind: string }) {
  const t = useTranslations("artifacts.unsupported");
  return (
    <Card data-testid="artifact-unsupported" className="w-fit border-dashed">
      <CardContent className="text-sm text-muted-foreground">
        <p className="font-medium text-foreground">{t("title")}</p>
        <p>{t("body", { kind })}</p>
      </CardContent>
    </Card>
  );
}

/**
 * `Artifact({kind, spec})`: switches on `kind` to the matching deterministic
 * renderer. `spec` is `unknown` here rather than a discriminated union on
 * `kind` — per the architecture (see specs.py's module docstring), a spec
 * reaching this component has already passed server-side `validate_spec`,
 * so each case below trusts the cast instead of re-validating; that keeps
 * this the single seam a caller (the demo page today, the gallery/segment
 * card in later tasks) needs, without forcing every caller to know which
 * per-kind type to import. An unrecognized-but-otherwise-valid kind (e.g.
 * "gear_card", not yet rendered — see types.ts) falls through to a graceful
 * placeholder instead of throwing, since new kinds are expected to reach the
 * client before their renderer lands.
 */
export function Artifact({ kind, spec }: { kind: string; spec: unknown }) {
  switch (kind) {
    case "chord_diagram":
      return <ChordDiagram spec={spec as ChordDiagramSpec} />;
    case "scale_diagram":
      return <ScaleDiagram spec={spec as ScaleDiagramSpec} />;
    case "tab":
      return <TabView spec={spec as TabSpec} />;
    case "tone_recipe":
      return <ToneRecipeCard spec={spec as ToneRecipeSpec} />;
    case "signal_chain":
      return <SignalChain spec={spec as SignalChainSpec} />;
    case "amp_settings":
      return <AmpDials spec={spec as AmpSettingsSpec} />;
    default:
      return <UnsupportedArtifact kind={kind} />;
  }
}
