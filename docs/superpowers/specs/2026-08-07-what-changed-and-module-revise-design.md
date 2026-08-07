# What changed, and a door into one module

**Date:** 2026-08-07
**Branch:** `desktop`
**Status:** design — Phase 2 of the 2026-08-07 pair. Phase 1 is
`2026-08-07-duplicate-and-zoom-design.md`.

## Why this exists

> "when he changes texts with AI, it would be dope if somehow can see the 'diff',
> like the git diff, although that seems hard when AI might generate new content
> instead of just 'appending' some sentences.. any ideas on how we can preview
> the diff? even if a floating panel pops next to it, it would be neat as well!"

And, discovered while scoping that:

> "on the 'Guitar Tone latest version' i want to make that module instead of 3
> lessons to have 5. how can i do that? from 'Revise with AI'? , there's an
> 'extend with ai' on each module but this one changes only the description of
> the module.. i think there we need a button which is for the whole module
> itself"

The second one is a smaller job than it looks and is included here because it
*creates* the diff's hardest case: a module-level restructure rewrites several
lessons at once, which is exactly the situation where "what changed?" stops
being a nicety.

## Decisions already taken (Phase 1 conversation)

| Decision | Choice | Who |
|---|---|---|
| Diff timing | **Inspector** — after the change lands, never a review gate | Chris |
| Diff reach | refine + `edit_segment` + whole-lesson redraft + **whole-module** redraft | Chris |
| Curriculum-wide `redraft` | Out. Duplicate is the answer at that scale | Claude, Phase 1 |

---

## Part A — "Τι άλλαξε;"

### A1. What already exists, and what does not

| AI surface | Has a "before"? |
|---|---|
| Extend-with-chat (`refine_block`) | ✅ `meta.prev_body` / `prev_title`, already serialized to the client (`lib/api.ts:720`) |
| Revise op `edit_segment` | ✅ same contract, copied verbatim on purpose (`revise.py:975-983`) |
| Revise op `modify_lesson` | ❌ — `revise.py:907` records that the stash was *removed* as a dead write |
| A module restructure (Part B) | ❌ — does not exist yet |
| `add_segment` / `insert_lesson` / `insert_module` | n/a — pure additions |

So half of this is free and half needs new snapshots.

### A2. The snapshots

`modify_lesson` does not edit text. It sets `draft_status: "queued"` and a
background worker rewrites **every segment from scratch** against the blueprint.
So the "before" is not a string, it is the lesson's whole segment set, and it has
to be captured at the moment the op is applied — after that, it is gone.

In `apply_revision`'s `modify_lesson` branch, before the requeue:

```python
lesson.meta = {
    **(lesson.meta or {}),
    "draft_status": "queued",
    "revise_instruction": op["instruction"],
    "prev_segments": [
        {"title": s.title, "body": s.body, "section": (s.meta or {}).get("section")}
        for s in _live_segments(db, lesson)
    ],
}
```

- **One level deep, overwritten每 time.** Same rule `prev_body` already follows,
  and for the same reason: two levels is version control, which is a different
  feature with a different UI.
- **Size.** A lesson is ~1800 words across ~4 segments — roughly 12KB of JSON.
  Postgres does not care, and `Block.meta` is already where `citations` live.
- **A module restructure snapshots PER LESSON**, not as one module-sized blob.
  The tutor reads a diff one lesson at a time, so lesson granularity is both the
  smaller payload and the one that matches how it will be read.
- **Whole-dict reassignment.** `Block.meta` is plain `sa.JSON` with no
  `MutableDict`. This is not optional and it fails silently in production.

`undo_refine` must ignore `prev_segments` — it restores a body, and a lesson's
body is not its segments. Restoring a whole segment set is a different operation
and is **not** in this spec; the panel is an inspector, and the existing
per-segment Undo already covers the surgical case.

### A3. The algorithm — the part that is actually hard

Chris named the problem precisely: *"AI might generate new content instead of
just appending some sentences"*. A naive word diff over a regenerated paragraph
is a wall of red and green that says nothing. Three rules answer it.

**Rule 1 — diff paragraphs, not characters.** Split both sides into blocks on
blank lines, keeping headings and list items as their own units.

**Rule 2 — match paragraphs by SIMILARITY, not equality.** Build the
old×new similarity matrix, then find the best *order-preserving* alignment with
a Needleman–Wunsch DP (an LCS that scores partial matches instead of demanding
identity). Pairs scoring above `MATCH_FLOOR` are "the same paragraph, edited" and
get a word-level diff inside them. Unmatched old paragraphs are removals,
unmatched new ones are additions.

This is the whole trick. It is what turns "everything changed" into "these two
paragraphs were reworded, this one is new, this one is gone".

**Rule 3 — when it really was a rewrite, SAY SO.** If the best global alignment
scores below `REWRITE_FLOOR`, do not render a diff at all. Show "αυτό
ξαναγράφτηκε" with the old and new text plainly side by side. A diff that admits
it cannot help is more useful than one that produces confetti — and this is the
case Chris was worried about, so it gets a first-class answer rather than a
degraded one.

```
similarity(a, b) = Dice coefficient over folded token BIGRAMS
  tokens  = text.match(/\p{L}[\p{L}\p{M}\p{N}]*/gu)
  folded  = NFD -> strip \p{Mn} -> ς→σ -> lowercase
  bigrams = consecutive token pairs
  dice    = 2|A ∩ B| / (|A| + |B|)
```

