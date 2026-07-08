import { Fragment } from "react";
import { Card, CardContent } from "@/components/ui/card";
import type { SignalChainSpec } from "./types";

/** A small right-pointing chevron connecting each node. A real inline SVG
 * (not a lucide icon) so this file has zero extra imports and the arrow's
 * geometry is trivial to reason about; `stroke="currentColor"` picks up the
 * `text-muted-foreground` set on the wrapping span, matching this app's
 * theme tokens for free (no light/dark branching needed). */
function Arrow() {
  return (
    <svg viewBox="0 0 16 16" className="size-4 shrink-0 text-muted-foreground/70" aria-hidden="true">
      <path
        d="M4 2l6 6-6 6"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Renders an ordered `SignalChainSpec` as a row of labeled node boxes
 * connected by arrow glyphs (`Guitar -> Compressor -> Overdrive -> ... ->
 * Amp`). Node boxes are plain HTML (not SVG) deliberately: label lengths
 * are unbounded free text, and CSS flexbox handles auto-sizing, text
 * wrapping and — the brief's explicit responsive requirement — wrapping
 * the whole row onto multiple lines on small screens, all without any
 * text-measurement code. Only the connectors between boxes are SVG. */
export function SignalChain({ spec }: { spec: SignalChainSpec }) {
  return (
    <Card data-testid="signal-chain">
      <CardContent className="flex flex-wrap items-center gap-x-2 gap-y-3">
        {spec.nodes.map((node, i) => (
          <Fragment key={i}>
            {i > 0 && <Arrow />}
            <div
              data-testid="signal-chain-node"
              className="flex flex-col items-center gap-0.5 rounded-lg border border-border bg-muted/40 px-3 py-1.5 text-center"
            >
              <span data-testid="signal-chain-node-label" className="text-sm font-medium text-foreground">
                {node.label}
              </span>
              {node.type && <span className="text-[11px] text-muted-foreground">{node.type}</span>}
            </div>
          </Fragment>
        ))}
      </CardContent>
    </Card>
  );
}
