# Prompt Transparency — design

**Date:** 2026-07-17
**Status:** approved for planning
**Scope:** a Settings surface where the tutor can READ every prompt the app sends
to a model, in Greek-annotated but otherwise unmodified form, and edit the few
slices that carry no contract.

---

## The one-sentence version

Thirty-seven prompts, shown verbatim, explained in Greek beside the text rather
than rewritten into it, read-only except for three slices that cannot break
anything.

## Why this exists

Chris asked for a component to "see and tweak" the prompts. The interview
refined that to something narrower and better:

> "They will actually be for the teacher, but we can't degrade their quality so
> the teacher understands them better. He won't tweak them himself, but he just
> needs to watch them. He might tweak only some text explaining stuff."

Two constraints fall out of that sentence, and they drive the whole design:

1. **Prompt quality is not negotiable.** We do not simplify, translate, or
   restructure a prompt to make it legible. The text the model gets is the text
   he reads.
2. **Reading is the feature; editing is the footnote.** The value is a tutor who
   can see what the app tells the AI about his teaching. Editing is a small,
   guarded affordance on top.

There is a third constraint the code supplies. `agent/prompts.py:67-87` is 1,301
characters and nearly every sentence is a guard that exists because something
broke once: *never invent a citation*, *never write tablature as text*, *never
invent a copyrighted riff*, *call tools silently*. Handing that to a textarea is
handing someone the brake line. The design must make the guards visible and
unremovable at the same time.

### The existing ethos this must not violate

`apps/web/src/app/[locale]/(cockpit)/settings/page.tsx:22-37` states it:

> THE TUTOR IS A TOTAL BEGINNER WITH COMPUTERS. [...] He never sees JSON, a
> stack trace, a status code, or an English string we forgot to translate.

A raw English prompt dump violates this. The resolution is not to dumb the
prompts down (constraint 1 forbids it) but to **annotate**: a Greek explanation
of what a prompt does and when it runs, rendered *beside* the untouched English.
He is never asked to understand the English. He is given a Greek account of it,
and the English is there because transparency means showing the real thing.

---

## What is actually there — the census

37 distinct prompts across 12 flows. "Distinct" = a named identifier or a
distinct builder branch contributing model-facing text.

| Flow | Count | Notable |
|---|---:|---|
| Chat copilot | 7 | `SYSTEM_PROMPT` (1,301 ch), grounding block, repair, tab-bluff guard |
| Lesson drafting | 6 | draft tail, gap-tier, citation repair |
| Outline / add-module / refine | 8 | outline tail, deepen pass |
| Retrieval | 3 | Greek→English query translation, grounded answer, zero-hits |
| Artifacts | 3 | tab / chord / tone-recipe specs |
| Curriculum prefix | 3 | `CURRICULUM_SYSTEM` (358 ch) — inside the cache prefix |
| Shared directives | 3 | `language_directive` (611 ch el / 617 en), injected into 8 prompts |
| OCR / settings probe | 2 | `OCR_PROMPT` |
| Tool descriptions | (21 schemas) | 5,126 ch of prose, sent every chat turn |

Structural facts that shape the UI:

- **Only two module-level constants are byte-stable by design:** `SYSTEM_PROMPT`
  and `CURRICULUM_SYSTEM`. Everything else is assembled at call time from
  f-strings and fragments.
- **Therefore "the full prompt" is not a string in the codebase.** It only
  exists at the moment of the call. The viewer must *render* it.
- **Locale-dependence is centralised**, not scattered: `i18n.language_directive`
  and `i18n.answer_in` are the only locale-varying strings.
- `claude_cli._tool_system_prompt` renders all 21 tool schemas into a 13,949-char
  system prompt — only when `provider == claude_cli`, which is today's setting.
  The viewer must show what the *active provider* actually sends.

---

## Architecture

### A registry, not a scrape

`app/prompts/registry.py` — one `PromptEntry` per prompt:

```python
@dataclass(frozen=True)
class PromptEntry:
    id: str                      # stable, e.g. "chat.system"
    flow: str                    # "chat" | "lesson_draft" | ...
    source_ref: str              # "app/agent/prompts.py:67" — shown, so it's auditable
    title_el: str                # "Ο βοηθός συνομιλίας"
    what_it_does_el: str         # plain Greek. NEW copy. Never replaces the prompt.
    when_it_runs_el: str         # "Όταν γράφεις στη συνομιλία"
    render: Callable[[PreviewCtx], RenderedPrompt]
    slices: tuple[Slice, ...]    # usually empty
```

`render` returns the assembled prompt with sample interpolations, plus the spans
of each interpolated variable so the UI can mark them as chips rather than hide
them. **What he reads is what the model gets**, with `{student_brief}` shown as a
labelled chip containing a representative example.

The registry does not own the prompt text. It *points at* the existing
definitions. `SYSTEM_PROMPT` stays in `agent/prompts.py`. Duplicating prompt text
into a registry is how a viewer silently starts lying — it shows the copy while
the model gets the original.

### Editable slices

