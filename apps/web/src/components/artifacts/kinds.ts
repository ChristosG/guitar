/**
 * The 7 backend-recognized artifact kinds (`app.artifacts.specs.SPECS`),
 * shared by every kind-picker UI — the Artifacts gallery's generate form
 * (`generate-form.tsx`) and the curriculum board's per-segment attach dialog
 * (`components/curriculum/attach-artifact-dialog.tsx`) — so the list, order,
 * and label-key mapping are defined exactly once instead of duplicated.
 *
 * Order: the 6 rendered kinds (see `artifact.tsx`), in the same order Task
 * 2/3's demo sections used. "gear_card" is deliberately NOT offered: it has
 * no client renderer, so picking it spent one (sometimes two, with the repair
 * retry) billed model calls to produce a card that can only ever display the
 * "unsupported kind" placeholder — forever, not transitionally. The kind
 * stays valid server-side and in `ARTIFACT_KIND_LABEL_KEY` below (existing
 * persisted gear_cards still need their label); it returns to this list the
 * day it gets a renderer in `artifact.tsx`.
 */
import type { ArtifactKind } from "./types";

export const ARTIFACT_KINDS: ArtifactKind[] = [
  "chord_diagram",
  "scale_diagram",
  "tab",
  "signal_chain",
  "amp_settings",
  "tone_recipe",
];

/** kind -> the `artifacts.sections.*` message key already used to label each
 * kind (Task 2/3's demo section headings) — reused here rather than
 * duplicated under a second set of keys. */
export const ARTIFACT_KIND_LABEL_KEY: Record<ArtifactKind, string> = {
  chord_diagram: "chordDiagram",
  scale_diagram: "scaleDiagram",
  tab: "tab",
  signal_chain: "signalChain",
  amp_settings: "ampDials",
  tone_recipe: "toneRecipe",
  gear_card: "gearCard",
};
