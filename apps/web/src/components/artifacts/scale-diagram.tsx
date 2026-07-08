import { useTranslations } from "next-intl";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { PositionSpec, ScaleDiagramSpec } from "./types";

const FRET_W = 56;
const STRING_GAP = 28;
const TOP_PAD = 22;
const LEFT_PAD = 30;
const RIGHT_PAD = 20;
const BOTTOM_PAD = 16;
const DOT_R = 11;
const MIN_CELLS = 4;

/** 1 = high-e .. 6 = low-E, matching `PositionSpec.string` (see types.ts). */
const STRING_LABELS: Record<number, string> = { 1: "e", 2: "B", 3: "G", 4: "D", 5: "A", 6: "E" };
const OPEN_STRING_PITCH: Record<number, number> = { 1: 4, 2: 11, 3: 7, 4: 2, 5: 9, 6: 4 };

const PITCH_CLASS: Record<string, number> = {
  C: 0,
  "B#": 0,
  "C#": 1,
  Db: 1,
  D: 2,
  "D#": 3,
  Eb: 3,
  E: 4,
  Fb: 4,
  F: 5,
  "E#": 5,
  "F#": 6,
  Gb: 6,
  G: 7,
  "G#": 8,
  Ab: 8,
  A: 9,
  "A#": 10,
  Bb: 10,
  B: 11,
  Cb: 11,
};
const SINGLE_INLAYS = [3, 5, 7, 9, 15, 17, 19, 21];
const DOUBLE_INLAYS = [12, 24];

function noteAt(stringNum: number, fret: number): number {
  return (OPEN_STRING_PITCH[stringNum] + fret) % 12;
}

/** Parses a free-text root like "A", "Eb", "f#" -> a 0-11 pitch class, or
 * `null` if unparseable (an unrecognized spelling shouldn't crash the
 * diagram — it just falls back to `degree`-only root detection below). */
function parseRootPitch(root: string): number | null {
  const trimmed = root.trim().replace(/\d+$/, ""); // drop a trailing octave number, e.g. "A4"
  if (!trimmed) return null;
  const key = trimmed.length > 1 ? trimmed[0].toUpperCase() + trimmed.slice(1).toLowerCase() : trimmed.toUpperCase();
  return key in PITCH_CLASS ? PITCH_CLASS[key] : null;
}

/** A position is "the root" if it's explicitly degree "1", or — when no
 * degree is given at all — its computed pitch matches `spec.root`. A
 * position with some *other* explicit degree is trusted over pitch math. */
function isRoot(pos: PositionSpec, rootPitch: number | null): boolean {
  if (pos.degree) return pos.degree === "1";
  return rootPitch !== null && noteAt(pos.string, pos.fret) === rootPitch;
}

/** A from-scratch fretboard SVG (not svguitar, which only draws chord-grip
 * boxes): 6 strings horizontal, high-e on top / low-E on bottom — the same
 * top-to-bottom order tab notation uses, matching this app's tab-first
 * beginner pedagogy (see the design doc). Frets run left->right. The
 * displayed fret window auto-fits the given positions (min 4 cells so a
 * tight 1-2 fret pattern doesn't look cramped); a position window that
 * doesn't start at the nut gets a chord-diagram-style "Nfr" label instead. */
