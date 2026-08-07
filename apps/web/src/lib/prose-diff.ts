/** Prose diff for AI-rewritten Greek lesson text.
 *
 * THE PROBLEM THIS SOLVES, in Chris's words: "AI might generate new content
 * instead of just appending some sentences". A word-level diff over a
 * REGENERATED paragraph is a wall of red and green that says nothing. Three
 * rules answer that, and they are the whole design:
 *
 *   1. Diff PARAGRAPHS, not characters.
 *   2. Match paragraphs by SIMILARITY, not equality — so "reworded" stops
 *      reading as "deleted and added". Word-level diffing happens only INSIDE
 *      a matched pair.
 *   3. When the match is genuinely that bad, say "this was rewritten" instead
 *      of pretending. A diff that admits it cannot help beats confetti, and
 *      this is the case Chris was actually worried about — so it gets a
 *      first-class answer rather than a degraded one.
 *
 * CLIENT-SIDE ON PURPOSE. `meta.prev_body` and `meta.prev_segments` are already
 * on the wire with the tree, so this needs no endpoint and no round trip. No
 * npm dependency either: jsdiff would give us `wordDiff` below and none of the
 * paragraph alignment, which is the half that matters.
 */

// Paragraphs scoring at or above this are "the same paragraph, edited". Below
// it they are unrelated, and pairing them produces exactly the confetti this
// file exists to avoid.
const MATCH_FLOOR = 0.35;
// Below this overall, we stop calling it a diff at all.
const REWRITE_FLOOR = 0.2;
// Cost of leaving a paragraph unmatched. Deliberately cheaper than a bad match,
// so the aligner prefers an honest "added + deleted" over "these two are
// vaguely related".
const GAP = -0.35;

export type DiffPiece = { type: "same" | "add" | "del"; text: string };
export type DiffBlock =
  | { kind: "same"; text: string }
  | { kind: "add"; text: string }
  | { kind: "del"; text: string }
  | { kind: "edit"; pieces: DiffPiece[] };
export type ProseDiff = { rewritten: boolean; blocks: DiffBlock[]; similarity: number };

/** Accent-, case- and final-sigma-insensitive form.
 *
 * MIRRORS `apps/api/app/text/normalize.py::fold` EXACTLY — NFD, strip category
 * Mn, unify final sigma, lowercase. That file's docstring lists three real bugs
 * in this codebase caused by Greek accents MOVING under inflection (μάθημα ->
 * μαθήματα), which is why folding is not optional here either.
 *
 * Two JavaScript-specific traps, both the same mistake in a different language:
 * `\w` is ASCII-only without the `u` flag, and `\p{Mn}` REQUIRES it. A regex
 * here without `/u` silently does nothing to Greek and everything looks fine.
 *
 * THE SIGMA STEP RUNS AFTER LOWERCASING, WHICH IS THE OPPOSITE ORDER TO THE
 * PYTHON. Not a stylistic difference — the two languages disagree about what
 * lowercasing does. Python's `.casefold()` already maps `ς` to `σ`, so
 * `normalize.py` can fold sigma first and casefold second. JavaScript's
 * `.toLowerCase()` implements the Unicode FINAL-SIGMA rule instead and
 * *produces* `ς`: `"ΦΩΣ".toLowerCase() === "φως"`. Fold sigma before that and
 * every Greek word ending in a capital sigma comes out with `ς` still on it,
 * so `fold("ΦΩΣ") !== fold("φώς")` and every -ς word silently stops matching
 * its own stem. Caught by the test that asserts those two are equal; the
 * behaviour is identical to Python's, only the step order differs.
 */
export function fold(text: string): string {
  if (!text) return "";
  return text
    .normalize("NFD")
    .replace(/\p{Mn}/gu, "")
    .toLowerCase()
    .replace(/ς/gu, "σ");
}

/** Unicode words. NOT `\w+`, which scores «συγχορδία» as zero tokens. */
function tokens(text: string): string[] {
  return text.match(/\p{L}[\p{L}\p{M}\p{N}]*/gu) ?? [];
}

/** Dice coefficient over folded token BIGRAMS.
 *
 * BIGRAMS, not unigrams: Greek function words (και, το, της, στο, με) are so
 * common that unigram overlap scores two entirely unrelated paragraphs as
 * similar. Bigrams measure whether PHRASING survived, which is the actual
 * question.
 *
 * DICE, not Jaccard: Dice is forgiving of length differences, and the normal
 * instruction — "give more detail about the amp" — makes the new paragraph
 * longer. That must not read as a low match.
 */
