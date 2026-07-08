import { test, expect } from "@playwright/test";

// Deterministic, offline coverage of the Plan 4 Task 2 artifact renderers:
// `/en/artifacts` is a temporary demo route (no API calls — see its
// docstring) rendering one of each of the five in-scope kinds from a fixed
// in-file spec, so unlike cockpit.spec.ts/knowledge.spec.ts there is no
// `page.route` mocking needed here.

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
  });

  test("the Artifacts nav link routes to the demo page", async ({ page }) => {
    await page.goto("/en/today");
    await page.getByTestId("nav-artifacts").click();
    await expect(page).toHaveURL(/\/en\/artifacts$/);
    await expect(page.getByTestId("artifacts-heading")).toBeVisible();
  });
});
