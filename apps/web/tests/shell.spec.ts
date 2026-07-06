import { test, expect } from "@playwright/test";

test("serves Greek and English shell", async ({ page }) => {
  await page.goto("/el");
  await expect(page.getByTestId("app-title")).toBeVisible();
  await page.goto("/en");
  await expect(page.getByTestId("app-title")).toBeVisible();
});

test("theme toggle flips the html class", async ({ page }) => {
  await page.goto("/en");
  const html = page.locator("html");
  const before = await html.getAttribute("class");
  await page.getByTestId("theme-toggle").click();
  await expect(html).not.toHaveClass(before ?? "");
});
