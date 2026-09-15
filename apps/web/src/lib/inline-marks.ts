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

/**
 * A span the parser actually opened, with the offsets the tree does not carry.
 *
 * `parseInlineMarks` returns nodes, not positions, and `toggleMark` has to know
 * WHERE the opener and closer of the span under the tutor's selection are —
 * which is a question only the grammar can answer. So this walks the text with
 * the same rules as `parseRange`, in the same order, using the same
 * `findCloser`, and records the four offsets instead of building children. It
 * is private on purpose: the tree is the public shape, and a second exported
 * view of the same walk is a second thing to keep in step with Python.
 */
type Span = {
  mark: MarkName;
  openStart: number;
  innerStart: number;
  innerEnd: number;
  closeEnd: number;
};

function collectSpans(text: string): Span[] {
  const spans: Span[] = [];

  const walk = (start: number, end: number, depth: number): void => {
    const absent: Absent = new Map();
    let i = start;
    outer: while (i < end) {
      if (depth < MAX_DEPTH) {
        for (const m of MARKERS) {
          if (i + m.open.length > end || !text.startsWith(m.open, i)) continue;
          const contentStart = i + m.open.length;
          if (m.mark !== "u" && (contentStart >= end || isSpace(text[contentStart]))) {
            i = contentStart; // rule 4 — literal, and no second chance
            continue outer;
          }
          const closerAt = findCloser(text, contentStart, end, m, absent);
          if (closerAt === null) continue; // rule 2
          spans.push({
            mark: m.mark,
            openStart: i,
            innerStart: contentStart,
            innerEnd: closerAt,
            closeEnd: closerAt + m.close.length,
          });
          walk(contentStart, closerAt, depth + 1);
          i = closerAt + m.close.length;
          continue outer;
        }
      }
      i++;
    }
  };

  walk(0, text.length, 0);
  return spans;
}

/** Widen an offset leftwards over the markers the selection is touching. */
function extendStart(text: string, i: number): number {
  const tag = tagAround(text, i);
  if (tag) i = tag.at;
  while (i > 0 && text[i - 1] === "*") i--;
  for (const t of TAGS) {
    if (i >= t.length && text.startsWith(t, i - t.length)) return i - t.length;
  }
  return i;
}

/** Widen an offset rightwards over the markers the selection is touching. */
function extendEnd(text: string, i: number): number {
  const tag = tagAround(text, i);
  if (tag) i = tag.at + tag.length;
  while (i < text.length && text[i] === "*") i++;
  for (const t of TAGS) {
    if (text.startsWith(t, i)) return i + t.length;
  }
  return i;
}

/** How many asterisks run away from `i` — leftwards when `step` is -1. */
function asteriskRun(text: string, i: number, step: -1 | 1): number {
  let n = 0;
  while (text[step < 0 ? i - 1 - n : i + n] === "*") n++;
  return n;
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
  const caret = Math.max(0, Math.min(start, text.length));
  let s = caret;
  let e = Math.max(s, Math.min(end, text.length));

  // 1. THE SPACE THE MOUSE PICKED UP. A double-click, or a drag that overshot
  //    by one character, hands us «πάνω » with the trailing space in it. Wrapped
  //    as selected that becomes `*πάνω *`, and the grammar is right to call a
  //    closer with whitespace in front of it literal (rule 4) — so the tutor is
  //    left looking at raw asterisks. The space was never part of the word he
  //    meant to italicise: push it back out of the selection.
  while (s < e && isSpace(text[s])) s++;
  while (e > s && isSpace(text[e - 1])) e--;

  if (s === e) {
    // Nothing but whitespace was selected (or nothing at all) — this is a caret.
    s = caret;
    const word = wordAround(text, s);
    if (word === null) {
      // On whitespace: drop an empty pair and put the caret between the markers.
      const at = s + m.open.length;
      return { text: text.slice(0, s) + m.open + m.close + text.slice(s), start: at, end: at };
    }
    [s, e] = word;
  }

  // 2. A selection that stops just short of a marker — or one character into it
  //    — is a selection the tutor made with the mouse, not a statement about the
  //    markers. Widen over whatever markers touch either edge, so an opener or a
  //    closer left just outside (or just inside) the selection is still his.
  const xs = extendStart(text, s);
  const xe = Math.max(xs, extendEnd(text, e));

  const sel = text.slice(s, e);

  const off = (span: Span): ToggleResult => ({
    text:
      text.slice(0, span.openStart) +
      text.slice(span.innerStart, span.innerEnd) +
      text.slice(span.closeEnd),
    start: span.innerStart - m.open.length,
    end: span.innerEnd - m.open.length,
  });

  // 3. Is the selection a span of this mark that the PARSER agrees is a span?
  //    Asked of the grammar's own walk, not of the characters either side: the
  //    markers next to an offset may be literal text, part of a `**` that is not
  //    ours, or the closer of something else entirely.
  const spans = collectSpans(text).filter((span) => span.mark === mark);
  const same = (a: number, b: number, c: number, d: number) => a === c && b === d;
  const hit =
    spans.find((span) => same(span.openStart, span.closeEnd, xs, xe)) ??
    spans.find((span) => same(span.openStart, span.closeEnd, s, e)) ??
    spans.find((span) => same(span.innerStart, span.innerEnd, s, e));
  if (hit) return off(hit);

  // PARITY, not adjacency. In a run of asterisks the `**` pairs are taken from
  // the outside in, so a run of ODD length has exactly one `*` left over and
  // that one is an em marker — `***σόλο***` is strong(em(…)). A run of EVEN
  // length is all `**` and none of it belongs to an em. Asking only "is there a
  // `**` next to the selection?" got this wrong in the direction that hurts:
  // Ctrl+I on a bold-italic word wrapped it again into `****σόλο****`, which
  // parses as two EMPTY strongs around the word — the bold gone and eight
  // invisible asterisks left behind.
  const runLeftOf = (i: number) => asteriskRun(text, i, -1);
  const runRightOf = (i: number) => asteriskRun(text, i, 1);

  // Where a span of this mark opens at the widened start, if one does.
  const leadRun = runRightOf(xs);
  const opensAt =
    mark === "em"
      ? leadRun % 2 === 1
        ? xs + leadRun - 1 // the leftover `*` is the LAST of the run
        : null
      : text.startsWith(m.open, xs)
        ? xs
        : null;

  // 4. The selection starts at a span of this mark — take the whole span off,
  //    whether or not the selection reaches its closer.
  if (opensAt !== null) {
    const o = opensAt;
    const closerAt = findCloser(text, o + m.open.length, text.length, m, new Map());
    if (closerAt !== null && closerAt + m.close.length >= e) {
      return off({
        mark,
        openStart: o,
        innerStart: o + m.open.length,
        innerEnd: closerAt,
        closeEnd: closerAt + m.close.length,
      });
    }
  }

  // 5. The markers sit just outside the selection, literal as far as the parser
  //    is concerned — `****x****`, where the pairs the tutor sees are empty
  //    spans to the grammar. One pair comes off.
  const outerConfusedByStrong =
    mark === "em" && (runLeftOf(s) % 2 === 0 || runRightOf(e) % 2 === 0);
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

  // 6. Nothing to remove: wrap what is left after the whitespace was handed back.
  return {
    text: text.slice(0, s) + m.open + sel + m.close + text.slice(e),
    start: s + m.open.length,
    end: e + m.open.length,
  };
}
