"use client";

import { useEffect, useRef } from "react";
import { useTheme } from "next-themes";
import { SVGuitarChord, type Barre, type Finger } from "svguitar";
import { Card, CardContent } from "@/components/ui/card";
import type { ChordDiagramSpec } from "./types";

/** Ink-on-transparent palettes for the two themes. svguitar draws into a
 * detached SVG it owns (not React-rendered JSX), so it can't pick up this
 * app's `oklch(...)` CSS custom properties for free the way our own
 * Tailwind-classed SVG components below can — passing `var(--foreground)`
 * strings into svguitar's `configure()` is unreliable across renderers, so
 * this redraws with an explicit literal palette instead whenever
 * `resolvedTheme` flips (see the effect below). Values approximate this
 * app's neutral/grayscale theme tokens (globals.css). Finger dots are ink-
 * filled with a punched-out label, matching the printed-songbook look —
 * consistent with the filled-dot style `scale-diagram.tsx` also uses. */
const PALETTES = {
  light: {
    color: "#18181b",
    backgroundColor: "none",
    titleColor: "#18181b",
    stringColor: "#18181b",
    fretColor: "#18181b",
    tuningsColor: "#71717a",
    fretLabelColor: "#71717a",
    fingerColor: "#18181b",
    fingerTextColor: "#fafafa",
    fingerStrokeColor: "#18181b",
    barreChordStrokeColor: "#18181b",
  },
  dark: {
    color: "#f4f4f5",
    backgroundColor: "none",
    titleColor: "#f4f4f5",
    stringColor: "#f4f4f5",
    fretColor: "#f4f4f5",
    tuningsColor: "#a1a1aa",
    fretLabelColor: "#a1a1aa",
    fingerColor: "#f4f4f5",
    fingerTextColor: "#18181b",
    fingerStrokeColor: "#f4f4f5",
    barreChordStrokeColor: "#f4f4f5",
  },
} as const;

/** svguitar numbers strings 1 (high-E) -> 6 (low-E, confirmed via its own
 * docs: "string 1 represents the highest pitch string"). Our spec's 6-length
 * `frets`/`fingers` arrays are positional, index 0 = low-E -> index 5 =
 * high-E (see types.ts), so string = 6 - index. */
function svguitarString(index: number): number {
  return 6 - index;
}

/** svguitar's per-finger/barre `fret` is 1-indexed *relative to `position`*
 * (fret "1" always means "the row at `position`"), while our spec's
 * `frets`/`baseFret` are absolute fret numbers on the neck (confirmed by
 * cross-checking svguitar's own docs example: an F#m barre at
 * `position: 9` uses finger/barre `fret` values of 1-3 for absolute frets
 * 9-11). Open (0) and muted strings are position-independent and pass
 * through unchanged. */
function relativeFret(absoluteFret: number, baseFret: number): number {
  return absoluteFret - baseFret + 1;
}

function toFingers(spec: ChordDiagramSpec): Finger[] {
  // Explicit `: Finger` on the callback (not just `toFingers`' own return
  // type) so every `return` below is contextually typed against the tuple
  // union directly, rather than relying on `.map()`'s generic inference to
  // find its way back to `Finger` (array literals like `[n, "x"]` would
  // otherwise widen to a plain `(number | string)[]`, not a tuple).
  return spec.frets.map((fret, i): Finger => {
    const stringNum = svguitarString(i);
    if (fret === -1) return [stringNum, "x"];
    if (fret === 0) return [stringNum, 0];
    const fingerNum = spec.fingers[i];
    const rel = relativeFret(fret, spec.baseFret);
    return fingerNum >= 1 && fingerNum <= 4 ? [stringNum, rel, String(fingerNum)] : [stringNum, rel];
  });
}

/** `BarreSpec.fromString`/`toString` are explicit named fields (unlike the
 * positional `frets`/`fingers` arrays), so — per types.ts's documented
 * convention — treated as already using svguitar's own 1=high-E..6=low-E
 * numbering and passed through unchanged; only `fret` needs the
 * absolute->relative conversion. */
function toBarres(spec: ChordDiagramSpec, inkColor: string): Barre[] {
  return spec.barres.map(
    (b): Barre => ({
      fromString: b.fromString,
      toString: b.toString,
      fret: relativeFret(b.fret, spec.baseFret),
      color: inkColor,
    }),
  );
}

/** Renders a `ChordDiagramSpec` via `svguitar` into a self-contained card.
 * The chord name is drawn as svguitar's own diagram title (so the SVG is a
 * complete, standalone artifact if exported/printed on its own) *and*
 * repeated in a visually-hidden span so tests/assistive tech have a stable
 * hook independent of svguitar's internal SVG structure. */