export function ScaleDiagram({ spec }: { spec: ScaleDiagramSpec }) {
  const t = useTranslations("artifacts.scaleDiagram");

  const fretValues = spec.positions.map((p) => p.fret);
  const minFret = fretValues.length ? Math.min(...fretValues) : 0;
  const maxFret = fretValues.length ? Math.max(...fretValues) : 0;
  const startAtNut = minFret === 0;
  const firstDrawnFret = startAtNut ? 1 : minFret;
  const lastFret = Math.max(maxFret, firstDrawnFret + MIN_CELLS - 1);
  const spanCells = lastFret - firstDrawnFret + 1;
  const rootPitch = parseRootPitch(spec.root);

  const width = LEFT_PAD + spanCells * FRET_W + RIGHT_PAD;
  const height = TOP_PAD + 5 * STRING_GAP + BOTTOM_PAD;

  const stringY = (stringNum: number) => TOP_PAD + (stringNum - 1) * STRING_GAP;
  const cellCenterX = (fret: number) => {
    if (fret === 0) return LEFT_PAD;
    const cell = fret - firstDrawnFret + 1;
    return LEFT_PAD + (cell - 0.5) * FRET_W;
  };
  const inWindow = (fret: number) => fret >= firstDrawnFret && fret <= lastFret;

  return (
    <Card data-testid="scale-diagram" className="w-fit">
      <CardHeader>
        <CardTitle data-testid="scale-diagram-name">{spec.name}</CardTitle>
        <CardDescription>
          {t("root")}: {spec.root}
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          className="h-auto max-w-full"
          role="img"
          aria-label={`${spec.name} fretboard diagram`}
        >
          {!startAtNut && (
            <text x={LEFT_PAD - 6} y={TOP_PAD - 8} textAnchor="end" className="fill-muted-foreground text-[10px]">
              {firstDrawnFret}fr
            </text>
          )}

          {/* string labels */}
          {[1, 2, 3, 4, 5, 6].map((s) => (
            <text
              key={`label-${s}`}
              x={LEFT_PAD - 14}
              y={stringY(s)}
              textAnchor="middle"
              dominantBaseline="central"
              className="fill-muted-foreground text-[11px] font-medium"
            >
              {STRING_LABELS[s]}
            </text>
          ))}

          {/* fret lines */}
          {Array.from({ length: spanCells + 1 }, (_, i) => {
            const x = LEFT_PAD + i * FRET_W;
            const isNut = i === 0 && startAtNut;
            return (
              <line
                key={`fret-${i}`}
                x1={x}
                y1={stringY(1)}
                x2={x}
                y2={stringY(6)}
                className={isNut ? "stroke-foreground" : "stroke-border"}
                strokeWidth={isNut ? 4 : 1.5}
              />
            );
          })}

          {/* strings */}
          {[1, 2, 3, 4, 5, 6].map((s) => (
            <line
              key={`string-${s}`}
              x1={LEFT_PAD}
              y1={stringY(s)}
              x2={LEFT_PAD + spanCells * FRET_W}
              y2={stringY(s)}
              className="stroke-border"
              strokeWidth={1.5}
            />
          ))}

          {/* fret inlay markers, like a real neck */}
          {SINGLE_INLAYS.filter(inWindow).map((fret) => (
            <circle
              key={`inlay-${fret}`}
              cx={cellCenterX(fret)}
              cy={(stringY(3) + stringY(4)) / 2}
              r={4}
              className="fill-muted"
            />
          ))}
          {DOUBLE_INLAYS.filter(inWindow).map((fret) => (
            <g key={`inlay-${fret}`}>
              <circle cx={cellCenterX(fret)} cy={stringY(2) + STRING_GAP / 4} r={4} className="fill-muted" />
              <circle cx={cellCenterX(fret)} cy={stringY(5) - STRING_GAP / 4} r={4} className="fill-muted" />
            </g>
          ))}

          {/* scale-tone dots, root ring-highlighted */}
          {spec.positions.map((pos, i) => {
            const root = isRoot(pos, rootPitch);
            const x = cellCenterX(pos.fret);
            const y = stringY(pos.string);
            return (
              <g key={i} data-testid="scale-diagram-dot" data-root={root || undefined}>
                {root && <circle cx={x} cy={y} r={DOT_R + 3} className="fill-none stroke-foreground" strokeWidth={2} />}
                <circle cx={x} cy={y} r={DOT_R} className="fill-foreground" />
                {pos.degree && (
                  <text
                    x={x}
                    y={y}
                    textAnchor="middle"
                    dominantBaseline="central"
                    className="fill-background text-[10px] font-semibold"
                  >
                    {pos.degree}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </CardContent>
    </Card>
  );
}
