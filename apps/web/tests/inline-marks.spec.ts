import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import {
  parseInlineMarks,
  stripInlineMarks,
  toggleMark,
  type InlineNode,
} from "../src/lib/inline-marks";
import { fold } from "../src/lib/prose-diff";

// Node-only: no browser, no server — `inline-marks.ts` is deliberately pure, the
// same way `prose-diff.ts` is, so the grammar is tested in milliseconds.
//
// The parser cases are NOT written here. They live in
// `tests/fixtures/inline_marks_cases.json`, which is a byte-for-byte copy of
// `apps/api/tests/fixtures/inline_marks_cases.json`; the API suite parses the
// very same file with `app/curriculum/inline_marks.py`. That is the whole point:
// the board, the editor, the Word export and the word counter must agree about
// what `**…**` means, and a case table that lives in one language cannot make
// them agree.

type Case = { name: string; in: string; tree: InlineNode[]; stripped: string };

const FIXTURE = join(__dirname, "fixtures", "inline_marks_cases.json");
const cases: Case[] = JSON.parse(readFileSync(FIXTURE, "utf8")).cases;

test.describe("the shared case table", () => {
  test("is the same bytes as the API's copy", () => {
    const api = readFileSync(
      join(__dirname, "..", "..", "api", "tests", "fixtures", "inline_marks_cases.json"),
    );
    expect(readFileSync(FIXTURE).equals(api)).toBe(true);
  });

  test("still covers the cases the grammar was written for", () => {
    expect(cases.length).toBeGreaterThanOrEqual(12);
    const inputs = cases.map((c) => c.in);
    expect(inputs).toContain("***και τα δύο***");
    expect(inputs).toContain("2 * 3 * 4");
    expect(inputs).toContain("");
  });
});

for (const c of cases) {
  test(`parseInlineMarks — ${c.name}`, () => {
    expect(parseInlineMarks(c.in)).toEqual(c.tree);
  });

  test(`stripInlineMarks — ${c.name}`, () => {
    expect(stripInlineMarks(c.in)).toBe(c.stripped);
  });
}

test.describe("parse invariants", () => {
  test("no character is ever lost", () => {
    // Whatever the tolerance rules decide, the round trip of the TEXT nodes plus
    // the markers of every span that opened must be the input again — otherwise
    // saving a lesson would quietly eat the tutor's typing.
    const render = (nodes: InlineNode[]): string =>
      nodes
        .map((n) => {
          if (n.type === "text") return n.value;
          if (n.type === "u") return `<u>${render(n.children)}</u>`;
          if (n.type === "strong") return `**${render(n.children)}**`;
          return `*${render(n.children)}*`;
        })
        .join("");
    for (const c of cases) expect(render(parseInlineMarks(c.in))).toBe(c.in);
  });
});

