import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AmpSettingsSpec } from "./types";

const SIZE = 64;
const CENTER = SIZE / 2;
const KNOB_R = 22;
const POINTER_LEN = 15;
/** Reference ticks at min/mid/max — purely decorative, drawn once "up" and
 * rotated into place the same way the value pointer is, below. */
const TICK_ANGLES = [-135, 0, 135];

/** Classic rotary-pot sweep: -135deg (value 0, ~7 o'clock) through 0deg
 * (value 5, straight up) to +135deg (value 10, ~5 o'clock). SVG's
 * `rotate()` is clockwise for positive angles, which is what makes 0->10
 * sweep left-to-right through "up" here. */
function angleFor(value: number): number {
  const clamped = Math.min(10, Math.max(0, value));
  return -135 + (clamped / 10) * 270;
}

function formatValue(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function Knob({ value }: { value: number }) {
  const angle = angleFor(value);
  return (
    <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="size-14" aria-hidden="true">
      <circle cx={CENTER} cy={CENTER} r={KNOB_R} className="fill-muted stroke-border" strokeWidth={1.5} />
      {TICK_ANGLES.map((a) => (
        <line
          key={a}
          x1={CENTER}
          y1={CENTER - KNOB_R - 1}
          x2={CENTER}
          y2={CENTER - KNOB_R - 5}
          className="stroke-muted-foreground/50"
          strokeWidth={1.5}
          strokeLinecap="round"
          transform={`rotate(${a} ${CENTER} ${CENTER})`}
        />
      ))}
      <line
        x1={CENTER}
        y1={CENTER}
        x2={CENTER}
        y2={CENTER - POINTER_LEN}
        className="stroke-foreground"
        strokeWidth={3}
        strokeLinecap="round"
        transform={`rotate(${angle} ${CENTER} ${CENTER})`}
      />
      <circle cx={CENTER} cy={CENTER} r={3} className="fill-foreground" />
    </svg>
  );
}

/** Renders an `AmpSettingsSpec` as a row of knob SVGs, each a circle with a
 * pointer rotated to `value / 10`, label + numeric value underneath. The
 * value is also rendered as plain text (not just the pointer angle) so it's
 * both readable at a glance and assertable/accessible independent of the
 * SVG geometry. */
export function AmpDials({ spec }: { spec: AmpSettingsSpec }) {
  return (
    <Card data-testid="amp-dials" className="w-fit">
      {spec.amp && (
        <CardHeader>
          <CardTitle data-testid="amp-dials-amp">{spec.amp}</CardTitle>
        </CardHeader>
      )}
      <CardContent className="flex flex-wrap gap-6">
        {spec.dials.map((dial, i) => (
          <div key={i} data-testid="amp-dial" className="flex flex-col items-center gap-1">
            <Knob value={dial.value} />
            <span data-testid="amp-dial-label" className="text-xs font-medium">
              {dial.label}
            </span>
            <span data-testid="amp-dial-value" className="text-xs text-muted-foreground">
              {formatValue(dial.value)}
            </span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
