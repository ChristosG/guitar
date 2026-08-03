import { expect, test, type Page } from "@playwright/test";

/** THE EGRESS CONTRACT, finally enforced rather than asserted in prose.
 *
 * `desktop/src-tauri/src/main.rs` promises: "The running app never contacts
 * any host but 127.0.0.1/localhost itself; the API process talks to
 * api.anthropic.com… Nothing else. No telemetry, no update pings." Until this
 * spec, no test enforced the WEB LAYER's half of that sentence — if a
 * dependency ever grew a font CDN, an analytics beacon, or an update ping,
 * nothing would have caught it before a paying tutor's machine did.
 *
 * The API side (api.anthropic.com only) can't be observed from a browser
 * test; what CAN be is that the pages themselves dial no one. The API is not
 * running in this suite, so its fetches fail fast — fine: the assertion is
 * about WHERE requests go, never whether they succeed.
 */

const SEGMENTS = [
  "curricula",
  "library",
  "canon",
  "lessons",
  "artifacts",
  "chat",
  "settings",
] as const;

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

function collectExternal(page: Page): string[] {
  const external: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    // data:/blob: carry no host; everything else must be loopback.
    if (url.startsWith("data:") || url.startsWith("blob:")) return;
    const host = new URL(url).hostname;
    if (!LOCAL_HOSTS.has(host)) external.push(url);
  });
  return external;
}

for (const segment of SEGMENTS) {
  test(`/${segment} requests nothing outside localhost`, async ({ page }) => {
    const external = collectExternal(page);
    await page.goto(`/el/${segment}`);
    // A fixed settle window instead of networkidle: several pages poll the
    // (absent) API every 2s, so "idle" never arrives by design. Three
    // seconds is enough for fonts/scripts/beacons to have fired if any
    // existed.
    await page.waitForTimeout(3000);
    expect(external).toEqual([]);
  });
}