test.describe("toggleMark", () => {
  test("wraps a selection and keeps the selection on the same words", () => {
    expect(toggleMark("Το σόλο", 3, 7, "strong")).toEqual({
      text: "Το **σόλο**",
      start: 5,
      end: 9,
    });
    expect(toggleMark("σόλο", 0, 4, "em")).toEqual({ text: "*σόλο*", start: 1, end: 5 });
    expect(toggleMark("σόλο", 0, 4, "u")).toEqual({
      text: "<u>σόλο</u>",
      start: 3,
      end: 7,
    });
  });

  test("unwraps when the selection is exactly inside the markers", () => {
    expect(toggleMark("Το **σόλο**", 5, 9, "strong")).toEqual({
      text: "Το σόλο",
      start: 3,
      end: 7,
    });
    expect(toggleMark("<u>σόλο</u>", 3, 7, "u")).toEqual({
      text: "σόλο",
      start: 0,
      end: 4,
    });
  });

  test("unwraps when the selection includes the markers", () => {
    expect(toggleMark("Το **σόλο**", 3, 11, "strong")).toEqual({
      text: "Το σόλο",
      start: 3,
      end: 7,
    });
  });

  test("an empty selection inside a word wraps the whole word", () => {
    expect(toggleMark("Το σόλο", 5, 5, "strong")).toEqual({
      text: "Το **σόλο**",
      start: 5,
      end: 9,
    });
  });

  test("a caret inside an already-bold word UNbolds it", () => {
    expect(toggleMark("**σόλο**", 4, 4, "strong")).toEqual({
      text: "σόλο",
      start: 0,
      end: 4,
    });
  });

  test("an empty selection on whitespace inserts an empty pair, caret between", () => {
    expect(toggleMark("Το σόλο ", 8, 8, "strong")).toEqual({
      text: "Το σόλο ****",
      start: 10,
      end: 10,
    });
    expect(toggleMark("", 0, 0, "u")).toEqual({ text: "<u></u>", start: 3, end: 3 });
  });

  test("only one layer comes off when a selection is wrapped twice", () => {
    expect(toggleMark("****x****", 4, 5, "strong")).toEqual({
      text: "**x**",
      start: 2,
      end: 3,
    });
  });

  test("italic over an already-bold word nests instead of eating the bold", () => {
    // The trap: `**σόλο**` has a single `*` on each side of the selection, so a
    // naive unwrap would turn bold into italic and lose the bold.
    const out = toggleMark("**σόλο**", 2, 6, "em");
    expect(out).toEqual({ text: "***σόλο***", start: 3, end: 7 });
    expect(parseInlineMarks(out.text)).toEqual([
      {
        type: "strong",
        children: [{ type: "em", children: [{ type: "text", value: "σόλο" }] }],
      },
    ]);
  });

  test("toggling twice returns the original text and selection", () => {
    for (const mark of ["strong", "em", "u"] as const) {
      const once = toggleMark("Το σόλο εδώ", 3, 7, mark);
      const twice = toggleMark(once.text, once.start, once.end, mark);
      expect(twice).toEqual({ text: "Το σόλο εδώ", start: 3, end: 7 });
    }
  });

  test("out-of-range offsets are clamped, not crashed on", () => {
    expect(toggleMark("σόλο", -5, 99, "strong")).toEqual({
      text: "**σόλο**",
      start: 2,
      end: 6,
    });
  });
});

test.describe("the diff ignores the markers", () => {
  // `fold` is what `diffProse` compares lines and paragraphs with. If it kept
  // the markers, bolding one word would report the whole paragraph as deleted
  // and re-added in «Τι άλλαξε;» — a screen of red and green for a Ctrl+B.
  test("bolding a word is not a change", () => {
    expect(fold("**λέξη**")).toBe(fold("λέξη"));
    expect(fold("*λέξη*")).toBe(fold("λέξη"));
    expect(fold("<u>λέξη</u>")).toBe(fold("λέξη"));
    expect(fold("Το **humbucker** έχει *δύο* πηνία <u>σε σειρά</u>")).toBe(
      fold("Το humbucker έχει δύο πηνία σε σειρά"),
    );
  });

  test("it still folds Greek the way it always did", () => {
    expect(fold("**Τονικότητα**")).toBe("τονικοτητα");
    expect(fold("ΦΩΣ")).toBe(fold("*φώς*"));
  });
});

// --- the board ------------------------------------------------------------
//
// Same route-interception convention as `what-changed.spec.ts`: the grammar is
// proved above in Node, so what is left to prove in a browser is that the board
// actually RENDERS it — real `<strong>`/`<em>`/`<u>` elements, not asterisks on
// the screen and not HTML injected into the page.
const API_ORIGIN = "http://localhost:8791";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};

const ROOT_ID = "33333333-3333-3333-3333-333333333333";
const MODULE_ID = "44444444-4444-4444-4444-444444444444";
const LESSON_ID = "55555555-5555-5555-5555-555555555555";
const SEGMENT_ID = "66666666-6666-6666-6666-666666666666";

