import { test, expect } from "@playwright/test";

const page47 = {
  id: "p47", page_no: 47, status: "ready", total_pages: 77,
  image_url: "/media/pages/p47.jpg",
  text: "The Tube Screamer is not really a distortion box so much as a mid-range hump.",
};

test.beforeEach(async ({ page }) => {
  await page.route("**/knowledge/sources/s1/pages/47", (r) => r.fulfill({ json: page47 }));
  await page.route("**/knowledge/sources/s1/pages/48", (r) =>
    r.fulfill({ json: { ...page47, id: "p48", page_no: 48, text: "Next page." } }));
  await page.route("**/media/pages/*.jpg", (r) =>
    r.fulfill({ contentType: "image/jpeg", body: Buffer.from("") }));
});

test("shows the scan beside its selectable text", async ({ page }) => {
  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("page-scan")).toBeVisible();
  await expect(page.getByTestId("page-text")).toContainText("Tube Screamer");
  await expect(page.getByTestId("page-indicator")).toContainText("47");
  await expect(page.getByTestId("page-indicator")).toContainText("77");
});

test("paging forward loads the next page", async ({ page }) => {
  await page.goto("/en/library/s1?page=47");
  await page.getByTestId("page-next").click();
  await expect(page.getByTestId("page-text")).toContainText("Next page.");
});

test("selecting text reveals 'author a lesson' and posts the selection with its page", async ({ page }) => {
  let body: Record<string, unknown> | null = null;
  await page.route("**/lessons/from-selection", (r) => {
    body = r.request().postDataJSON();
    return r.fulfill({ status: 201, json: {
      selection_id: "sel1", source_id: "s1", source_title: "Book",
      page_no: 47, text: "mid-range hump" } });
  });

  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("page-text")).toContainText("Tube Screamer");
  await page.evaluate(() => {
    const el = document.querySelector('[data-testid="page-text"]')!;
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection()!;
    sel.removeAllRanges();
    sel.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });

  await page.getByTestId("author-lesson").click();
  await expect.poll(() => body?.page_no).toBe(47);       // provenance travels
  await expect.poll(() => body?.source_id).toBe("s1");
});

test("there is NO OCR editing affordance", async ({ page }) => {
  // spec D5 — the tutor must never be handed the machine's chores
  await page.goto("/en/library/s1?page=47");
  await expect(page.getByTestId("fix-ocr")).toHaveCount(0);
  await expect(page.getByTestId("page-text")).not.toHaveAttribute("contenteditable", "true");
});
