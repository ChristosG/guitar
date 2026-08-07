# "Τι άλλαξε;" + module-scoped AI restructure — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the tutor see what the AI changed in his prose, restore a whole lesson if he doesn't like it, and point the revise planner at ONE module instead of the whole course.

**Architecture:** The diff is computed client-side from data already on the wire (`meta.prev_body`, and a new `meta.prev_segments`) — no endpoint, no dependency. Paragraph alignment by similarity is what makes a regenerated block readable instead of a wall of red. The module scope is a parameter threaded through the three existing revise functions, not a second planner.

**Tech Stack:** TypeScript/React 19/Next 16 + next-intl (web), FastAPI/SQLAlchemy 2 (api), Playwright, pytest.

## Global Constraints

- **Greek is the product.** Every regex over prose uses the `u` flag (`\p{L}`, `\p{Mn}`); `\w` is ASCII-only in JS and is banned here. Every diff unit test uses Greek text. The fold mirrors `apps/api/app/text/normalize.py::fold` exactly: NFD → strip `\p{Mn}` → `ς`→`σ` → lowercase.
- **`Block.meta` is plain `sa.JSON` with no `MutableDict`.** Every write reassigns the whole dict: `block.meta = {**(block.meta or {}), ...}`. In-place mutation silently no-ops in production.
- **No new npm dependency.** `jsdiff` solves the word-level half and none of the paragraph alignment.
- **No Alembic migration.** Everything new lives in the existing JSON `meta` column — this keeps the desktop's first-run and seed/restore paths untouched.
- **Serialize the test suites.** `guitar_test` is a singleton DB; Playwright owns port 3100. Never run either concurrently.
- **Both surfaces.** The webapp runs `LLM_PROVIDER=claude_cli`, the `.deb`/`.dmg` run `claude`. Nothing in this plan may branch on provider.
- **Curriculum register.** Any new prompt text injects `language_directive` + `curriculum_style` (see `app/i18n.py`), same as every other content flow.

---

## File Structure

**Create**
- `apps/web/src/lib/prose-diff.ts` — pure diff engine. Fold, tokenize, similarity, paragraph alignment, word diff, rewrite detection. No React, no DOM.
- `apps/web/src/components/curriculum/what-changed.tsx` — the panel. Renders a `ProseDiff` and, for lessons, the restore button.
- `apps/web/tests/prose-diff.spec.ts` — Node-only unit tests (Playwright runs them without a browser).
- `apps/web/tests/what-changed.spec.ts` — browser tests for the panel.
- `apps/api/tests/test_revise_snapshots.py` — `prev_segments` capture + restore toggle.
- `apps/api/tests/test_revise_scope.py` — module-scoped planning + op rejection.

**Modify**
- `apps/api/app/curriculum/revise.py` — snapshot in `modify_lesson`; `scope_module_id` on `compact_tree_text`, `validate_ops`, `plan_revision`.
- `apps/api/app/curriculum/restore.py` *(new)* — `restore_lesson_segments`, framework-free, caller commits.
- `apps/api/app/routers/curriculum.py` — `POST /blocks/{lesson_id}/restore-segments`; `scope_module_id` on the revise route.
- `apps/api/app/schemas/curriculum.py` — `ReviseRequest.scope_module_id`.
- `apps/api/app/jobs/curriculum_revise.py` — thread `scope_module_id` from job params.
- `apps/web/src/lib/api.ts` — `prev_segments` on `BlockMeta`, `restoreLessonSegments()`, `scope_module_id` on `reviseCurriculum`.
- `apps/web/src/components/curriculum/extend-with-chat.tsx` — the «Τι άλλαξε;» chip.
- `apps/web/src/components/curriculum/block-card.tsx` — module menu item; mount the panel for lessons.
- `apps/web/src/components/curriculum/revise-drawer.tsx` — accept and display a module scope.
- `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx` — provide `ReviseScopeContext`.
- `apps/web/src/messages/{el,en}.json` — new strings, Greek first.

---

## Task 1: The diff engine