const MARKED = "Το **humbucker** έχει *δύο* πηνία <u>σε σειρά</u>";

function node(over: Record<string, unknown>) {
  return {
    id: "x", kind: "segment", title: "τ", body: null, est_minutes: null, order: 0,
    language: "el", plane: "content", student_id: null, meta: null, children: [],
    ...over,
  };
}

async function mockTree(page: Page, segmentBody: string) {
  async function handler(route: Route) {
    const req = route.request();
    const { pathname } = new URL(req.url());
    if (req.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    const json = (status: number, body: unknown) =>
      route.fulfill({
        status, contentType: "application/json", headers: CORS_HEADERS,
        body: JSON.stringify(body),
      });

    if (pathname === `/curricula/${ROOT_ID}`) {
      await json(200, node({
        id: ROOT_ID, kind: "course", title: "Ήχος Κιθάρας", meta: { brief: null },
        children: [node({
          id: MODULE_ID, kind: "module", title: "Ενότητα 1", body: "Στόχος.",
          meta: { tier: "library" },
          children: [node({
            id: LESSON_ID, kind: "lesson", title: "Μάθημα 1",
            meta: { draft_status: "ready", word_count: 300 },
            children: [node({
              id: SEGMENT_ID, kind: "segment", title: "Θεωρία", body: segmentBody,
            })],
          })],
        })],
      }));
      return;
    }
    if (pathname.endsWith("/progress")) {
      await json(200, { root_id: ROOT_ID, total: 1, queued: 0, drafting: 0, ready: 1, failed: 0, done: true });
      return;
    }
    if (pathname === "/curricula/interview/open") {
      await json(200, null);
      return;
    }
    await json(500, { detail: "unexpected" });
  }

  await page.route(`${API_ORIGIN}/curricula/**`, handler);
  await page.route(`${API_ORIGIN}/curricula`, handler);
  await page.route(`${API_ORIGIN}/blocks/**`, handler);
}

/** Open the board and expand down to the segment card — non-root rows mount
 * collapsed. */
async function openSegment(page: Page) {
  await page.goto(`/el/curricula/${ROOT_ID}`);
  await expect(page.getByTestId("tree-board")).toBeVisible();
  await page.locator('[data-testid="block-card"][data-kind="module"]').getByTestId("block-card-toggle").click();
  await page.locator('[data-testid="block-card"][data-kind="lesson"]').getByTestId("block-card-toggle").click();
  await expect(page.locator('[data-testid="block-card"][data-kind="segment"]')).toBeVisible();
}

test.describe("the board renders the marks", () => {
  test("bold, italic and underline become real elements", async ({ page }) => {
    await mockTree(page, MARKED);
    await openSegment(page);

    const body = page
      .locator('[data-testid="block-card"][data-kind="segment"]')
      .getByTestId("block-card-body");
    await expect(body).toBeVisible();
    await expect(body.locator("strong")).toHaveText("humbucker");
    await expect(body.locator("em")).toHaveText("δύο");
    await expect(body.locator("u")).toHaveText("σε σειρά");
    // the markers themselves never reach the screen
    await expect(body).toHaveText("Το humbucker έχει δύο πηνία σε σειρά");
  });

  test("angle brackets in the tutor's prose stay text, never markup", async ({ page }) => {
    // The body is rendered from a parsed TREE, never with innerHTML — so a tag
    // the tutor typed (or pasted) is shown, not executed.
    await mockTree(page, "Γράψε <b>έντονα</b> με <script>alert(1)</script>");
    await openSegment(page);

    const body = page
      .locator('[data-testid="block-card"][data-kind="segment"]')
      .getByTestId("block-card-body");
    await expect(body).toHaveText("Γράψε <b>έντονα</b> με <script>alert(1)</script>");
    await expect(body.locator("b")).toHaveCount(0);
    await expect(body.locator("script")).toHaveCount(0);
  });
});