```python
@dataclass(frozen=True)
class Slice:
    id: str                 # "lesson_draft.student_pitch"
    label_el: str
    default: str            # the current text, in code
    kind: Literal["replace", "append"]
```

A slice is a contiguous, contract-free region.

**Finding: contract-free regions are rarer than expected.** An audit of
`curriculum/draft.py` found the pedagogy is *interwoven* with contracts, not
separable from them:

- `draft.py:78-79` — *"THIS MODULE IS GROUNDED IN HIS LIBRARY. Teach it from the
  pages above."* (style) **and** *"cite the page you used on every..."*
  (contract), one sentence.
- `draft.py:135-139` — *"LENGTH IS NOT OPTIONAL"* wrapped around
  `{ctx.teaching_minutes}` and `{ctx.position}`.
- `draft.py:90` — *"Leave every citations array empty."* Pure contract.

So the honest starting set is **two**, not three:

| Slice | Where | Kind | Why safe |
|---|---|---|---|
| `student.pitch` | `students/context.py:110-113` — *"Write for THIS student: pitch the explanations at his level, and where his notes say he is stuck, address it directly instead of teaching past it."* | replace | Verified: pure pedagogy, no schema, no citation rule, **no placeholder**. |
| `*.extra_guidance` | one **append** slot per major flow | append | Additive only. Cannot delete a guard. |

The append slot is the safest possible editing model and is most of what is
actually wanted: he can *add* instruction, never *remove* one.

### Slice extraction is a byte-identical refactor

More slices can be created later, but only by a refactor that lifts a pedagogy
fragment into a named constant **without changing the rendered output**:

```python
LESSON_STYLE = "teach from, not a plan for them."   # draft.py:129, lifted verbatim
```

This does not violate "do not degrade prompt quality" — the model receives the
identical bytes. The rule that makes it safe is a test:

> **Every slice extraction ships with a test asserting the rendered prompt is
> byte-identical to the pre-refactor string.**

Extraction without that test is prompt editing disguised as refactoring, which is
the one thing this spec exists to prevent. Ship the two slices above first; earn
more by refactoring, each with its own equality test.

**Locked, with a Greek reason shown:** JSON-schema contracts, `[p.N]` citation
rules, `language_directive`, tool descriptions, every guard sentence in
`SYSTEM_PROMPT`. Visible, not editable. The lock has a stated reason — "αυτό
εγγυάται ότι οι παραπομπές είναι αληθινές" — so it reads as engineering, not
paternalism.

### "Locked" means an editor can't break it by accident — not that Chris can't change it

It is his private app and his call. Every locked region names its reason in the
UI, and every one is a normal code change away. The lock exists because a textarea
is the wrong instrument for editing a guard, not because the guard is sacred.

The live worked example, which is why this section exists: Chris asked to remove
`SYSTEM_PROMPT`'s copyright rule — *"those books are copywrited, but i bought them
and they're mine, why should i get fucked by prompting for such stuff?"*

Investigating it found three things:

1. **The rule restricts none of his books.** The whole library ships in the prompt
   and is quoted and cited freely; `CURRICULUM_SYSTEM` explicitly instructs the
   model to prefer it. The premise of the complaint was unfounded.
2. **The rule is an anti-hallucination guard, not a copyright rule.** Its own
   decline message leads with *"I don't actually have it memorized, and guessing
   would just invent a confidently wrong ... transcription"*; "(and possibly
   copyrighted)" is a parenthetical. Removing it yields wrong tabs, not permitted
   ones.
3. **But the complaint pointed at a real bug** — an ordering one, elsewhere:
   `loop.py:601` declines *before* `loop.py:608` searches the library, so a
   transcription he **owns** is refused unread.

Fixed by reordering (search first, decline only on a miss) and dropping the
copyright parenthetical as noise for a private single-user app. See
`2026-07-17-library-scaling-design.md` § A7.

That is the pattern to expect from every lock: **the complaint is real, the
diagnosis is usually somewhere else, and a viewer that shows the prompt honestly
is what makes the diagnosis possible at all.** Which is the argument for this
whole spec — the transparency is not decoration, it is how the prompts get
debugged.

### Storage

New table. **Not** columns on `app_setting`, which is a fixed-column singleton
(`id`, `anthropic_key_ct`, `model`, timestamps) pinned to
`00000000-0000-0000-0000-000000000001`.

```
prompt_override
  slice_id    varchar(80)  PK
  text        text         NOT NULL
  created_at  timestamptz  NOT NULL default now()
  updated_at  timestamptz  NOT NULL default now()

prompt_override_history
  id          uuid  PK
  slice_id    varchar(80)  NOT NULL
  text        text         NOT NULL
  replaced_at timestamptz  NOT NULL default now()
```

Resolution: `resolve(slice_id) -> override.text if present else Slice.default`.
Defaults live in code — diffable, reviewable, and the reset target. Overrides
layer on top. History makes a bad edit recoverable, which is the price of
allowing edits at all.

### Validation on save

A slice edit is rejected, in Greek, when it:

1. removes a placeholder the surrounding f-string requires (else `KeyError` at
   call time, i.e. a 500 in the tutor's face);
2. exceeds a per-slice length ceiling;
3. is empty for a `replace` slice (use Reset instead — an explicit act).

### The cache note the UI must surface

`CURRICULUM_SYSTEM` sits **inside** the cached prefix (`corpus.py:242-249`,
`cache: True` at `corpus.py:285`). Editing anything in the prefix re-mints the
cache once: 1.25× base input on the whole library block. At today's ~90K that is
~$0.34; once the four books land (~593K) it is ~$2.20.

Once, then it re-warms. But it must be **shown**, not hidden — "αυτή η αλλαγή
κοστίζει μία φορά περίπου $2" — because the entire premise of `corpus.py` is that
a cache mistake is invisible until the invoice arrives a month later.

The append slots for curriculum flows go in the **volatile tail**, after the
breakpoint, where an edit costs nothing. Only prefix edits carry the warning.

---

## API

```
GET  /prompts                      -> [{id, flow, title_el, what_it_does_el,
                                        when_it_runs_el, has_override}]
GET  /prompts/{id}                 -> {..., rendered, spans, slices[], source_ref}
PUT  /prompts/slices/{slice_id}    -> {text} -> validate, snapshot, save
DELETE /prompts/slices/{slice_id}  -> reset to default
GET  /prompts/slices/{slice_id}/history
```

Auth: same session middleware as the rest of the cockpit. Errors follow the
existing machine-readable `code` convention that `settings/page.tsx` turns into
one Greek sentence — no new error style.

## Web

`settings/page.tsx` gains a third card below Key and Model, following the
existing patterns exactly: client component, `useTranslations("prompts")`,
`Card`/`Button`/`Textarea`, optimistic update reverting on failure, `data-testid`
throughout.

Grouped by flow, collapsed by default. Each prompt expands to:

```
┌──────────────────────────────────────────────┐
│ Ο βοηθός συνομιλίας                          │
│ Τι κάνει: ...  Πότε τρέχει: ...              │  ← Greek, ours
├──────────────────────────────────────────────┤
│ You are the guitar tutor's copilot. You do   │  ← verbatim, read-only
│ NOT have his students, curricula, [...]      │
│   [ΚΛΕΙΔΩΜΕΝΟ: εγγυάται αληθινές παραπομπές] │
├──────────────────────────────────────────────┤
│ Επιπλέον οδηγίες (προαιρετικό)               │  ← append slot
│ ┌──────────────────────────────────────────┐ │
│ └──────────────────────────────────────────┘ │
│ [Επαναφορά]  [Ιστορικό]           [Αποθήκευση]│
└──────────────────────────────────────────────┘
```

Read-only prompt text is selectable (he may want to paste it somewhere) but not
editable. Interpolated variables render as chips with their sample value.

## Testing

- **Registry completeness**: a test asserts every `LLMProvider` call site outside
  `app/llm/` is reachable from a registry entry. The census is **15 call sites**
  (counted: `artifacts/generate.py` ×2, `lessons/draft.py`, `brain/ocr.py`,
  `curriculum/draft.py` ×3, `brain/retrieve.py` ×2, `curriculum/extend.py`,
  `agent/loop.py` ×2, `curriculum/refine.py`, `curriculum/outline.py`,
  `routers/settings.py`). The test fails when someone adds the sixteenth without
  registering it. This is the test that keeps the viewer honest over time.
- **No-duplication**: `SYSTEM_PROMPT` as rendered by the registry is
  `is`-identical to the constant the agent uses — not merely equal.
- **Override resolution**: default when absent, override when present, default
  again after DELETE.
- **Validation**: an edit dropping a required placeholder is rejected.
- **Cache invariant** (the important one): with overrides applied to tail slices
  only, the cached prefix stays byte-identical across two curriculum calls, and
  the second call reports non-zero `cache_read_input_tokens` — extending the
  existing assertion in `test_curriculum_grounding.py`.
- **i18n**: every `prompts.*` key exists in both `el` and `en`.

## Explicitly out of scope

- Editing `SYSTEM_PROMPT`'s guard sentences.
- Editing tool descriptions.
- Editing `language_directive` — Greek is the product; this is load-bearing.
- Per-student or per-curriculum prompt variants. One system prompt, always.
  Variants are precisely the failure `corpus.py:233-241` documents: each variant
  mints its own cache entry at 1.25×, nothing breaks, and the invoice arrives a
  month later.
- A prompt playground / dry-run. Worth wanting; not this spec.

## Risks

| Risk | Mitigation |
|---|---|
| Viewer drifts from reality as prompts change | Registry points at definitions; completeness test fails on unregistered call sites |
| An append slot is used to smuggle in a contradiction ("ignore the citation rules") | Slots are appended *before* the schema contract, which stays last and therefore strongest; append text is capped |
| Tutor edits the prefix and re-mints a $2 cache repeatedly | Cost shown before save; only prefix slices warn |
| Rendered preview ≠ what the model got | Preview uses the same builders as the live path, not a copy |
