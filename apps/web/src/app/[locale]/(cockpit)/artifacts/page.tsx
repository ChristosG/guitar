import { useTranslations } from "next-intl";
import { Artifact } from "@/components/artifacts/artifact";
import type {
  AmpSettingsSpec,
  ChordDiagramSpec,
  ScaleDiagramSpec,
  SignalChainSpec,
  ToneRecipeSpec,
} from "@/components/artifacts/types";

// Fixed, in-file demo specs — this route is a temporary preview harness for
// Plan 4 Task 2's renderers (`components/artifacts/*`); a later task wires
// real generation (LLM `guided_json` -> validate_spec -> persisted
// Artifact) and a proper gallery behind this same URL. One of each of the
// five kinds this task renders, per the brief.

const CHORD_DEMO: ChordDiagramSpec = {
  name: "G",
  // low-E -> high-E: 3-2-0-0-0-3, the standard open G major shape.
  frets: [3, 2, 0, 0, 0, 3],
  fingers: [3, 2, 0, 0, 0, 4],
  barres: [],
  baseFret: 1,
};

// A minor pentatonic, "box 1" (5th position) — root, b3, 4, 5, b7 twice
// across the neck. string: 1 = high-e .. 6 = low-E (see types.ts).
const SCALE_DEMO: ScaleDiagramSpec = {
  name: "A Minor Pentatonic — Box 1",
  root: "A",
  positions: [
    { string: 6, fret: 5, degree: "1" },
    { string: 6, fret: 8, degree: "b3" },
    { string: 5, fret: 5, degree: "4" },
    { string: 5, fret: 7, degree: "5" },
    { string: 4, fret: 5, degree: "b7" },
    { string: 4, fret: 7, degree: "1" },
    { string: 3, fret: 5, degree: "b3" },
    { string: 3, fret: 7, degree: "4" },
    { string: 2, fret: 5, degree: "5" },
    { string: 2, fret: 8, degree: "b7" },
    { string: 1, fret: 5, degree: "1" },
    { string: 1, fret: 8, degree: "b3" },
  ],
};

const TONE_DEMO: ToneRecipeSpec = {
  artist: "Stevie Ray Vaughan",
  song: "Texas Flood",
  guitar: '1963 Fender Stratocaster ("Number One"), heavy-gauge strings, tuned a half-step down',
  amp: "Blackface-era Fender (Vibroverb/Super Reverb) pushed loud, into natural tube breakup",
  drive: "Ibanez Tube Screamer (TS808) run as a clean-ish boost ahead of the amp",
  chain: "Strat -> Vox wah (occasional) -> Tube Screamer (boost) -> cranked tube amp",
  hands: "Heavy pick attack, wide aggressive vibrato, big string bends, thumb-over chord/rhythm grip",
  listen: ["Texas Flood (1983)", "Pride and Joy", "Couldn't Stand the Weather"],
};

const CHAIN_DEMO: SignalChainSpec = {
  nodes: [
    { label: "Guitar" },
    { label: "Tuner", type: "utility" },
    { label: "Compressor", type: "dynamics" },
    { label: "Overdrive", type: "drive" },
    { label: "Delay", type: "time" },
    { label: "Reverb", type: "time" },
    { label: "Amp", type: "amplifier" },
  ],
};

const AMP_DEMO: AmpSettingsSpec = {
  amp: "Fender '65 Twin Reverb",
  dials: [
    { label: "Volume", value: 6 },
    { label: "Treble", value: 7 },
    { label: "Mid", value: 4 },
    { label: "Bass", value: 5 },
    { label: "Reverb", value: 3 },
  ],
};

/** A plain Server Component, same reasoning as `today/page.tsx`: purely
 * static in-file demo data, no client state or API calls, so nothing here
 * needs "use client" (the one renderer that does — `ChordDiagram`, for its
 * theme-driven svguitar redraw — is still fine to render as a child; a
 * Server Component can render a Client Component directly). */
export default function ArtifactsPage() {
  const t = useTranslations("artifacts");

  const sections = [
    { key: "chordDiagram", node: <Artifact kind="chord_diagram" spec={CHORD_DEMO} /> },
    { key: "scaleDiagram", node: <Artifact kind="scale_diagram" spec={SCALE_DEMO} /> },
    { key: "toneRecipe", node: <Artifact kind="tone_recipe" spec={TONE_DEMO} /> },
    { key: "signalChain", node: <Artifact kind="signal_chain" spec={CHAIN_DEMO} /> },
    { key: "ampDials", node: <Artifact kind="amp_settings" spec={AMP_DEMO} /> },
  ] as const;

  return (
    <div className="flex flex-col gap-8">
      <div>
        <h1 className="text-2xl font-semibold" data-testid="artifacts-heading">
          {t("heading")}
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">{t("subheading")}</p>
      </div>

      <div className="grid gap-8 sm:grid-cols-2">
        {sections.map(({ key, node }) => (
          <section key={key} className="flex flex-col items-start gap-3" data-testid={`artifact-section-${key}`}>
            <h2 className="text-sm font-semibold text-muted-foreground">{t(`sections.${key}`)}</h2>
            {node}
          </section>
        ))}
      </div>
    </div>
  );
}