export function ChordDiagram({ spec }: { spec: ChordDiagramSpec }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const { resolvedTheme } = useTheme();
  const palette = resolvedTheme === "dark" ? PALETTES.dark : PALETTES.light;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    // Set directly on the DOM node rather than via a React `style` prop:
    // `palette` depends on `resolvedTheme`, which is always `undefined`
    // during SSR/first paint (next-themes can't know the real theme until
    // it runs client-side) and only resolves to the real value after this
    // effect's first run — a React-rendered `style` would make the
    // server-rendered and post-hydration `color` values genuinely differ
    // and trip a hydration-mismatch warning. Effect-only avoids that
    // entirely, consistent with the rest of this component already being
    // fully imperative (svguitar itself never renders anything server-side).
    el.style.color = palette.fingerColor;

    // Defensive normalization (Task 5, found via two real end-to-end LLM
    // generations in the same verification session): a validated spec can
    // still carry a `baseFret` inconsistent with its own fret/barre data, in
    // two different ways. (1) `baseFret: 0` — schema-valid (the Pydantic
    // model had no `ge=1` at the time this was found) but not a real
    // fretboard position; svguitar's own `position` setting throws
    // ("Position cannot be less than 1") for anything under 1, crashing this
    // whole component. (2) `baseFret` *higher* than the lowest actually-
    // fretted note/barre doesn't crash, but pushes that note's *relative*
    // fret below 1 — off the top of svguitar's drawable grid, so it's
    // silently never drawn (observed live: `baseFret: 6` alongside frets
    // `[3,2,0,0,0,3]` rendered an apparently-empty grid, since
    // `relativeFret(3, 6) = -2`). Both are the same underlying problem — an
    // untrustworthy `baseFret` — so both are guarded here in one place:
    // clamp to `[1, the lowest fretted/barred position]`, which guarantees
    // every relative fret computed below lands at 1 or higher. A
    // *well-formed* spec is unaffected: clamping only ever pulls `baseFret`
    // DOWN to the lowest fretted/barred position, and a correct spec's
    // `baseFret` is already at or below that (by definition — otherwise its
    // own lowest note wouldn't render either), so `Math.min` is a no-op for
    // it. Every use below reads `normalizedSpec`, never `spec`, so the
    // diagram's position and its
    // relative-fret math always agree.
    const frettedPositions = [...spec.frets.filter((f) => f > 0), ...spec.barres.map((b) => b.fret)];
    const lowestFrettedPosition = frettedPositions.length ? Math.min(...frettedPositions) : spec.baseFret;
    const safeBaseFret = Math.min(Math.max(1, spec.baseFret), Math.max(1, lowestFrettedPosition));
    const normalizedSpec: ChordDiagramSpec = { ...spec, baseFret: safeBaseFret };

    const relFrets = normalizedSpec.frets
      .filter((f) => f > 0)
      .map((f) => relativeFret(f, normalizedSpec.baseFret));
    const fretsToShow = Math.min(Math.max(4, ...relFrets), 12);

    const chart = new SVGuitarChord(el);
    chart
      .configure({
        ...palette,
        frets: fretsToShow,
        position: normalizedSpec.baseFret,
      })
      .chord({
        fingers: toFingers(normalizedSpec),
        barres: toBarres(normalizedSpec, palette.fingerColor),
        title: normalizedSpec.name,
      })
      .draw();

    // Clean up the previous render on every spec/theme change, not just
    // unmount: svguitar owns the DOM inside `el` imperatively, so without
    // this a theme flip would just draw a second SVG on top of the first.
    return () => chart.remove();
  }, [spec, palette]);

  return (
    <Card data-testid="chord-diagram" className="w-fit max-w-[280px]">
      <CardContent className="pt-1">
        <div
          ref={containerRef}
          role="img"
          aria-label={spec.name}
          // svguitar 2.5.1 has a rendering bug in its default RECTANGLE
          // barre style: the fill it draws is hardcoded to `'black'`
          // instead of the per-barre/settings `color` this component sets
          // (confirmed by reading its bundled source — the ARC style's
          // fill *does* honor `color` correctly, only RECTANGLE doesn't).
          // Worked around here rather than switching to the ARC style,
          // since a solid rectangle is the more universally recognized
          // barre convention in printed chord charts: the effect above sets
          // this element's `color`, and `fill-current` on a
          // `.barre-rectangle` descendant is a real CSS rule, which —
          // unlike svguitar's own inline `fill="black"` attribute — wins
          // the cascade (presentation attributes always lose to an author
          // stylesheet rule).
          className="[&_.barre-rectangle]:fill-current [&_svg]:h-auto [&_svg]:w-full"
        />
        <span className="sr-only" data-testid="chord-diagram-name">
          {spec.name}
        </span>
      </CardContent>
    </Card>
  );
}
