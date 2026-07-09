"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Card, CardContent } from "@/components/ui/card";
import { TabView } from "./tab-view";
import type { TabSpec } from "./types";

// How far outside the viewport to start mounting — a positive rootMargin
// pre-loads slightly-below-the-fold content so it's ready by the time a
// user actually scrolls to it, instead of popping in mid-scroll.
const ROOT_MARGIN = "300px 0px";

/**
 * IntersectionObserver-gated mount for `TabView` (Plan 4 Task 3's own
 * carry-forward note to Task 5): every `TabView` instance builds a real
 * AlphaTab synth — one Web Worker + a 1.35 MB soundfont decode PER INSTANCE
 * (the soundfont *bytes* are cache-deduped by the browser across instances;
 * the worker and the decode are not). The old single-of-each-kind demo page
 * only ever mounted one `TabView` at a time, so this never mattered; a
 * gallery of N artifacts (Task 5) can hold several `tab` kinds at once, and
 * mounting all of them eagerly would spin up N synth workers on page load
 * for cards the user may never scroll to.
 *
 * The other 5 rendered kinds (chord/scale/tone/chain/amp — see
 * `artifact.tsx`) are plain SVG/CSS, cheap enough to render immediately per
 * the brief, so only the `tab` case is routed through this wrapper; nothing
 * else changed. Once triggered, this stays mounted permanently — no
 * unmount-on-scroll-away — since tearing down and rebuilding a synth worker
 * every time a card scrolls past the viewport would cost more than it saves
 * for a handful of short artifacts (not an infinite feed).
 */
export function LazyTabView({ spec }: { spec: TabSpec }) {
  const t = useTranslations("artifacts.tab");
  const containerRef = useRef<HTMLDivElement>(null);
  // Lazy initializer (not a synchronous setState-in-effect): a browser with
  // no IntersectionObserver at all (very old) starts already "visible" so it
  // mounts TabView immediately instead of waiting on an observer that could
  // never fire — a permanently-blank artifact would be worse than the perf
  // cost this wrapper otherwise guards against.
  const [visible, setVisible] = useState(() => typeof IntersectionObserver === "undefined");

  useEffect(() => {
    if (visible) return; // already visible (or observer unsupported) — nothing to observe
    const el = containerRef.current;
    if (!el) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
      },
      { rootMargin: ROOT_MARGIN },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [visible]);

  if (visible) return <TabView spec={spec} />;

  return (
    <div ref={containerRef} data-testid="tab-view-pending">
      <Card className="w-full max-w-2xl">
        <CardContent className="flex h-40 items-center justify-center text-sm text-muted-foreground">
          {t("pending")}
        </CardContent>
      </Card>
    </div>
  );
}
