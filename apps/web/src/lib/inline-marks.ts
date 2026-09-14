// The inline-mark grammar — the TypeScript half.
//
// The tutor's lesson text is stored as PLAIN STRINGS. Bold, italic and underline
// live in the string itself as three markers: `**έντονο**`, `*πλάγιο*` and
// `<u>υπογράμμιση</u>`. Nothing else is markup: a lesson body is not Markdown
// and must never be fed to a Markdown renderer, because the tutor writes `#`,
// `-`, `1.` and `_` as ordinary characters and expects to read them back.
//
// This module is the ONLY place the grammar is written in TypeScript. Its twin
// is `apps/api/app/curriculum/inline_marks.py`, and the two are pinned against
// each other by ONE case table — `tests/fixtures/inline_marks_cases.json`, whose
// bytes the API suite compares with its own copy. What renders on the board must
// be exactly what lands in the Word export and what the word counter counts;
// two hand-written parsers drift the moment they are allowed to.
//
// The rules, in the order the scanner applies them:
//   1. Scan left to right. `<u>` is tried first, then `**`, then `*`.
//   2. A marker OPENS a span only if a matching closer exists later (inside the
//      current span, when nested). Otherwise it is literal text — an unclosed
//      `**` must not swallow the rest of the lesson.
//   3. An unmatched closer is literal. `**…**` and `<u>…</u>` may be EMPTY (the
//      toolbar inserts `****` and `<u></u>` when you press Ctrl+B on a blank
//      line — those must read as an empty span, not as stray asterisks); a `*`
//      span must have content, which is what keeps `**x` literal.
//   4. For `*`/`**`: whitespace immediately INSIDE the marker makes it literal.
//      This is what keeps «2 * 3 * 4» text instead of italics, and it is why
//      `* x*` stays as typed.
//   5. A closer that sits in a run of asterisks is taken at the END of the run,
//      which is what turns `***και τα δύο***` into strong(em(...)) rather than
//      strong("*και τα δύο") plus a stray asterisk — EXCEPT when that run opens
//      a `**` span of its own, which the em then skips whole, so that
//      `*a **b** c*` nests the same way `**a *b* c**` does.
//   6. Newlines are ordinary characters: a span may cross one.
//   7. Nesting stops at MAX_DEPTH. Past it every marker is literal text, so a
//      pathological string of asterisks can neither blow the Python twin's
//      recursion limit (it did, at ~4k) nor build a tree nobody can render.

export type InlineNode =
  | { type: "text"; value: string }
  | { type: "strong" | "em" | "u"; children: InlineNode[] };

export type MarkName = "strong" | "em" | "u";

type Marker = { mark: MarkName; open: string; close: string };

// Order matters: `**` MUST be tried before `*`.
const MARKERS: readonly Marker[] = [
  { mark: "u", open: "<u>", close: "</u>" },
  { mark: "strong", open: "**", close: "**" },
  { mark: "em", open: "*", close: "*" },
];

const STRONG: Marker = MARKERS[1];

/** The same cap on both sides — see rule 7. */
const MAX_DEPTH = 32;

// Spelled out rather than taken from a regex/`isspace()`, because the Python
// twin must agree character for character.
const SPACE = " \t\n\r\f\v\u00a0";

function isSpace(ch: string | undefined): boolean {
  return ch !== undefined && SPACE.includes(ch);
}

function markerFor(mark: MarkName): Marker {
  const found = MARKERS.find((m) => m.mark === mark);
  if (!found) throw new Error(`unknown mark: ${mark}`);
  return found;
}

/**
 * Per parse range: the offset from which a closer has already been PROVEN
 * absent. Without it the scanner is quadratic — 20k characters of `*a ` took
 * 5.6s in Python, because every one of the 5000 asterisks re-scanned the whole
 * tail looking for the closer the one before it had just failed to find. The
 * search only ever gets stricter as `from` grows, so one failure settles every
 * later opener in the same range.
 */
type Absent = Map<string, number>;

function rememberAbsent(absent: Absent, close: string, from: number): void {
  const known = absent.get(close);
  if (known === undefined || from < known) absent.set(close, from);
}