**Files:**
- Create: `apps/web/src/lib/prose-diff.ts`
- Test: `apps/web/tests/prose-diff.spec.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  ```ts
  export type DiffPiece = { type: "same" | "add" | "del"; text: string };
  export type DiffBlock =
    | { kind: "same"; text: string }
    | { kind: "add"; text: string }
    | { kind: "del"; text: string }
    | { kind: "edit"; pieces: DiffPiece[] };
  export type ProseDiff = { rewritten: boolean; blocks: DiffBlock[]; similarity: number };
  export function fold(text: string): string;
  export function diffProse(before: string, after: string): ProseDiff;
  ```

- [ ] **Step 1: Write the failing tests**

Create `apps/web/tests/prose-diff.spec.ts`. Import relatively (`../src/lib/prose-diff`) — do not rely on the `@/` alias resolving in Playwright's Node transform.

```ts
import { test, expect } from "@playwright/test";
import { diffProse, fold } from "../src/lib/prose-diff";

test("fold matches the API's Greek fold", () => {
  expect(fold("Τονικότητα")).toBe("τονικοτητα");
  expect(fold("ΦΩΣ")).toBe(fold("φώς"));
  expect(fold("μάθημα")).toBe("μαθημα");
});

test("an appended sentence is an edit, not a rewrite", () => {
  const before = "Ο ενισχυτής χρωματίζει τον ήχο.";
  const after = "Ο ενισχυτής χρωματίζει τον ήχο. Δοκίμασε χαμηλό gain.";
  const d = diffProse(before, after);
  expect(d.rewritten).toBe(false);
  expect(d.blocks).toHaveLength(1);
  expect(d.blocks[0].kind).toBe("edit");
  const added = (d.blocks[0] as any).pieces.filter((p: any) => p.type === "add");
  expect(added.map((p: any) => p.text).join(" ")).toContain("gain");
});

test("an inserted paragraph is an add and leaves its neighbours alone", () => {
  const before = "Πρώτη παράγραφος.\n\nΤρίτη παράγραφος.";
  const after = "Πρώτη παράγραφος.\n\nΔεύτερη παράγραφος.\n\nΤρίτη παράγραφος.";
  const d = diffProse(before, after);
  expect(d.blocks.map((b) => b.kind)).toEqual(["same", "add", "same"]);
});

test("a deleted paragraph is a del", () => {
  const before = "Μία.\n\nΔύο.\n\nΤρία.";
  const after = "Μία.\n\nΤρία.";
  const d = diffProse(before, after);
  expect(d.blocks.map((b) => b.kind)).toEqual(["same", "del", "same"]);
});

test("accents and final sigma alone are NOT a change", () => {
  const d = diffProse("Ο ήχος της κιθάρας.", "Ο ηχος της κιθαρας.");
  expect(d.blocks.every((b) => b.kind === "same")).toBe(true);
});

test("a total rewrite says so instead of producing confetti", () => {
  const before = "Ο ενισχυτής χρωματίζει τον ήχο με τον προενισχυτή του.";
  const after = "Οι χορδές καθορίζουν το ύφος: πάχος, υλικό, και ηλικία.";
  const d = diffProse(before, after);
  expect(d.rewritten).toBe(true);
});