export function similarity(a: string, b: string): number {
  const ta = tokens(a).map(fold);
  const tb = tokens(b).map(fold);
  if (!ta.length && !tb.length) return 1;
  if (!ta.length || !tb.length) return 0;

  // A one-token paragraph has no bigrams at all, so fall back to the tokens
  // themselves — otherwise a heading like «Ζέσταμα» could not match itself.
  const grams = (t: string[]): string[] =>
    t.length < 2 ? t : t.slice(0, -1).map((w, i) => `${w} ${t[i + 1]}`);

  const ga = grams(ta);
  const gb = grams(tb);
  // Multiset intersection: a bigram repeated twice on one side and once on the
  // other counts once, not twice.
  const pool = new Map<string, number>();
  for (const g of ga) pool.set(g, (pool.get(g) ?? 0) + 1);
  let hits = 0;
  for (const g of gb) {
    const n = pool.get(g) ?? 0;
    if (n > 0) {
      hits++;
      pool.set(g, n - 1);
    }
  }
  return (2 * hits) / (ga.length + gb.length);
}

/** Blank-line separated blocks. Headings and list items stay whole because they
 * are already their own lines and never contain a blank one. */
function paragraphs(text: string): string[] {
  return text
    .split(/\n\s*\n/u)
    .map((p) => p.trim())
    .filter(Boolean);
}

/** Word-level diff inside ONE matched pair.
 *
 * Plain LCS over FOLDED tokens, rendered over the ORIGINAL text — so a word
 * that changed only its accent is not reported, but the accented spelling is
 * what the tutor reads back. Whitespace is kept as its own token so the
 * reassembled text still looks like prose.
 */
function wordDiff(before: string, after: string): DiffPiece[] {
  const a = before.split(/(\s+)/u).filter((s) => s !== "");
  const b = after.split(/(\s+)/u).filter((s) => s !== "");
  const key = (s: string) => fold(s.trim());
  const n = a.length;
  const m = b.length;

  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] =
        key(a[i]) === key(b[j])
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const out: DiffPiece[] = [];
  const push = (type: DiffPiece["type"], text: string) => {
    const last = out[out.length - 1];
    if (last && last.type === type) last.text += text;
    else out.push({ type, text });
  };

  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (key(a[i]) === key(b[j])) {
      push("same", b[j]);
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      push("del", a[i++]);
    } else {
      push("add", b[j++]);
    }
  }
  while (i < n) push("del", a[i++]);
  while (j < m) push("add", b[j++]);
  return out;
}

export function diffProse(before: string, after: string): ProseDiff {
  const A = paragraphs(before);
  const B = paragraphs(after);

  if (!A.length && !B.length) return { rewritten: false, blocks: [], similarity: 1 };
  if (!A.length) {
    return { rewritten: false, similarity: 0, blocks: B.map((text) => ({ kind: "add", text })) };
  }
  if (!B.length) {
    return { rewritten: false, similarity: 0, blocks: A.map((text) => ({ kind: "del", text })) };
  }

  // NEEDLEMAN-WUNSCH over the similarity matrix — an LCS that scores PARTIAL
  // matches instead of demanding identity. Order-preserving by construction, so
  // paragraphs can never cross: a swap is reported honestly as one add and one
  // del rather than as two matches in the wrong order.
  const n = A.length;
  const m = B.length;
  const sim: number[][] = A.map((a) => B.map((b) => similarity(a, b)));

  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) dp[i][m] = dp[i + 1][m] + GAP;
  for (let j = m - 1; j >= 0; j--) dp[n][j] = dp[n][j + 1] + GAP;
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      const pair = sim[i][j] >= MATCH_FLOOR ? sim[i][j] + dp[i + 1][j + 1] : -Infinity;
      dp[i][j] = Math.max(pair, dp[i + 1][j] + GAP, dp[i][j + 1] + GAP);
    }
  }

  const blocks: DiffBlock[] = [];
  let matched = 0;
  let scored = 0;
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const pair = sim[i][j] >= MATCH_FLOOR ? sim[i][j] + dp[i + 1][j + 1] : -Infinity;
    if (pair >= dp[i + 1][j] + GAP && pair >= dp[i][j + 1] + GAP) {
      // Folded equality, not raw: a paragraph whose only change is an accent
      // must render as untouched, or the panel cries wolf on every re-render.
      if (fold(A[i]) === fold(B[j])) blocks.push({ kind: "same", text: B[j] });
      else blocks.push({ kind: "edit", pieces: wordDiff(A[i], B[j]) });
      scored += sim[i][j];
      matched++;
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      blocks.push({ kind: "del", text: A[i++] });
    } else {
      blocks.push({ kind: "add", text: B[j++] });
    }
  }
  while (i < n) blocks.push({ kind: "del", text: A[i++] });
  while (j < m) blocks.push({ kind: "add", text: B[j++] });

  // Overall similarity = the mean match quality, weighted by how much of BOTH
  // sides found a partner at all. Two paragraphs matching perfectly out of ten
  // must not score 1.0 — that is precisely the wholesale regeneration this
  // number exists to detect.
  const overall = matched === 0 ? 0 : (scored / matched) * ((2 * matched) / (n + m));
  return { rewritten: overall < REWRITE_FLOOR, blocks, similarity: overall };
}
