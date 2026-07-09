import { test, expect } from "@playwright/test";

// Deterministic, offline coverage of the Plan 4 artifact renderers:
// `/en/artifacts` is a temporary demo route (no API calls — see its
// docstring) rendering one of each in-scope kind from a fixed in-file spec,
// so unlike cockpit.spec.ts/knowledge.spec.ts there is no `page.route`
// mocking needed here. Task 3 adds the sixth kind, "tab" (AlphaTab).

test.describe("artifacts demo page", () => {
  test("renders one of each artifact kind from a fixed spec", async ({ page }) => {
    await page.goto("/en/artifacts");

    await expect(page.getByTestId("artifacts-heading")).toBeVisible();

    // Chord diagram: the svguitar SVG renders, and the chord name is present
    // (asserted via the visually-hidden name span, independent of svguitar's
    // own internal SVG structure — see chord-diagram.tsx).
    const chord = page.getByTestId("chord-diagram");
    await expect(chord.locator("svg")).toBeVisible();
    await expect(chord.getByTestId("chord-diagram-name")).toHaveText("G");

    // Scale diagram: the custom fretboard SVG renders with its name/root.
    const scale = page.getByTestId("scale-diagram");
    await expect(scale.locator("svg")).toBeVisible();
    await expect(scale.getByTestId("scale-diagram-name")).toHaveText(/A Minor Pentatonic/);

    // Tone recipe — the "money artifact": amp text (and title) visible.
    const tone = page.getByTestId("tone-recipe-card");
    await expect(tone.getByTestId("tone-recipe-title")).toHaveText("Texas Flood");
    await expect(tone.getByTestId("tone-recipe-amp")).toContainText("Blackface-era Fender");

    // Signal chain: every node's label visible, in spec order.
    const chain = page.getByTestId("signal-chain");
    await expect(chain.getByTestId("signal-chain-node-label")).toHaveText([
      "Guitar",
      "Tuner",
      "Compressor",
      "Overdrive",
      "Delay",
      "Reverb",
      "Amp",
    ]);

    // Amp dials: every dial's formatted value visible, in spec order.
    const dials = page.getByTestId("amp-dials");
    await expect(dials.getByTestId("amp-dials-amp")).toHaveText("Fender '65 Twin Reverb");
    await expect(dials.getByTestId("amp-dial-value")).toHaveText(["6", "7", "4", "5", "3"]);

    // Tab/staff: AlphaTab mounts client-side (dynamic import + useEffect), so
    // its container starts empty and only gains an <svg> after its own
    // async render pass — toBeVisible()'s auto-retry covers that, no manual
    // wait needed. A Play control must exist per the brief; whether it's
    // enabled (real synth playback wired) or present-but-disabled (the
    // brief's sanctioned notation-only escape hatch) is a rendering detail
    // this test deliberately doesn't pin down — "audio itself not asserted".
    const tab = page.getByTestId("tab-view");
    await expect(tab.locator("svg")).toBeVisible();
    await expect(page.getByTestId("tab-view-play")).toBeVisible();
  });

  test("the Artifacts nav link routes to the demo page", async ({ page }) => {
    await page.goto("/en/today");
    await page.getByTestId("nav-artifacts").click();
    await expect(page).toHaveURL(/\/en\/artifacts$/);
    await expect(page.getByTestId("artifacts-heading")).toBeVisible();
  });

  test("AlphaTab renders the tab artifact without any cross-origin request", async ({ page, baseURL }) => {
    // The hard offline/no-CDN constraint (see tab-view.tsx): AlphaTab
    // defaults to pulling its music font + soundfont from a CDN unless every
    // asset path is pointed at a same-origin copy under public/alphatab/.
    // Listening from before navigation catches the async font/soundfont
    // fetches AlphaTab issues once it mounts, not just the initial document.
    const baseOrigin = new URL(baseURL!).origin;
    const crossOrigin: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).origin !== baseOrigin) crossOrigin.push(request.url());
    });

    await page.goto("/en/artifacts");
    await expect(page.getByTestId("tab-view").locator("svg")).toBeVisible();
    // Wait for the Play control to become *enabled*, not just for the
    // notation to render: it only enables once AlphaTab's `playerReady`
    // fires, which — per tab-view.tsx's own player.ready handler (confirmed
    // by reading AlphaTab's shipped source) — is downstream of the
    // soundfont finishing its (same-origin) load. Asserting right after the
    // SVG appears would race the soundfont fetch and could pass even if it
    // silently pointed at a CDN. The timeout here is set comfortably above
    // TabView's own PLAYER_READY_TIMEOUT_MS (8s) so a genuinely-stuck
    // player fails this assertion instead of the test just timing out first
    // for an unrelated reason.
    await expect(page.getByTestId("tab-view-play")).toBeEnabled({ timeout: 10_000 });

    expect(crossOrigin).toEqual([]);
  });
});