test("empty before means everything is new, and it never crashes", () => {
  expect(diffProse("", "Κάτι νέο.").blocks.map((b) => b.kind)).toEqual(["add"]);
  expect(diffProse("Κάτι.", "").blocks.map((b) => b.kind)).toEqual(["del"]);
  expect(diffProse("", "").blocks).toEqual([]);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/web && npx playwright test prose-diff --reporter=line`
Expected: FAIL — cannot resolve `../src/lib/prose-diff`.

- [ ] **Step 3: Implement the engine**

Create `apps/web/src/lib/prose-diff.ts`:

```ts
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
 *      of pretending. A diff that admits it cannot help beats confetti.
 *
 * Client-side on purpose: `prev_body`/`prev_segments` are already on the wire,
 * so this needs no endpoint and no round trip. No npm dependency either —
 * jsdiff would give us step 3's word LCS and none of step 2, which is the half
 * that matters.
 */

// Paragraphs scoring at or above this are "the same paragraph, edited".
// Below it they are unrelated, and pairing them would produce exactly the
// confetti this file exists to avoid.
const MATCH_FLOOR = 0.35;
// Below this overall, we stop calling it a diff.
const REWRITE_FLOOR = 0.2;
// Cost of leaving a paragraph unmatched. Deliberately cheaper than a bad
// match, so the aligner prefers "added + deleted" over "these two are related".
const GAP = -0.35;

/** Accent-, case- and final-sigma-insensitive form.
 *
 * MIRRORS `apps/api/app/text/normalize.py::fold` EXACTLY, and that file's
 * docstring lists three real bugs in this codebase that came from Greek accents
 * moving under inflection (μάθημα -> μαθήματα). Two JS-specific traps: `\w` is
 * ASCII-only without the `u` flag, and `\p{Mn}` REQUIRES it — the same mistake
 * in a different language. */
export function fold(text: string): string {
  if (!text) return "";
  return text
    .normalize("NFD")
    .replace(/\p{Mn}/gu, "")
    .replace(/ς/gu, "σ")
    .toLowerCase();
}

/** Unicode words. NOT `\w+`, which would score «συγχορδία» as zero tokens. */
function tokens(text: string): string[] {
  return text.match(/\p{L}[\p{L}\p{M}\p{N}]*/gu) ?? [];
}

/** Dice over folded token BIGRAMS.
 *
 * Bigrams, not unigrams: Greek function words (και, το, της, στο) are so common
 * that unigram overlap scores two unrelated paragraphs as similar. Bigrams
 * measure whether PHRASING survived.
 *
 * Dice, not Jaccard: Dice is forgiving of length differences, and "give more
 * detail about the amp" makes the new paragraph longer — which must not read as
 * a low match. */
export function similarity(a: string, b: string): number {
  const ta = tokens(a).map(fold);
  const tb = tokens(b).map(fold);
  if (!ta.length && !tb.length) return 1;
  if (!ta.length || !tb.length) return 0;
  // One-token paragraphs have no bigrams; fall back to the token sets so a
  // heading like «Ζέσταμα» still matches itself.
  const grams = (t: string[]) =>
    t.length < 2 ? t : t.slice(0, -1).map((w, i) => `${w} ${t[i + 1]}`);
  const ga = grams(ta);
  const gb = grams(tb);
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

/** Blank-line separated blocks, with headings and list items kept whole. */
function paragraphs(text: string): string[] {
  return text
    .split(/\n\s*\n/u)
    .map((p) => p.trim())
    .filter(Boolean);
}

/** Word-level diff inside one matched pair — plain LCS over folded tokens,
 * rendered over the ORIGINAL text so accents come back untouched. */
function wordDiff(before: string, after: string): DiffPiece[] {
  const a = before.split(/(\s+)/u).filter((s) => s !== "");
  const b = after.split(/(\s+)/u).filter((s) => s !== "");
  const key = (s: string) => fold(s.trim());
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = key(a[i]) === key(b[j]) ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
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
    if (key(a[i]) === key(b[j])) push("same", b[j++]), i++;
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) push("del", a[i++]);
    else push("add", b[j++]);
  }
  while (i < n) push("del", a[i++]);
  while (j < m) push("add", b[j++]);
  return out;
}

export type DiffPiece = { type: "same" | "add" | "del"; text: string };
export type DiffBlock =
  | { kind: "same"; text: string }
  | { kind: "add"; text: string }
  | { kind: "del"; text: string }
  | { kind: "edit"; pieces: DiffPiece[] };
export type ProseDiff = { rewritten: boolean; blocks: DiffBlock[]; similarity: number };

export function diffProse(before: string, after: string): ProseDiff {
  const A = paragraphs(before);
  const B = paragraphs(after);
  if (!A.length && !B.length) return { rewritten: false, blocks: [], similarity: 1 };
  if (!A.length) return { rewritten: false, similarity: 0, blocks: B.map((text) => ({ kind: "add", text })) };
  if (!B.length) return { rewritten: false, similarity: 0, blocks: A.map((text) => ({ kind: "del", text })) };

  // Needleman-Wunsch over the similarity matrix — an LCS that scores partial
  // matches instead of demanding identity. ORDER-PRESERVING by construction,
  // so paragraphs never cross.
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

  // Overall similarity = mean match score weighted by how much of BOTH sides
  // found a partner. Two paragraphs that matched perfectly out of ten do not
  // make a 1.0 diff.
  const overall = matched === 0 ? 0 : (scored / matched) * ((2 * matched) / (n + m));
  return { rewritten: overall < REWRITE_FLOOR, blocks, similarity: overall };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/web && npx playwright test prose-diff --reporter=line`
Expected: 7 passed. If "an appended sentence" fails as `rewritten`, the tuning is wrong — do NOT loosen `REWRITE_FLOOR` blindly; print `d.similarity` and check `similarity()` on the pair first.

- [ ] **Step 5: Typecheck and commit**

```bash
cd apps/web && npx tsc --noEmit
git add apps/web/src/lib/prose-diff.ts apps/web/tests/prose-diff.spec.ts
git commit -m "feat(diff): a prose diff that survives the AI rewriting a paragraph"
```

---

## Task 2: The «Τι άλλαξε;» panel

**Files:**
- Create: `apps/web/src/components/curriculum/what-changed.tsx`
- Modify: `apps/web/src/components/curriculum/extend-with-chat.tsx`, `apps/web/src/lib/api.ts`, `apps/web/src/messages/{el,en}.json`
- Test: `apps/web/tests/what-changed.spec.ts`

**Interfaces:**
- Consumes: `diffProse`, `ProseDiff` from Task 1.
- Produces: `<WhatChanged before={string} after={string} instruction?={string} onRestore?={() => Promise<void>} />`

- [ ] **Step 1: Add the strings (Greek is the real one)**

`curricula.extend` in `el.json`:
```json
"whatChanged": "Τι άλλαξε;",
"whatChangedTitle": "Τι άλλαξε",
"whatChangedInstruction": "Ζήτησες:",
"whatChangedRewritten": "Αυτό ξαναγράφτηκε από την αρχή — δεν υπάρχει χρήσιμη σύγκριση, οπότε δες τα δύο κείμενα δίπλα-δίπλα.",
"whatChangedBefore": "Πριν",
"whatChangedAfter": "Μετά",
"whatChangedUnchanged": "{count, plural, one {# παράγραφος αμετάβλητη} other {# παράγραφοι αμετάβλητες}}",
"whatChangedClose": "Κλείσιμο"
```
and the English mirror in `en.json`.

- [ ] **Step 2: Write the failing browser test**

`apps/web/tests/what-changed.spec.ts` — mock `GET /curricula/{id}` to return a segment carrying both `body` and `meta.prev_body`, open the board, click `what-changed-trigger`, and assert the panel shows an insertion and does NOT show the rewrite notice. Use the same route-interception convention as `curricula-duplicate.spec.ts` (anchored `API_ORIGIN`, CORS headers, OPTIONS, unexpected→500).

- [ ] **Step 3: Run to verify it fails**

Run: `cd apps/web && npx playwright test what-changed --reporter=line`
Expected: FAIL — no `what-changed-trigger`.

- [ ] **Step 4: Build the panel**

`what-changed.tsx` renders `diffProse(before, after)`:
- `rewritten` → the notice + the two texts stacked under «Πριν»/«Μετά». No red/green.
- otherwise → blocks in order; runs of consecutive `same` blocks collapse into one muted «N παράγραφοι αμετάβλητες» line; `add` green, `del` red with `line-through`, `edit` renders its pieces inline with the same colours.
- `instruction` shown at the top under «Ζήτησες:» when present.
- Container: `max-w-2xl max-h-[70vh] overflow-y-auto`. Use the existing `Dialog` (`components/ui/dialog.tsx`) — it is already portal-positioned and, since the zoom fix, correct at any zoom.
- **`onRestore` is optional and unused in this task** — Task 5 passes it.

- [ ] **Step 5: Add the chip**

In `extend-with-chat.tsx`, beside the existing Undo button and under the same `canUndo` condition, add a ghost button `data-testid="what-changed-trigger"` opening the panel. It needs the before/after text, so `ExtendWithChat` gains `prevBody?: string` and `body?: string` props; `block-card.tsx` passes `meta.prev_body` and `node.body`.

- [ ] **Step 6: Extend the API types**

In `lib/api.ts`, add to the block meta type: `prev_title?: string;` and `prev_segments?: { title: string; body: string | null; section?: string | null }[];` (the latter is unused until Task 3 but belongs with `prev_body`).

- [ ] **Step 7: Run tests, typecheck, commit**

```bash
cd apps/web && npx playwright test what-changed --reporter=line && npx tsc --noEmit
git add -A apps/web && git commit -m "feat(diff): the panel that shows what the AI actually changed"
```

---

## Task 3: `prev_segments` snapshots

**Files:**
- Modify: `apps/api/app/curriculum/revise.py:905-913`
- Test: `apps/api/tests/test_revise_snapshots.py`

**Interfaces:**
- Produces: `lesson.meta["prev_segments"] = [{"title": str, "body": str|None, "section": str|None}]`, captured at apply time, one level deep.

- [ ] **Step 1: Write the failing test**

```python
def test_modify_lesson_snapshots_the_live_segments_before_requeueing():
    """`modify_lesson` does not edit text — it requeues and a worker rewrites
    every segment from scratch. So the "before" must be captured HERE or it is
    gone forever."""
    # build course->module->lesson->2 segments with bodies
    # apply_revision(db, root, {"summary": "...", "ops": [{"op": "modify_lesson",
    #   "lesson_id": str(lesson.id), "instruction": "πιο αναλυτικά", "reason": "..."}]})
    # assert lesson.meta["draft_status"] == "queued"
    # assert [s["title"] for s in lesson.meta["prev_segments"]] == ["Ζέσταμα", "Θεωρία"]
    # assert lesson.meta["prev_segments"][0]["body"] == "Ξεκίνα με ανοιχτές χορδές."
```
Plus: a second `modify_lesson` overwrites rather than appends (one level deep); `undo_refine` on the lesson is unaffected by the presence of `prev_segments`.

- [ ] **Step 2: Run to verify it fails** — `cd apps/api && ./.venv/bin/python -m pytest tests/test_revise_snapshots.py -q`

- [ ] **Step 3: Implement**

Replace the `modify_lesson` branch body (keeping its existing comment about `prev_body` being a dead write, amended to explain what replaced it):

```python
segs = db.scalars(
    select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment").order_by(Block.order)
).all()
lesson.meta = {
    **(lesson.meta or {}), "draft_status": "queued", "error": None,
    "revise_instruction": op["instruction"],
    "prev_segments": [
        {"title": s.title, "body": s.body, "section": (s.meta or {}).get("section")}
        for s in segs
    ],
}
```

- [ ] **Step 4: Run to verify it passes**, then the full suite (`-m "not integration"`, serialized).

- [ ] **Step 5: Commit** — `git commit -m "feat(revise): capture the lesson before a rewrite replaces it"`

---

## Task 4: The restore toggle

**Files:**
- Create: `apps/api/app/curriculum/restore.py`
- Modify: `apps/api/app/routers/curriculum.py`
- Test: `apps/api/tests/test_revise_snapshots.py` (extend)

**Interfaces:**
- Produces: `restore_lesson_segments(db, lesson) -> bool` (False when nothing stashed); `POST /blocks/{lesson_id}/restore-segments -> BlockTreeOut`, 422 when there is no snapshot.

- [ ] **Step 1: Write the failing tests**

Cover: restore swaps the segment set back; **restore stashes what it replaced** so a second restore returns the AI's version (the toggle — this is the test that matters); segment count changes are handled (3 → 2 → 3); restored segments come back `segment_status: "ready"` so the draft fan-out does not immediately rewrite them; word count recomputed; 422 with no snapshot.

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement `restore.py`**

One transaction, in this order (order matters — see the spec's A2b):
1. Read current segments → build `snapshot`.
2. `db.delete()` each current segment; `db.flush()`.
3. Recreate stored `prev_segments` in order with `meta={"segment_status": "ready", "section": section}`.
4. `lesson.meta = {**meta, "prev_segments": snapshot}` — whole-dict.
5. `_recompute_lesson_word_count(db, lesson)` (import from `revise.py`).

Docstring must record: ids are NOT preserved, so an `Artifact` on a restored-away segment detaches via its own `ondelete="SET NULL"` — noted, not solved.

- [ ] **Step 4: Add the route** beside `undo_block_refine`, 404 for non-lessons, 422 for no snapshot.

- [ ] **Step 5: Run tests + full suite. Commit.**

---

## Task 5: Restore in the panel

**Files:**
- Modify: `what-changed.tsx`, `block-card.tsx`, `lib/api.ts`, `messages/{el,en}.json`
- Test: `apps/web/tests/what-changed.spec.ts` (extend)

- [ ] **Step 1: Strings** — `"restore": "Επαναφορά"`, `"restoreConfirm": "Να επαναφερθεί το προηγούμενο μάθημα; Η τωρινή έκδοση κρατιέται και μπορείς να γυρίσεις πίσω."`, `"restoreError": "Η επαναφορά απέτυχε — δοκίμασε ξανά."`
- [ ] **Step 2: `restoreLessonSegments(lessonId)` in `lib/api.ts`.**
- [ ] **Step 3: Failing test** — lesson with `prev_segments` shows a restore button; clicking it POSTs and refreshes.
- [ ] **Step 4: Wire it.** For a `lesson` with `meta.prev_segments`, `block-card.tsx` mounts `WhatChanged` with `before` = the snapshot's bodies joined by `\n\n`, `after` = the live segments' bodies joined the same way, and `onRestore`. Confirm-gated via the existing `useConfirm()` — the copy says the current version is KEPT, because it is.
- [ ] **Step 5: Tests, typecheck, commit.**

---

## Task 6: Scope the planner to one module

**Files:**
- Modify: `apps/api/app/curriculum/revise.py` (`compact_tree_text`, `validate_ops`, `plan_revision`)
- Test: `apps/api/tests/test_revise_scope.py`

**Interfaces:**
- Produces: `compact_tree_text(db, course, *, scope_module_id: UUID | None = None)`, `validate_ops(db, root_id, raw, *, scope_module_id=None)`, `plan_revision(db, root_id, *, instruction, scope_module_id=None)`. All default to today's behaviour.

- [ ] **Step 1: Write the failing tests**

- scoped `compact_tree_text` contains the target module's lessons and NOT a sibling module's;
- `validate_ops` with a scope drops an `insert_lesson` whose `module_id` is a different module, and keeps one targeting the scoped module, recording the drop in `dropped`;
- a `modify_lesson` whose lesson lives in another module is dropped under scope and kept without it;
- unscoped calls behave exactly as before (regression guard).

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

`compact_tree_text`: when scoped, render only that module's subtree (same line format — the model must not learn a second shape).
`validate_ops`: after the existing `_tree_ids` resolution, when `scope_module_id` is set, narrow `module_ids` to just that module and `lesson_ids` to lessons whose `parent_id` is it; every existing per-op check then rejects out-of-scope ids with no new branch. Drop reason: `"outside the module this revision is scoped to"`.
`plan_revision`: accept and forward; include the module title in the prompt so the model knows what it is working on.

- [ ] **Step 4: Run tests + full suite. Commit.**

---

## Task 7: Route + job plumbing for the scope

**Files:**
- Modify: `apps/api/app/schemas/curriculum.py`, `apps/api/app/routers/curriculum.py`, `apps/api/app/jobs/curriculum_revise.py`
- Test: `apps/api/tests/test_revise_scope.py` (extend)

- [ ] **Step 1: Failing test** — `POST /curricula/{root}/revise` with `scope_module_id` stores it in the job params; the job passes it to `plan_revision`; a `scope_module_id` that is not a module under this root → 422.
- [ ] **Step 2: Run to verify it fails**
- [ ] **Step 3: Implement** — `ReviseRequest.scope_module_id: UUID | None = None`; validate it resolves to a `kind == "module"` with `parent_id == root_id`; thread through `job.params["scope_module_id"]`; `run_curriculum_revise_job` reads it back and forwards. Apply mode is unchanged — the plan is already validated.
- [ ] **Step 4: Tests + full suite. Commit.**

---

## Task 8: The module door

**Files:**
- Modify: `block-card.tsx`, `revise-drawer.tsx`, `curricula/[rootId]/page.tsx`, `lib/api.ts`, `messages/{el,en}.json`
- Test: `apps/web/tests/curricula-revise.spec.ts` (extend)

**Interfaces:**
- Produces: `ReviseScopeContext` — `{ openForModule(moduleId: string, moduleTitle: string): void }`, provided by the detail page, consumed by `BlockCard`.

- [ ] **Step 1: Strings** — `"restructureWithAi": "Αναδιάρθρωση με AI"`, `"scopedTo": "Μόνο η ενότητα «{title}»"`.
- [ ] **Step 2: Failing test** — a module's ⋯ shows «Αναδιάρθρωση με AI»; clicking it opens the revise drawer showing the scope chip; the revise POST carries `scope_module_id`.
- [ ] **Step 3: Run to verify it fails**
- [ ] **Step 4: Implement**
  - `ReviseScopeContext` in `revise-drawer.tsx` (exported), provided by the detail page wrapping `TreeBoard` + `ReviseDrawer`. **A context, not a prop** — `BlockCard` recurses and the codebase already notes there is no clean callback channel from a card up to the page; threading a prop through two recursion sites to reach one menu item is the drilling that comment complains about.
  - `BlockCard`: for `isModule`, a menu item calling `openForModule(node.id, node.title)`.
  - `ReviseDrawer`: holds `scope` state; renders a dismissible chip naming the module; passes `scope_module_id` on the revise call. **A scoped planner that looks unscoped is a trap** — the chip is not decoration.
  - `reviseCurriculum(rootId, instruction, scopeModuleId?)` in `lib/api.ts`.
- [ ] **Step 5: Full Playwright run, typecheck, commit.**

---

## Task 9: Verify on both surfaces

- [ ] **Step 1:** `cd apps/api && ./.venv/bin/python -m pytest -m "not integration" -q` — all pass, serialized.
- [ ] **Step 2:** `cd apps/web && npx playwright test --reporter=line` — all pass.
- [ ] **Step 3:** `docker compose up -d --build api web` and confirm `/health/ready` is `{"db":true,"llm":true,"embed":true}` — the webapp must stay on `ClaudeCLIProvider`.
- [ ] **Step 4:** Exercise the panel by hand on `guitar.cgrigoriadis.online`: Extend-with-chat a segment, open «Τι άλλαξε;», confirm the diff reads sensibly on real Greek prose.
- [ ] **Step 5:** Report to Chris — including whether unified inline actually reads well, which is the one open question in the spec.

---

## Self-Review

**Spec coverage:** A1 snapshots → Task 3. A2b restore toggle → Tasks 4, 5. A3 algorithm → Task 1. A4 client-side → Task 1. A5 panel → Task 2. B1/B2 module scope → Tasks 6, 7, 8. Testing section → each task plus Task 9.

**Gaps accepted and named:** whole-curriculum `redraft` gets no diff (Duplicate is the answer at that scale — Phase 1 decision); artifacts detach on restore (recorded in Task 4's docstring); `chat_tools_stream` is untouched.

**Open question carried to Task 9 Step 5:** unified inline vs side-by-side as the default rendering.
