"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { Pause, Play } from "lucide-react";
import type { AlphaTabApi } from "@coderline/alphatab";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TabSpec } from "./types";

// AlphaTab draws notation glyphs into its own SVG with a hardcoded default
// ink (near-black — RenderingResources.mainGlyphColor etc. all default to
// `rgb(0,0,0)`-ish values), the same "can't pick up this app's CSS custom
// properties for free" problem chord-diagram.tsx already solved for
// svguitar — confirmed live: on this app's dark theme, default-black
// notation against the dark `Card` background was barely legible. Same
// literal-hex-palette fix, reusing chord-diagram.tsx's exact ink/muted
// values for visual consistency across renderers. `barNumberColor` is left
// at AlphaTab's own default (a saturated red, `rgb(200,0,0)`) rather than
// added to this palette — it already reads fine against both themes, so
// there's nothing to fix there.
const PALETTES = {
  light: {
    mainGlyphColor: "#18181b",
    secondaryGlyphColor: "rgba(24, 24, 27, 0.4)",
    scoreInfoColor: "#18181b",
    barSeparatorColor: "#18181b",
    staffLineColor: "#71717a",
  },
  dark: {
    mainGlyphColor: "#f4f4f5",
    secondaryGlyphColor: "rgba(244, 244, 245, 0.4)",
    scoreInfoColor: "#f4f4f5",
    barSeparatorColor: "#f4f4f5",
    staffLineColor: "#a1a1aa",
  },
} as const;

// Same-origin copies of AlphaTab's asset dependencies under public/alphatab/
// (see the repo's task brief / plan doc for the exact copy commands run).
// AlphaTab's default behavior is to resolve its music font (and, if not
// overridden, its worker/soundfont) relative to wherever it thinks its own
// script was loaded from — which can otherwise mean a CDN (jsDelivr) fetch.
// This app's hard constraint is a fully offline/no-CDN standalone build, so
// every one of these paths is pinned to a same-origin static file instead.
const FONT_DIRECTORY = "/alphatab/font/";
const SOUND_FONT = "/alphatab/soundfont/sonivox.sf2";
// AlphaTab's ESM build resolves its Worker/AudioWorklet scripts relative to
// its own `import.meta.url` (`new URL("./alphaTab.worker.mjs", import.meta.url)`),
// which depends on the bundler recognizing and rewriting that reference —
// true for webpack/Vite *with* AlphaTab's own bundler plugin, not guaranteed
// for a plain Next.js/Turbopack build with neither (this app's dev server
// confirms Turbopack). `core.scriptFile` is AlphaTab's documented escape
// hatch for exactly this ("AlphaTab will do its best to auto-detect this
// path but in case it fails, this setting can be used to explicitly define
// it" — CoreSettings.scriptFile): every one of AlphaTab's internal fallback
// strategies (including the plain classic-worker `importScripts` route,
// confirmed by reading the shipped alphaTab.mjs) ultimately loads *this*
// URL, so it needs to be a same-origin copy of the self-contained UMD build
// (not the split .mjs, which has a bare ESM `import` of alphaTab.core.mjs
// and would fail to parse via `importScripts`). It must also be an
// *absolute* URL: confirmed live (Chromium) that the classic-worker
// fallback's `importScripts('/alphatab/alphaTab.min.js')` throws
// `SyntaxError: ... The URL '/alphatab/alphaTab.min.js' is invalid` — a
// root-relative path has no document to resolve against inside a `blob:`-
// URL worker, unlike `fontDirectory`/`soundFont` below (loaded from the main
// thread via CSS/fetch, where root-relative paths resolve against the page
// as normal). Resolved to absolute at use (inside the effect, `window` is
// guaranteed to exist) rather than hardcoding a `://` scheme here, so this
// still works unchanged under plain HTTP or HTTPS deploys alike.
const SCRIPT_FILE_PATH = "/alphatab/alphaTab.min.js";

// How long to wait for `playerReady` before treating playback as
// unavailable rather than merely "still loading" (see `PlaybackStatus`
// below). AlphaTab's player always creates its own Web Worker for
// synthesis regardless of `core.useWorkers` (confirmed by reading
// `createWorkerPlayer()` in the shipped source, which unconditionally calls
// `BrowserUiFacade.createAlphaSynthWebWorker()`) — if that worker's script
// 404s, the browser doesn't surface a synchronous error (a `new
// Worker(url)` for a missing file fails asynchronously, if at all, from
// AlphaTab's perspective), so `playerReady` would otherwise just never fire
// and the Play button would look permanently "loading" rather than clearly
// non-functional.
const PLAYER_READY_TIMEOUT_MS = 8000;

type PlaybackStatus = "loading" | "ready" | "unavailable";

