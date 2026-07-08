/**
 * Client-side mirrors of `apps/api/app/artifacts/specs.py`'s per-kind Pydantic
 * models (Plan 4 Task 1). These are *not* re-validated here — per that
 * module's docstring, a spec reaching a renderer has already passed
 * `validate_spec` server-side; rendering is deterministic and trusts the
 * shape. Kept in sync by hand (small, stable surface) rather than generated.
 *
 * Two different string-numbering conventions coexist in the backend spec on
 * purpose, and every renderer below has to respect both:
 *  - `ChordDiagramSpec.frets`/`fingers` are positional 6-length arrays with
 *    no explicit per-entry string number, documented "index 0 = low-E ->
 *    index 5 = high-E" (see specs.py).
 *  - Every *explicit* numeric `string` field (`BarreSpec.fromString`/
 *    `toString`, `PositionSpec.string`) instead follows conventional guitar/
 *    tab numbering, 1 = high-E .. 6 = low-E — the same convention svguitar
 *    itself uses, and the natural reading of a named field vs. a bare array
 *    position. `chord-diagram.tsx` documents the resulting index<->string
 *    mapping where it matters.
 */

export interface BarreSpec {
  fret: number;
  fromString: number;
  toString: number;
}

export interface ChordDiagramSpec {
  name: string;
  /** Length 6, low-E -> high-E. -1 = muted, 0 = open, else an absolute fret number. */
  frets: number[];
  /** Length 6, low-E -> high-E. 0 = no assigned finger, 1-4 = fret-hand finger. */
  fingers: number[];
  barres: BarreSpec[];
  baseFret: number;
}

export interface PositionSpec {
  /** 1-6, 1 = high-E .. 6 = low-E. */
  string: number;
  fret: number;
  degree?: string | null;
}

export interface ScaleDiagramSpec {
  name: string;
  root: string;
  positions: PositionSpec[];
}

export interface NodeSpec {
  label: string;
  type?: string | null;
}

export interface SignalChainSpec {
  /** Ordered guitar -> ... -> amp. */
  nodes: NodeSpec[];
}

export interface DialSpec {
  label: string;
  /** 0-10. */
  value: number;
}

export interface AmpSettingsSpec {
  amp?: string | null;
  dials: DialSpec[];
}

export interface ToneRecipeSpec {
  artist?: string | null;
  song?: string | null;
  guitar: string;
  amp: string;
  drive?: string | null;
  chain: string;
  hands?: string | null;
  listen: string[];
}

/** Every kind the backend's `SPECS` registry recognizes (see specs.py).
 * `artifact.tsx` only has renderers for the five in this plan's Task 2 scope
 * ("tab" is Task 3 via AlphaTab; "gear_card" is unscheduled) — both fall
 * through to its "unsupported kind" placeholder rather than failing to
 * render entirely, since a validated-but-not-yet-rendered kind is expected
 * to reach the client over time as later tasks land. */
export type ArtifactKind =
  | "chord_diagram"
  | "scale_diagram"
  | "tab"
  | "signal_chain"
  | "amp_settings"
  | "tone_recipe"
  | "gear_card";