/** The closer of the `**` span opening at `runStart`, or null if none opens. */
function strongSpanCloser(
  text: string,
  runStart: number,
  end: number,
  absent: Absent,
): number | null {
  const contentStart = runStart + 2;
  if (contentStart >= end || isSpace(text[contentStart])) return null;
  return findCloser(text, contentStart, end, STRONG, absent);
}

/** Position of the closer for `marker` inside [from, end), or null. */
function findCloser(
  text: string,
  from: number,
  end: number,
  marker: Marker,
  absent: Absent,
): number | null {
  const { close } = marker;
  const provenAbsentFrom = absent.get(close);
  if (provenAbsentFrom !== undefined && from >= provenAbsentFrom) return null;

  let j = from;
  while (j + close.length <= end) {
    if (!text.startsWith(close, j)) {
      j++;
      continue;
    }
    if (close === "*") {
      let runEnd = j;
      while (runEnd < end && text[runEnd] === "*") runEnd++;
      if (runEnd - j >= 2) {
        // A `**` span INSIDE this em — `*a **b** c*`. Step over it whole;
        // right-aligning into its opener would end the em mid-marker.
        const inner = strongSpanCloser(text, j, end, absent);
        if (inner !== null) {
          j = inner + 2;
          continue;
        }
      }
      const at = runEnd - 1; // rule 5
      if (at > from && !isSpace(text[at - 1])) return at; // an em is never empty
      j = runEnd;
      continue;
    }
    let at = j;
    if (close === "**") {
      let runEnd = j;
      while (runEnd < end && text[runEnd] === "*") runEnd++;
      at = runEnd - 2; // rule 5
      if (isSpace(text[at - 1])) {
        j = runEnd;
        continue;
      }
    }
    return at; // `**…**` and `<u>…</u>` may be empty — rule 3
  }
  rememberAbsent(absent, close, from);
  return null;
}

function parseRange(text: string, start: number, end: number, depth: number): InlineNode[] {
  const nodes: InlineNode[] = [];
  const absent: Absent = new Map();
  let buf = "";
  const flush = () => {
    if (buf) {
      nodes.push({ type: "text", value: buf });
      buf = "";
    }
  };

  let i = start;
  outer: while (i < end) {
    if (depth < MAX_DEPTH) {
      for (const m of MARKERS) {
        if (i + m.open.length > end || !text.startsWith(m.open, i)) continue;
        const contentStart = i + m.open.length;
        if (m.mark !== "u" && (contentStart >= end || isSpace(text[contentStart]))) {
          // Rule 4. The marker is literal, and the shorter marker inside it does
          // NOT get a second chance — otherwise «** x**» would open an em.
          buf += m.open;
          i = contentStart;
          continue outer;
        }
        const closerAt = findCloser(text, contentStart, end, m, absent);
        if (closerAt === null) continue; // rule 2 — try the next, shorter marker
        flush();
        nodes.push({
          type: m.mark,
          children: parseRange(text, contentStart, closerAt, depth + 1),
        });
        i = closerAt + m.close.length;
        continue outer;
      }
    }
    buf += text[i];
    i++;
  }
  flush();
  return nodes;
}

/** Parse lesson text into inline nodes. Never throws; never loses a character. */
export function parseInlineMarks(text: string): InlineNode[] {
  return parseRange(text, 0, text.length, 0);
}

/** The text a reader sees — every marker that actually opened a span removed. */
export function stripInlineMarks(text: string): string {
  const out: string[] = [];
  const walk = (nodes: InlineNode[]) => {
    for (const n of nodes) {
      if (n.type === "text") out.push(n.value);
      else walk(n.children);
    }
  };
  walk(parseInlineMarks(text));
  return out.join("");
}

/** The run of non-whitespace around `pos`, or null when `pos` sits on whitespace. */
function wordAround(text: string, pos: number): [number, number] | null {
  const isWord = (ch: string | undefined) => ch !== undefined && !isSpace(ch);
  if (!isWord(text[pos - 1]) && !isWord(text[pos])) return null;
  let s = pos;
  let e = pos;
  while (s > 0 && isWord(text[s - 1])) s--;
  while (e < text.length && isWord(text[e])) e++;
  return [s, e];
}

