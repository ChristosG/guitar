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
    // Named, not counted: a `length >= 12` guard is satisfied by twelve cases
    // about nothing. The same list is asserted in `tests/test_inline_marks.py`.
    const required = [
      "plain text has no marks",
      "strong",
      "em",
      "underline",
      "em nested inside strong",
      "strong nested inside em",
      "nesting works both ways round",
      "strong nested inside underline",
      "triple markers are strong around em",
      "an opener with no closer is literal",
      "an unmatched closer is literal",
      "the empty strong pair the toolbar inserts",
      "the empty underline pair the toolbar inserts",
      "a bare pair of asterisks has no closer, so it is text",
      "a lone asterisk is literal",
      "arithmetic is not italics",
      "whitespace just inside the marker keeps it literal",
      "a span may cross a newline",
      "the empty string",
    ];
    const names = cases.map((c) => c.name);
    expect(required.filter((name) => !names.includes(name))).toEqual([]);
    expect(new Set(names).size).toBe(names.length);
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

  test("a long lesson full of lone asterisks parses in linear time", () => {
    // 15k characters, 5000 asterisks, not one of them a mark. The naive scanner
    // re-searched the whole tail for every one of them; in Python that was 5.6
    // seconds, and the word count runs on save.
    const text = "*a ".repeat(5000);
    const started = Date.now();
    const tree = parseInlineMarks(text);
    expect(Date.now() - started).toBeLessThan(1000);
    expect(tree).toEqual([{ type: "text", value: text }]);
  });

  test("twenty thousand asterisks parse, capped at MAX_DEPTH", () => {
    // The Python twin used to raise RecursionError at ~4k asterisks while this
    // side parsed on — the worst kind of divergence. These are the SAME
    // assertions as `tests/test_inline_marks.py`'s.
    const text = "*".repeat(20000);
    const depth = (nodes: InlineNode[], at = 0): number =>
      nodes.reduce(
        (deepest, n) =>
          n.type === "text" ? deepest : Math.max(deepest, depth(n.children, at + 1)),
        at,
      );
    const started = Date.now();
    const tree = parseInlineMarks(text);
    expect(Date.now() - started).toBeLessThan(1000);
    expect(depth(tree)).toBe(32);
    expect(stripInlineMarks(text).length).toBe(20000 - 32 * 4);
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

  test("a selection that STARTS inside a marker still unwraps", () => {
    // Dragged from between the two asterisks to the middle of the word. Without
    // snapping the selection out of the run first, this bolded the bold:
    // `****ab**c**`.
    expect(toggleMark("**abc**", 1, 4, "strong")).toEqual({
      text: "abc",
      start: 0,
      end: 3,
    });
    expect(toggleMark("Το **σόλο**", 4, 9, "strong")).toEqual({
      text: "Το σόλο",
      start: 3,
      end: 7,
    });
    expect(toggleMark("<u>σόλο</u>", 1, 6, "u")).toEqual({
      text: "σόλο",
      start: 0,
      end: 4,
    });
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

  test("un-italicising a bold-italic word keeps the bold", () => {
    // The trap is PARITY. `***σόλο***` is strong(em(…)); the run on each side is
    // three asterisks, of which exactly one is the em's. Reading "there is a
    // `**` next to the selection" as "the neighbours are not mine" wrapped the
    // word again into `****σόλο****` — which parses as two EMPTY strongs around
    // it: the bold silently gone, eight invisible asterisks left in the lesson.
    const out = toggleMark("***σόλο***", 3, 7, "em");
    expect(out).toEqual({ text: "**σόλο**", start: 2, end: 6 });
    expect(parseInlineMarks(out.text)).toEqual([
      { type: "strong", children: [{ type: "text", value: "σόλο" }] },
    ]);

    // …and the same when the tutor selected the markers too.
    const outside = toggleMark("***σόλο***", 0, 10, "em");
    expect(outside.text).toBe("**σόλο**");
    expect(parseInlineMarks(outside.text)).toEqual([
      { type: "strong", children: [{ type: "text", value: "σόλο" }] },
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

// --- the editor -----------------------------------------------------------
//
// The grammar above says what `**…**` MEANS; the board section says it renders.
// What is left is the only part the tutor actually touches: the three buttons
// over the textarea, the three shortcuts, and the fact that his typing is saved
// whether or not he finds the Save button. The API is mocked and every
// `PATCH /blocks/{id}` is recorded, because the whole risk of an autosave is the
// PATCH you did not mean to send — one per pause, none while idle, and never a
// second one for the same text when the blur and the Save click arrive together.

const SEGMENT_TEXT = "Το σόλο ξεκινά αργά";

type Patch = { body: string };

/** The board, editable: GETs come from memory and every PATCH lands in `patches`.
 *
 * `patchDelayMs` is the whole point of the second half of these tests. A PATCH
 * that answers instantly hides the window that matters — the one between the
 * request leaving and the response landing, where the tutor's next click
 * arrives. Held open for 600 ms, a Save or a Cancel during a save is something
 * a test can actually aim at. */
async function mockEditor(page: Page, patches: Patch[], patchDelayMs = 0) {
  let segmentBody = SEGMENT_TEXT;

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

    if (req.method() === "PATCH" && pathname === `/blocks/${SEGMENT_ID}`) {
      const payload = (req.postDataJSON() ?? {}) as { body?: string };
      // Recorded on ARRIVAL, before the delay — so the test can see a write is
      // in flight, which is exactly the state it is trying to interrupt.
      patches.push({ body: payload.body ?? "" });
      segmentBody = payload.body ?? "";
      if (patchDelayMs) await new Promise((r) => setTimeout(r, patchDelayMs));
      await json(200, node({ id: SEGMENT_ID, kind: "segment", title: "Θεωρία", body: segmentBody }));
      return;
    }
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

/** Open the board, expand down to the segment, and put its body into edit mode. */
async function openEditor(page: Page) {
  await openSegment(page);
  const segment = page.locator('[data-testid="block-card"][data-kind="segment"]');
  await segment.getByTestId("body-edit-trigger").click();
  await expect(segment.getByTestId("body-edit-textarea")).toBeVisible();
  return segment;
}

/** Put the caret/selection where a tutor's mouse would have put it. */
async function select(textarea: ReturnType<Page["getByTestId"]>, start: number, end: number) {
  await textarea.evaluate((el, [s, e]: [number, number]) => {
    const ta = el as HTMLTextAreaElement;
    ta.focus();
    ta.setSelectionRange(s, e);
  }, [start, end] as [number, number]);
}

test.describe("editor", () => {
  test("the B button bolds the selection and gives the textarea back", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await select(textarea, 3, 7); // «σόλο»
    await segment.getByTestId("mark-bold").click();

    await expect(textarea).toHaveValue("Το **σόλο** ξεκινά αργά");
    // The tutor keeps typing where he was — the button must never steal focus.
    await expect(textarea).toBeFocused();
  });

  test("Ctrl+I italicises the selection", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await select(textarea, 3, 7);
    await page.keyboard.press("Control+i");

    await expect(textarea).toHaveValue("Το *σόλο* ξεκινά αργά");
    await expect(textarea).toBeFocused();
  });

  test("a pause in the typing saves, once, with the editor still open", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");
    const typed = `${SEGMENT_TEXT} και δυναμώνει`;

    await textarea.fill(typed);
    await page.waitForTimeout(2200);

    expect(patches).toEqual([{ body: typed }]);
    // Autosave is not the Save button: the box stays open and usable while it runs.
    await expect(textarea).toBeVisible();
    await expect(textarea).toBeEnabled();
    await expect(segment.getByTestId("body-edit-status")).toHaveText("Αποθηκεύτηκε");
  });

  test("clicking away flushes the draft without waiting for the debounce", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");
    const typed = `${SEGMENT_TEXT} με σιγουριά`;

    await textarea.fill(typed);
    await segment.getByTestId("block-card-title").click();

    // Under the 1500 ms debounce, so only a blur-flush can explain the PATCH.
    await expect.poll(() => patches.length, { timeout: 1000 }).toBe(1);
    expect(patches[0].body).toBe(typed);
  });

  test("Cancel puts back the text the editor opened with", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await textarea.fill(`${SEGMENT_TEXT} — λάθος`);
    await page.waitForTimeout(2200);
    expect(patches).toHaveLength(1);

    await segment.getByTestId("body-edit-cancel").click();

    await expect(segment.getByTestId("body-edit-textarea")).toHaveCount(0);
    await expect.poll(() => patches.length).toBe(2);
    expect(patches[1].body).toBe(SEGMENT_TEXT);
    await expect(segment.getByTestId("block-card-body")).toHaveText(SEGMENT_TEXT);
  });

  test("Save sends the draft exactly once and closes the editor", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");
    const typed = `${SEGMENT_TEXT} **δυνατά**`;

    await textarea.fill(typed);
    await segment.getByTestId("body-edit-save").click();

    await expect(segment.getByTestId("body-edit-textarea")).toHaveCount(0);
    await expect.poll(() => patches.length).toBe(1);
    expect(patches[0].body).toBe(typed);
    // The blur the Save click causes and the Save itself are ONE PATCH, not two.
    await page.waitForTimeout(2200);
    expect(patches).toHaveLength(1);
    await expect(segment.getByTestId("block-card-body")).toHaveText(`${SEGMENT_TEXT} δυνατά`);
  });

  test("two shortcuts back to back nest, instead of the second reading stale offsets", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await select(textarea, 3, 7); // «σόλο»
    await page.keyboard.press("Control+b");
    await page.keyboard.press("Control+i");

    await expect(textarea).toHaveValue("Το ***σόλο*** ξεκινά αργά");
    const picked = await textarea.evaluate((el) => {
      const ta = el as HTMLTextAreaElement;
      return ta.value.slice(ta.selectionStart, ta.selectionEnd);
    });
    expect(picked).toBe("σόλο");
  });

  test("Ctrl+Shift+B is the browser's, not ours", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await select(textarea, 3, 7);
    await page.keyboard.press("Control+Shift+b");

    await expect(textarea).toHaveValue(SEGMENT_TEXT);
  });

  test("Save during an autosave does not send the same body twice", async ({ page }) => {
    // The window the whole chain exists for: the debounce has fired, the PATCH
    // is still out, and `lastSavedRef` — written only when the response lands —
    // still says the text is unsaved. Without `savingTextRef` the Save click's
    // blur sends an identical second PATCH, and whichever response arrives last
    // wins.
    const patches: Patch[] = [];
    await mockEditor(page, patches, 600);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");
    const typed = `${SEGMENT_TEXT} α`;

    await textarea.fill(typed);
    await page.waitForTimeout(1600);
    expect(patches).toHaveLength(1); // in flight, held by the route

    await segment.getByTestId("body-edit-save").click();
    await expect(segment.getByTestId("body-edit-textarea")).toHaveCount(0);
    await page.waitForTimeout(1500);

    const bodies = patches.map((p) => p.body);
    expect(new Set(bodies).size).toBe(bodies.length); // no body written twice
    expect(bodies).toEqual([typed]);
    await expect(segment.getByTestId("block-card-body")).toHaveText(typed);
  });

  test("Cancel during an autosave lands LAST, with the original text", async ({ page }) => {
    const patches: Patch[] = [];
    await mockEditor(page, patches, 600);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await textarea.fill(`${SEGMENT_TEXT} — λάθος`);
    await page.waitForTimeout(1600);
    expect(patches).toHaveLength(1); // in flight

    await segment.getByTestId("body-edit-cancel").click();
    await expect(segment.getByTestId("body-edit-textarea")).toHaveCount(0);

    await expect.poll(() => patches.length, { timeout: 5000 }).toBe(2);
    // The restore is queued BEHIND the save it undoes — never before it.
    expect(patches[patches.length - 1].body).toBe(SEGMENT_TEXT);
    await expect(segment.getByTestId("block-card-body")).toHaveText(SEGMENT_TEXT);
  });

  test("Cancel before the debounce fires writes nothing at all", async ({ page }) => {
    // Nothing had been saved yet, so Cancel must not save-then-undo: two PATCHes
    // for an abandoned edit also stamp a «επεξεργασμένο από σένα» that is a lie.
    const patches: Patch[] = [];
    await mockEditor(page, patches);
    const segment = await openEditor(page);
    const textarea = segment.getByTestId("body-edit-textarea");

    await textarea.fill(`${SEGMENT_TEXT} — λάθος`);
    await segment.getByTestId("body-edit-cancel").click();

    await expect(segment.getByTestId("body-edit-textarea")).toHaveCount(0);
    await page.waitForTimeout(2200); // long past the debounce that was cancelled
    expect(patches).toEqual([]);
    await expect(segment.getByTestId("block-card-body")).toHaveText(SEGMENT_TEXT);
  });
});