**Bigrams, not unigrams.** Greek function words (και, το, της, στο) are extremely
common; unigram overlap scores two unrelated paragraphs as similar purely on
those. Bigrams measure whether *phrasing* survived.

**Dice, not Jaccard.** Dice is more forgiving when lengths differ, which is the
normal case — "give more detail about the amp" makes the new paragraph longer,
and that must not read as a low match.

**The folding is not optional and it is not English.** `apps/api/app/text/
normalize.py` exists because three separate bugs in this codebase came from
Greek accents moving under inflection (μάθημα → μαθήματα), and its docstring
lists them. The client-side fold must mirror `fold()` exactly: NFD, strip
category `Mn`, unify final sigma, lowercase. Two JavaScript-specific traps:
`\w` is ASCII-only without the `u` flag, and `\p{Mn}` **requires** it — the same
class of mistake, in a different language.

### A4. Client-side, no new endpoint

`prev_body` is already in the tree payload; `prev_segments` will be too. So the
diff is computed in the browser: instant, no round trip, no server load, and
nothing new to deploy. The cost is a second implementation of `fold()` in
TypeScript, ~6 lines, which is cheap next to an endpoint.

New `apps/web/src/lib/prose-diff.ts` — pure, framework-free, unit-testable
without a browser. No npm dependency: `jsdiff` would give us the word-level LCS
but not the paragraph alignment, which is the part that matters, and the whole
thing is ~120 lines. A dependency that solves the easy half and none of the hard
half is not worth the bundle in a 756MB `.deb`.

### A5. The panel

A chip — «Τι άλλαξε;» — next to the existing Undo in `ExtendWithChat`, shown
whenever the block has a `prev_body`. It opens an anchored popover, wide
(`max-w-2xl`), scrollable (`max-h-[70vh]`), falling back to a centred dialog on
narrow viewports.

**This is only viable because of Phase 1.** An anchored popover is a portaled
floating element, which is precisely what was landing off-screen at any zoom
above 1. Building this on top of the CSS-zoom engine would have shipped a panel
the tutor could not see at his own zoom level.

Contents, in order: the instruction he gave ("what you asked for"), then the
diff. Unified inline rendering — additions underlined, removals struck through —
not side-by-side: Greek prose paragraphs are long and two columns halve the width
each gets. Unchanged paragraphs collapse to a one-line "N παράγραφοι αμετάβλητες"
so the panel shows what moved, not the whole lesson again.

---

## Part B — A door into one module

### B1. The capability exists; the door does not

`revise.py:195-206` already has `insert_lesson(module_id, title, objective)`,
`modify_lesson`, `move_lesson` and `remove_lesson`. "Make this module 5 lessons
instead of 3" is expressible **today** through Revise with AI, in prose.

"Extend with AI" on a module is not the door and is working as designed: it is
`refine_block`, which rewrites a block's own `title`/`body`. On a module the body
*is* the objective. It is a text tool and it is structurally blind — it never
sees the module's children. It should stay that way.

### B2. What to add

`plan_revision(db, root_id, *, instruction, scope_module_id=None)`:

- `compact_tree_text` renders only that module's subtree when scoped.
- `validate_ops` drops ops whose target does not resolve **under that module**,
  reusing the id-resolution it already performs against the root.

Scoping is not only ergonomics. Unscoped, the planner is fed
`compact_tree_text(db, course)` — the entire course, every module and lesson with
ids and objectives. Restructuring one module means reading 24 lessons to edit 3:
a longer prompt, a more distracted model, and collateral edits elsewhere that are
merely unlikely rather than impossible. Scoped, they are structurally impossible.

**Entry point:** the module's own ⋯ menu gets «Αναδιάρθρωση με AI», which opens
the existing `ReviseDrawer` pre-scoped to that module. Same drawer, same plan
card, same HITL approval, same apply path. The drawer shows which module it is
scoped to, because a scoped planner that looks unscoped is a trap.

**And it feeds Part A:** every `modify_lesson` the scoped plan emits snapshots
its lesson per A2, so the restructure is inspectable lesson by lesson when the
redraft lands.

---

## Testing

- `prose-diff.ts` unit tests, in **Greek**: an appended sentence, a reworded
  paragraph, an inserted paragraph, a deleted one, a total rewrite (must trip
  `REWRITE_FLOOR`), and accent-only / final-sigma-only differences (must score as
  identical). An English-only suite would pass while the feature was broken for
  the default locale — which is exactly how this codebase has been bitten before.
- pytest for the snapshots: `prev_segments` captured before the requeue, one
  level deep, whole-dict reassigned, and `undo_refine` unaffected by it.
- pytest for the scope: a scoped plan targeting another module's lesson is
  dropped by `validate_ops`.
- Playwright for the panel and the module menu entry.
- `.deb` by hand for the popover at zoom 1.5 — the panel is portaled, so it is
  exactly the surface Phase 1 fixed and the one place a regression would show.

## Open for Chris

1. **Inline vs side-by-side** as the default rendering. Spec says unified inline;
   easy to flip, and worth seeing real Greek prose in both before deciding.
2. **Should a lesson-level diff offer a restore?** The spec says no — the panel
   is an inspector and restoring a whole segment set is a different operation
   with different failure modes. But `prev_segments` makes it *possible*, and if
   he wants it, it is better designed now than bolted on.
