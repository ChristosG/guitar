import { test, expect } from "@playwright/test";

test("serves the shell translated per locale", async ({ page }) => {
  await page.goto("/en");
  await expect(page.getByTestId("app-title")).toHaveText(/Guitar Tutor Copilot/);
  await page.goto("/el");
  await expect(page.getByTestId("app-title")).toHaveText(/Βοηθός Δασκάλου Κιθάρας/);
});

// no-store is a production/deploy concern (Cloudflare edge). The dev server
// (`next dev`) sets cache headers differently, so it's asserted in scripts/smoke.sh
// against the real standalone container, not here against dev.

test("theme toggle flips the html class (light default)", async ({ page }) => {
  await page.goto("/en");
  const html = page.locator("html");
  const before = await html.getAttribute("class");
  await page.getByTestId("theme-toggle").click();
  await expect(html).not.toHaveClass(before ?? "");
});

test.describe("system prefers dark", () => {
  test.use({ colorScheme: "dark" });
  test("first click flips away from resolved dark (needs resolvedTheme, not theme)", async ({ page }) => {
    await page.goto("/en");
    const html = page.locator("html");
    await expect(html).toHaveClass(/dark/); // system-dark resolved on load
    await page.getByTestId("theme-toggle").click();
    await expect(html).toHaveClass(/light/); // first click MUST switch to light
    await expect(html).not.toHaveClass(/dark/);
  });
});