const TAGS = ["</u>", "<u>"] as const;

/** Where inside `text` an offset sits strictly inside a `<u>`/`</u>` tag. */
function tagAround(text: string, i: number): { at: number; length: number } | null {
  for (const tag of TAGS) {
    for (let k = 1; k < tag.length; k++) {
      if (i - k >= 0 && text.startsWith(tag, i - k)) return { at: i - k, length: tag.length };
    }
  }
  return null;
}

/** Move an offset OUT of a marker, leftwards. */
function snapStart(text: string, i: number): number {
  while (i > 0 && i < text.length && text[i - 1] === "*" && text[i] === "*") i--;
  const tag = tagAround(text, i);
  return tag ? tag.at : i;
}

/** Move an offset OUT of a marker, rightwards. */
function snapEnd(text: string, i: number): number {
  while (i > 0 && i < text.length && text[i - 1] === "*" && text[i] === "*") i++;
  const tag = tagAround(text, i);
  return tag ? tag.at + tag.length : i;
}

export type ToggleResult = { text: string; start: number; end: number };

/**
 * Toggle `mark` over [start, end) — what the editor's Ctrl+B calls with the
 * textarea's selectionStart/selectionEnd.
 *
 * The returned selection always covers the same INNER text: just inside the
 * markers after wrapping, and the bare text after unwrapping, so the caller can
 * assign it back to the textarea and the tutor's selection does not jump.
 */
export function toggleMark(text: string, start: number, end: number, mark: MarkName): ToggleResult {
  const m = markerFor(mark);
  let s = Math.max(0, Math.min(start, text.length));
  let e = Math.max(s, Math.min(end, text.length));

  if (s === e) {
    const word = wordAround(text, s);
    if (word === null) {
      // On whitespace: drop an empty pair and put the caret between the markers.
      const caret = s + m.open.length;
      return { text: text.slice(0, s) + m.open + m.close + text.slice(s), start: caret, end: caret };
    }
    [s, e] = word;
  }

  // A selection that starts or ends INSIDE a marker is a selection the tutor
  // made with the mouse, not a statement about the markers. Drag it out of them
  // first, or `**abc**` selected from offset 1 gets bolded into `****ab**c**`.
  s = snapStart(text, s);
  e = Math.max(s, snapEnd(text, e));

  const sel = text.slice(s, e);
  // A single `*` next to another `*` belongs to a `**` run, not to an em.
  const emOnStrongRun = mark === "em" && text.startsWith("**", s);

  // 1. The selection starts at a span of this mark — take the whole span off,
  //    whether or not the selection reaches its closer.
  if (!emOnStrongRun && text.startsWith(m.open, s)) {
    const closerAt = findCloser(text, s + m.open.length, text.length, m, new Map());
    if (closerAt !== null && closerAt + m.close.length >= e) {
      const inner = text.slice(s + m.open.length, closerAt);
      return {
        text: text.slice(0, s) + inner + text.slice(closerAt + m.close.length),
        start: s,
        end: closerAt - m.open.length,
      };
    }
  }

  // 2. The markers sit just outside the selection.
  const outerConfusedByStrong =
    mark === "em" && (text.slice(s - 2, s) === "**" || text.slice(e, e + 2) === "**");
  if (
    !outerConfusedByStrong &&
    s >= m.open.length &&
    text.slice(s - m.open.length, s) === m.open &&
    text.slice(e, e + m.close.length) === m.close
  ) {
    return {
      text: text.slice(0, s - m.open.length) + sel + text.slice(e + m.close.length),
      start: s - m.open.length,
      end: e - m.open.length,
    };
  }

  // 3. Nothing to remove: wrap.
  return {
    text: text.slice(0, s) + m.open + sel + m.close + text.slice(e),
    start: s + m.open.length,
    end: e + m.open.length,
  };
}
