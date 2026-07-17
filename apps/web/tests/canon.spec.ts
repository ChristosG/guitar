import { test, expect } from "@playwright/test";

/** C7 — the tutor can SEE the canon. These specs mock `GET /canon/concepts`
 * (browse) and `POST /knowledge/concepts/search` (C8's search route) the same way
 * `library.spec.ts` mocks the sources list, and assert the ONE thing that must not
 * regress: DIVERGENCE IS THE HEADLINE. A canon view that renders two disagreeing
 * authors as one blended line, or hides the disagreement below the consensus, has
 * missed the entire point of owning ten books. */

const divergentConcept = {
  concept_id: "cc1",
  key: "pickup-height",
  label_en: "Pickup height",
  label_el: "Ύψος μαγνήτη",
  score: 0,
  coverage: 2,
  divergence: true,
  positions: [
    {
      kind: "divergence",
      position: "warns a low pickup kills sustain",
      books: ["Tone Manual (Hunter)"],
      citations: [
        {
          source_id: "s-hunter",
          source_title: "Tone Manual (Hunter)",
          pages: [113],
          pages_label: "p.113",
          grounding: "author",
        },
      ],
    },
    {
      kind: "divergence",
      position: "prefers a lower treble-side height",
      books: ["Guitar Tone (Gallagher)"],
      citations: [
        {
          source_id: "s-gallagher",
          source_title: "Guitar Tone (Gallagher)",
          pages: [201],
          pages_label: "p.201",
          grounding: "author",
        },
      ],
    },
  ],
};

const consensusConcept = {
  concept_id: "cc2",
  key: "string-gauge",
  label_en: "String gauge and tone",
  label_el: null,
  score: 0,
  coverage: 2,
  divergence: false,
  positions: [
    {
      kind: "consensus",
      position: "heavier strings, fuller tone",
      books: ["Book A", "Book B"],
      citations: [
        { source_id: "s-a", source_title: "Book A", pages: [5], pages_label: "p.5", grounding: "author" },
        { source_id: "s-b", source_title: "Book B", pages: [5], pages_label: "p.5", grounding: "author" },
      ],
    },
  ],
};

const figureConcept = {
  concept_id: "cc3",
  key: "caged",
  label_en: "The CAGED system",
  label_el: null,
  score: 0,
  coverage: 1,
  divergence: false,
  positions: [
    {
      kind: "only_in",
      position: "five interlocking shapes",
      books: ["Fretboard Workbook"],
      citations: [
        {
          source_id: "s-fb",
          source_title: "Fretboard Workbook",
          pages: [15],
          pages_label: "p.15",
          grounding: "figure",
        },
      ],
    },
  ],
};

const overview = {
  concepts: [divergentConcept, consensusConcept, figureConcept],
  total_concepts: 3,
  divergence_count: 1,
  books_compiled: 2,
  books_compiling: 1,
};

function routeCanon(json: unknown) {
  return (page: import("@playwright/test").Page) =>
    page.route("**/canon/concepts", (r) => r.fulfill({ json }));
}

test("the canon is reachable from the nav", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.goto("/en/library");
  await page.getByTestId("nav-canon").click();
  await expect(page).toHaveURL(/\/en\/canon/);
  await expect(page.getByTestId("canon-heading")).toBeVisible();
});

test("browse lists concepts, DIVERGENCE first and flagged", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.goto("/en/canon");

  // The divergent concept renders its disagreement as the headline block, with a
  // badge — and BOTH positions survive, never averaged into one line.
  const card = page.getByTestId("concept-pickup-height");
  await expect(card.getByTestId("divergence-badge")).toBeVisible();
  await expect(card.getByTestId("divergence-block")).toContainText("warns a low pickup kills sustain");
  await expect(card.getByTestId("divergence-block")).toContainText("prefers a lower treble-side height");

  // Divergence sorts ahead of consensus in the list.
  const cards = page.getByTestId("canon-list").locator("article");
  await expect(cards.first()).toHaveAttribute("data-testid", "concept-pickup-height");
});

test("a divergence citation is a chip that deep-links into the Reader", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.goto("/en/canon");
  const chip = page
    .getByTestId("concept-pickup-height")
    .getByTestId("concept-citation-chip")
    .first();
  await expect(chip).toHaveAttribute("href", "/en/library/s-hunter?page=113");
  await expect(chip).toContainText("p.113");
});

test("the overview shows how many books, concepts, and DISAGREEMENTS", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.goto("/en/canon");
  const bar = page.getByTestId("canon-overview");
  await expect(bar).toContainText("2 books in the canon");
  await expect(bar).toContainText("3 concepts");
  await expect(bar).toContainText("1 points of disagreement");
});

test("the 'only where they disagree' filter hides the consensus concepts", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.goto("/en/canon");
  await expect(page.getByTestId("concept-string-gauge")).toBeVisible();
  await page.getByTestId("canon-divergent-toggle").click();
  await expect(page.getByTestId("concept-pickup-height")).toBeVisible();
  await expect(page.getByTestId("concept-string-gauge")).toHaveCount(0);
});

test("a figure-grounded citation is marked, never presented as the author's words", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.goto("/en/canon");
  const chip = page
    .getByTestId("concept-caged")
    .getByTestId("concept-citation-chip")
    .first();
  await expect(chip).toHaveAttribute("data-grounding", "figure");
  await expect(chip).toContainText(/figure/i);
});

test("search over the canon shows cross-book concept hits", async ({ page }) => {
  await routeCanon(overview)(page);
  await page.route("**/knowledge/concepts/search", (r) =>
    r.fulfill({ json: { hits: [divergentConcept] } }),
  );
  await page.goto("/en/canon");
  await page.getByTestId("canon-search-input").fill("pickup height");
  await page.getByTestId("canon-search-submit").click();
  const results = page.getByTestId("canon-search-results");
  await expect(results.getByTestId("concept-pickup-height")).toBeVisible();
  await expect(results.getByTestId("divergence-block")).toBeVisible();
});

test("an empty canon says how to fill it, not an error", async ({ page }) => {
  await routeCanon({
    concepts: [],
    total_concepts: 0,
    divergence_count: 0,
    books_compiled: 0,
    books_compiling: 0,
  })(page);
  await page.goto("/en/canon");
  await expect(page.getByTestId("canon-empty")).toBeVisible();
  await expect(page.getByTestId("canon-empty").getByRole("link")).toHaveAttribute("href", "/en/library");
  await expect(page.getByTestId("canon-error")).toHaveCount(0);
});

test("a canon route not yet deployed reads as 'being built', not a crash", async ({ page }) => {
  await page.route("**/canon/concepts", (r) => r.fulfill({ status: 404, json: { detail: "Not Found" } }));
  await page.goto("/en/canon");
  await expect(page.getByTestId("canon-empty")).toBeVisible();
  await expect(page.getByTestId("canon-error")).toHaveCount(0);
});
