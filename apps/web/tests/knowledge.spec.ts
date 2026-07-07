import { randomUUID } from "node:crypto";
import { test, expect, type Page } from "@playwright/test";

// Deterministic, offline coverage of the Knowledge cockpit page: every
// `/knowledge/*` call is intercepted via `page.route` and answered from an
// in-memory fixture — no backend needs to be running. This asserts the UI
// wiring (form -> POST -> list refresh; search box -> ranked snippets; ask
// box -> grounded answer + citations), not the model/retrieval behavior
// itself (that's covered by the API's own integration tests).

interface FixtureSource {
  id: string;
  title: string;
  type: string;
  status: string;
  domain: string | null;
  language: string | null;
  char_count: number | null;
  error: string | null;
  created_at: string;
}

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

function seedSource(overrides: Partial<FixtureSource> = {}): FixtureSource {
  return {
    id: randomUUID(),
    title: "Getting Great Guitar Sounds",
    type: "pdf",
    status: "ready",
    domain: "tone",
    language: "en",
    char_count: 12345,
    error: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

/** Wires up an in-memory mock of the 4 `/knowledge/*` endpoints this page
 * calls (sources list/create/delete, search, ask), all via one `page.route`
 * so precise URL-glob overlap (e.g. `/sources` vs `/sources/upload`) is
 * never a concern — matching is done on `pathname` instead. Returns the
 * mutable fixture state plus the last request body seen per endpoint, so
 * tests can assert both the rendered UI *and* that the API was actually
 * called with the right payload. Any request this doesn't recognize is
 * recorded (not silently proxied to the real network) and fulfilled with a
 * 500, so a routing mistake fails the test loudly instead of leaking to a
 * real backend.
 */
async function mockKnowledgeApi(page: Page, initialSources: FixtureSource[] = []) {
  const sources = [...initialSources];
  const calls = { list: 0, create: 0, delete: 0, search: 0, ask: 0 };
  const lastBody: { create?: unknown; search?: unknown; ask?: unknown } = {};
  const unexpected: string[] = [];

  await page.route("**/knowledge/**", async (route) => {
    const req = route.request();
    const method = req.method();
    const { pathname } = new URL(req.url());

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/knowledge/sources" && method === "GET") {
      calls.list++;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(sources),
      });
      return;
    }

    if (pathname === "/knowledge/sources" && method === "POST") {
      calls.create++;
      const payload = req.postDataJSON() as {
        kind: string;
        title: string;
        domain?: string;
        language?: string;
        text?: string;
        url?: string;
      };
      lastBody.create = payload;
      const created = seedSource({
        title: payload.title,
        type: payload.kind,
        domain: payload.domain ?? null,
        language: payload.language ?? null,
        char_count: (payload.text ?? payload.url ?? "").length,
      });
      sources.unshift(created);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(created),
      });
      return;
    }

    const deleteMatch = pathname.match(/^\/knowledge\/sources\/([^/]+)$/);
    if (deleteMatch && method === "DELETE") {
      calls.delete++;
      const idx = sources.findIndex((s) => s.id === deleteMatch[1]);
      if (idx >= 0) sources.splice(idx, 1);
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (pathname === "/knowledge/search" && method === "POST") {
      calls.search++;
      lastBody.search = req.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          hits: [
            {
              chunk_id: randomUUID(),
              source_id: sources[0]?.id ?? randomUUID(),
              source_title: "Getting Great Guitar Sounds",
              text: "A humbucker pickup cancels 60-cycle hum by combining two coils wound in opposite polarity.",
              section_path: "Chapter 2: Pickups",
              page: 14,
              score: 0.83,
            },
          ],
        }),
      });
      return;
    }

    if (pathname === "/knowledge/ask" && method === "POST") {
      calls.ask++;
      lastBody.ask = req.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify({
          text: "A humbucker is a pickup that cancels 60-cycle hum by combining two coils in opposite polarity [1].",
          citations: [
            {
              chunk_id: randomUUID(),
              source_id: sources[0]?.id ?? randomUUID(),
              source_title: "Getting Great Guitar Sounds",
              text: "A humbucker pickup cancels 60-cycle hum by combining two coils wound in opposite polarity.",
              section_path: "Chapter 2: Pickups",
              page: 14,
              score: 0.91,
            },
          ],
        }),
      });
      return;
    }

    unexpected.push(`${method} ${pathname}`);
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: CORS_HEADERS,
      body: JSON.stringify({ detail: "unmocked request in test" }),
    });
  });

  return { sources, calls, lastBody, unexpected };
}

test.describe("knowledge cockpit (mocked API)", () => {
  test("shows the source list, adds a source, searches, and asks", async ({ page }) => {
    const seed = seedSource();
    const mock = await mockKnowledgeApi(page, [seed]);

    await page.goto("/en/knowledge");

    // The seeded source is shown on load (GET /knowledge/sources was called).
    await expect(page.getByTestId("source-list")).toBeVisible();
    const seedRow = page.getByTestId("source-item").filter({ hasText: seed.title });
    await expect(seedRow).toBeVisible();
    await expect(seedRow.getByTestId("status-badge")).toHaveText(/Ready/);
    expect(mock.calls.list).toBeGreaterThanOrEqual(1);

    // Add a text source -> POST /knowledge/sources -> list refresh shows it, ready.
    await page.getByTestId("add-source-mode-text").click();
    await page.getByTestId("add-source-title").fill("A humbucker cancels hum");
    await page.getByTestId("add-source-text").fill("A humbucker cancels hum.");
    await page.getByTestId("add-source-submit").click();

    const newRow = page.getByTestId("source-item").filter({ hasText: "A humbucker cancels hum" });
    await expect(newRow).toBeVisible();
    await expect(newRow.getByTestId("status-badge")).toHaveText(/Ready/);
    expect(mock.calls.create).toBe(1);
    expect((mock.lastBody.create as { kind: string; title: string; text?: string }).kind).toBe("text");
    expect((mock.lastBody.create as { kind: string; title: string; text?: string }).title).toBe(
      "A humbucker cancels hum",
    );

    // Search -> POST /knowledge/search -> a ranked snippet appears.
    await page.getByTestId("search-input").fill("hum");
    await page.getByTestId("search-submit").click();
    await expect(page.getByTestId("search-results")).toBeVisible();
    await expect(page.getByTestId("search-result-item")).toHaveCount(1);
    await expect(page.getByTestId("search-result-item").first()).toContainText("humbucker");
    expect((mock.lastBody.search as { query: string }).query).toBe("hum");

    // Ask -> POST /knowledge/ask -> the grounded answer + its citation appear.
    await page.getByTestId("ask-input").fill("What is a humbucker?");
    await page.getByTestId("ask-submit").click();
    await expect(page.getByTestId("ask-answer")).toContainText("humbucker");
    await expect(page.getByTestId("ask-citation-item")).toHaveCount(1);
    expect((mock.lastBody.ask as { query: string; locale: string }).locale).toBe("en");

    expect(mock.unexpected).toEqual([]);
  });

  test("deletes a source from the list", async ({ page }) => {
    const seed = seedSource({ title: "Delete Me" });
    const mock = await mockKnowledgeApi(page, [seed]);

    await page.goto("/en/knowledge");

    const row = page.getByTestId("source-item").filter({ hasText: "Delete Me" });
    await expect(row).toBeVisible();

    await row.getByTestId("source-delete").click();
    await expect(page.getByTestId("source-item").filter({ hasText: "Delete Me" })).toHaveCount(0);
    expect(mock.calls.delete).toBe(1);
    expect(mock.unexpected).toEqual([]);
  });
});