/**
 * Renders a `TabSpec` (an alphaTex string) as guitar tab + standard notation
 * via `@coderline/alphatab`, with a Play button wired to the built-in synth.
 *
 * Must be a Client Component: AlphaTab manipulates the DOM directly (it owns
 * `containerRef`'s children imperatively, like svguitar in chord-diagram.tsx)
 * and cannot run during SSR. `@coderline/alphatab` is dynamically imported
 * *inside* the effect (not at module scope) for the same reason — importing
 * it eagerly would still execute its module-init code (which touches
 * `window`/`document`) during the server render pass.
 *
 * Notation is the non-negotiable part of this component (must always render,
 * fully offline); playback is best-effort on top — if the synth's worker
 * never becomes ready (see PLAYER_READY_TIMEOUT_MS), the Play button stays
 * present but disabled with an explanatory title, per the plan's sanctioned
 * "notation-only" fallback. It is not expected to be needed (see the task
 * report for what was actually observed), but the component degrades to it
 * automatically rather than assuming success.
 */
export function TabView({ spec }: { spec: TabSpec }) {
  const t = useTranslations("artifacts.tab");
  const { resolvedTheme } = useTheme();
  const palette = resolvedTheme === "dark" ? PALETTES.dark : PALETTES.light;
  const containerRef = useRef<HTMLDivElement>(null);
  const apiRef = useRef<AlphaTabApi | null>(null);
  const [status, setStatus] = useState<PlaybackStatus>("loading");
  const [isPlaying, setIsPlaying] = useState(false);

  // Recreates the whole AlphaTabApi (tex parse + render + a fresh player/
  // worker/soundfont fetch) whenever `spec` *or* `palette` changes — same
  // full-recreate-on-palette-change approach chord-diagram.tsx already
  // takes for svguitar, for the same reason: AlphaTab's `RenderingResources`
  // colors are read once at construction (they're plain `ColorJson`
  // strings only on the way in; `updateSettings()`'s docs example only
  // shows mutating already-typed fields like `display.scale`, not
  // rebuilding the resources' internal `Color` objects), so reliably
  // re-coloring an already-running instance isn't a documented/verified
  // path — recreating is simpler and, since this is a small demo lick, has
  // no noticeable cost. The one downside — toggling the theme mid-playback
  // restarts the player — is accepted for the same reason chord-diagram.tsx
  // accepts a redraw-on-toggle: correctness over micro-optimizing a rare
  // interaction.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    let cancelled = false;
    let api: AlphaTabApi | null = null;
    let readyTimeout: ReturnType<typeof setTimeout> | undefined;

    (async () => {
      const alphaTab = await import("@coderline/alphatab");
      if (cancelled) return;

      api = new alphaTab.AlphaTabApi(el, {
        core: {
          fontDirectory: FONT_DIRECTORY,
          scriptFile: new URL(SCRIPT_FILE_PATH, window.location.origin).toString(),
          useWorkers: false,
          logLevel: alphaTab.LogLevel.Warning,
        },
        display: {
          resources: palette,
        },
        player: {
          playerMode: alphaTab.PlayerMode.EnabledSynthesizer,
          soundFont: SOUND_FONT,
          // Sidesteps a second, less forgiving asset-resolution path: the
          // AudioWorklet module load has no fallback to `scriptFile` if its
          // own `import.meta.url`-relative URL doesn't resolve (unlike the
          // worker path above, which does) — confirmed by reading the
          // shipped source. ScriptProcessorNode is deprecated but still
          // implemented everywhere and needs no separate module file.
          outputMode: alphaTab.PlayerOutputMode.WebAudioScriptProcessor,
          enableCursor: true,
          scrollMode: alphaTab.ScrollMode.Off,
        },
      });
      apiRef.current = api;

      api.error.on((e) => {
        console.error("AlphaTab error:", e);
        setStatus("unavailable");
      });
      api.playerReady.on(() => {
        clearTimeout(readyTimeout);
        setStatus("ready");
      });
      // `alphaTab.PlayerState` (as shown in AlphaTab's own docs examples) is
      // not actually exported at the top level in 1.8.4 — confirmed by
      // inspecting the shipped bundle's export list; the real export lives
      // under the `synth` sub-namespace.
      api.playerStateChanged.on((e) => setIsPlaying(e.state === alphaTab.synth.PlayerState.Playing));

      readyTimeout = setTimeout(() => setStatus((s) => (s === "loading" ? "unavailable" : s)), PLAYER_READY_TIMEOUT_MS);

      api.tex(spec.alphaTex);
    })();

    return () => {
      cancelled = true;
      clearTimeout(readyTimeout);
      api?.destroy();
      apiRef.current = null;
    };
  }, [spec, palette]);

  const title = spec.title ?? t("fallbackTitle");

  return (
    <Card data-testid="tab-view" className="w-full max-w-2xl">
      <CardHeader className="has-data-[slot=card-action]:grid-cols-[1fr_auto]">
        <CardTitle>{title}</CardTitle>
        <CardAction>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="tab-view-play"
            disabled={status !== "ready"}
            title={status === "unavailable" ? t("playbackUnavailable") : status === "loading" ? t("playbackLoading") : undefined}
            onClick={() => apiRef.current?.playPause()}
          >
            {isPlaying ? <Pause /> : <Play />}
            {isPlaying ? t("pause") : t("play")}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        <div ref={containerRef} aria-label={title} className="overflow-x-auto" />
      </CardContent>
    </Card>
  );
}
