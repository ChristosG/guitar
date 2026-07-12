import { test, expect } from "@playwright/test";

const collections = [{ id: "c1", name: "Tone & Gear", source_count: 2 }];
const sources = [
  { id: "s1", type: "pdf", title: "Getting Great Guitar Sounds",
    status: "ready", char_count: 91000, collection_id: "c1" },
  { id: "s2", type: "url", title: "Humbucker (Wikipedia)",
    status: "empty", char_count: 0, collection_id: "c1" },
  { id: "s3", type: "text", title: "Loose note", status: "ready",
    char_count: 337, collection_id: null },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/library/collections", (r) => r.fulfill({ json: collections }));
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: sources }));
});

test("groups sources under their collection, with Unfiled for the rest", async ({ page }) => {
  await page.goto("/en/library");
  await expect(page.getByTestId("collection-Tone & Gear")).toContainText("Getting Great Guitar Sounds");
  await expect(page.getByTestId("collection-unfiled")).toContainText("Loose note");
});

test("an empty source is shown as broken with a retry, NOT as healthy", async ({ page }) => {
  // regression: three real sources sat "ready" with 0 chars and rendered green
  await page.goto("/en/library");
  const row = page.getByTestId("source-s2");
  await expect(row).toContainText(/empty/i);
  await expect(row.getByTestId("retry-s2")).toBeVisible();
  await expect(row.getByTestId("status-ok-s2")).toHaveCount(0);
});

test("a stale 'ready' source with 0 chars is ALSO shown as broken, not just an explicit 'empty' status", async ({ page }) => {
  // regression: the real live API still has three pre-existing rows written
  // before the backend's D6 fix landed — they report status "ready" with
  // char_count 0 (the backfill was never run). The honesty requirement is
  // about the CONTENT having nothing readable, not merely the status label,
  // so this must render broken too.
  const staleSources = [
    ...sources,
    { id: "s4", type: "url", title: "Guitar amplifier (Wikipedia)",
      status: "ready", char_count: 0, collection_id: null },
  ];
  await page.route("**/knowledge/sources", (r) => r.fulfill({ json: staleSources }));
  await page.goto("/en/library");
  const row = page.getByTestId("source-s4");
  await expect(row).toContainText(/empty/i);
  await expect(row.getByTestId("retry-s4")).toBeVisible();
  await expect(row.getByTestId("status-ok-s4")).toHaveCount(0);
});

test("retry calls the API", async ({ page }) => {
  let called = false;
  await page.route("**/knowledge/sources/s2/retry", (r) => {
    called = true;
    return r.fulfill({ status: 202, json: { job_id: "j1" } });
  });
  await page.goto("/en/library");
  await page.getByTestId("retry-s2").click();
  await expect.poll(() => called).toBe(true);
});

test("a ready source links into the reader", async ({ page }) => {
  await page.goto("/en/library");
  await page.getByTestId("source-s1").getByRole("link").click();
  await expect(page).toHaveURL(/\/en\/library\/s1/);
});
