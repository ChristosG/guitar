import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect } from "@playwright/test";
import {
  parseInlineMarks,
  stripInlineMarks,
  toggleMark,
  type InlineNode,
} from "../src/lib/inline-marks";

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
