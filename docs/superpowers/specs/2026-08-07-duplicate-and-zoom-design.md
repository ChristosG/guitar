# Duplicate a curriculum, and make zoom stop breaking menus

**Date:** 2026-08-07
**Branch:** `desktop`
**Status:** approved, Phase 1 of 2

## Why this exists

Chris asked for two things that sound unrelated and are not:

> "i want an extra option 'duplicate' or 'create a copy' ... so that the tutor
> can make changes and always have a 'backup' or a version more general which
> needs to be a base for similar curricula"

> "when he changes texts with AI, it would be dope if somehow can see the
> 'diff' ... although that seems hard when AI might generate new content
> instead of just 'appending' some sentences"

Both are the same instinct arriving from two directions: *I want a base I can
compare against.* They are separated here only because one of them is small and
one of them needs a Greek-aware text-diff engine and new server-side snapshots.

While describing them he reported two bugs from the `.deb`, which is the build
that actually matters ("thats the version we care"). Those bugs turned out to be
the more urgent half of the work, so they lead.

## Scope

**Phase 1 (this spec).** Duplicate a curriculum. A tooltip on truncated titles.
The zoom fix. Ship a `.deb`.

**Phase 2 (its own spec).** The "what changed" diff panel, and a module-scoped
AI restructure entry point.

Explicitly **not** in either phase: version history for lesson prose. `refine.py`
already argues that case ("A tutor who refines twice and wants the original back
is asking for version control, and version control over lesson prose is a
different feature with a different UI"), and Duplicate is the answer at the scale
where it matters.

## Decisions taken, and by whom

| Decision | Choice | Who |
|---|---|---|
| Diff timing | Inspector (after the change lands), not a review gate | Chris |
| Diff reach | Refine + `edit_segment` + whole-lesson redraft + **module** redraft | Chris |
| Sequencing | Bugs + Duplicate first, ship, then the features | Chris |
| Zoom fix | Native webview zoom, keep pinch via one scoped permission | Chris |
| Copy scope | True fork — prose, blueprint, artifacts, everything | Chris |
| Copy title language | The **course's** `language`, not the UI locale | Claude |
| Auto-navigate after copy | No. The copy appears; nothing moves under him | Claude |
| Copy chat sessions | No | Claude |
| Duplicate during an active job | Allowed — it only reads the source | Claude |

---

## Section A — Duplicate a curriculum

### A1. The clone already exists

`app/curriculum/assign.py:clone_content_subtree` is a recursive deep-clone of a
`Block` subtree that already solves the hard parts: the `plane == "content"`
filter, the fresh-id flush-then-reparent ordering, and independent dict copies of
`meta` and `target_profile`. It stamps `is_template=False` + `student_id` because
it was written for per-student assignment.

A curriculum duplicate is the same walk with the opposite stamp. **It must not be
a second copy of the walk.** That module's docstring exists specifically to argue
this point — it was extracted from two call sites precisely so a new `Block`
field or a plane-filter change could not silently diverge them. A third copy
would reintroduce exactly the drift it was written to prevent.

So `clone_content_subtree` gains three parameters:

```python
def clone_content_subtree(
    db, node, *, parent_id, student_id,
    is_template: bool = False,
    id_map: dict[UUID, UUID] | None = None,
) -> Block
```

`assign` passes nothing new and behaves identically. `duplicate` passes
`student_id=None, is_template=True` and an `id_map` dict it owns, which the
recursion fills as `{source_block_id: clone_id}` for every node it creates.

### A2. The duplicate service

New framework-free module `app/curriculum/duplicate.py`, same caller-owned-session
convention as its siblings under `app/curriculum/`.

```python
def duplicate_curriculum(db, root_id: UUID) -> Block
```

1. Load the root; refuse anything that is not `kind == "course"` with
   `is_template=True` and `parent_id is None`.
2. `id_map = {}`; `clone_content_subtree(db, course, parent_id=None,
   student_id=None, is_template=True, id_map=id_map)`.
3. Title the copy. `«{title} (αντίγραφο)»` when `course.language` starts with
   `el`, `"{title} (copy)"` otherwise. **Truncate to 300 chars** — `Block.title`
   is `String(300)` and duplicating a duplicate of a duplicate is a real thing a
   tutor does. Truncation cuts the *original* title, never the suffix, so the
   marker always survives.
4. Stamp the copy's root meta, **whole-dict reassignment** — `Block.meta` is
   plain `sa.JSON` with no `MutableDict`, so in-place mutation appears to work in
   dev and silently no-ops in production:
   ```python
   copy.meta = {**(copy.meta or {}),
                "copied_from": str(root_id),
                "copied_at": datetime.now(timezone.utc).isoformat()}
   ```
   Nothing reads these yet. They cost one dict key each and are what makes Phase
   2's "compare with the original" possible without a migration.
5. Normalise stale draft markers **on the copy only**: any block whose
   `meta.draft_status == "drafting"` is written as `"queued"`. A `drafting`
   marker means "a worker is mid-write on this"; the copy has no worker, and
   without this the copy shows phantom in-progress lessons until the next boot,
   when `jobs/sweep.py:49` would sweep them. The copy's own Resume must be honest
   the moment it exists.
6. Clone attached artifacts. One query — `select(Artifact).where(
   Artifact.block_id.in_(id_map.keys()))` — then a new `Artifact` row per hit
   with `block_id` remapped through `id_map`. `Artifact` is `{kind, spec}` JSON
   with no filesystem backing, so this is rows only: no media copy, no disk, no
   partial-copy failure mode.
7. `db.flush()`. The caller commits.

**`assign.py` shares the stale-`drafting` issue** — assigning a mid-draft
curriculum to a student copies the marker too. That is a real latent bug and it
is deliberately **not** fixed here. It is a behaviour change to a different
feature, it needs its own test, and the student surfaces are being retired
(`0d5a40a`). Noted, not touched.

### A3. The endpoint

```
POST /curricula/{root_id}/duplicate  ->  201  CurriculumListItem
```

- 404 via the existing `_get_course_root_or_404`.
- **No 409.** Delete refuses while a job is in flight because deleting strands a
  worker mid-write. Duplicate only *reads* the source, so there is nothing to
  strand; refusing would just block him at the exact moment a backup is most
  valuable.
- No request body. The name is derived, and Rename already exists for the rest.
- **No Alembic migration.** Everything new lives in the existing JSON `meta`
  column. This is load-bearing for the desktop: a migration has to survive
  first-run `alembic upgrade head` *and* the seed/backup restore path, and not
  having one removes that entire class of risk from this release.

### A4. Web

`lib/api.ts`:

```ts
export function duplicateCurriculum(rootId: string): Promise<CurriculumListItem> {
  return request<CurriculumListItem>(`/curricula/${rootId}/duplicate`, { method: "POST" });
}
```

`CurriculumActionsMenu` gains an `onDuplicated: (created: CurriculumListItem) => void`
prop and a third item:

```
⋯
├─ Rename
├─ Create a copy        <- new, Copy icon
├────────────────────
└─ Delete               (destructive)
```

The separator is new and is not decoration: three items with the destructive one
flush against a benign one is a misclick waiting to happen, and the two benign
items now sit together.

Duplicating shares the existing `busy` state, so the trigger shows the same
spinner Rename and Delete already show, and errors surface through the existing
`curriculum-actions-error` element. **No new dialog** — there is nothing to
confirm and nothing to type.

**No auto-navigation.** From the index, `onDuplicated` refetches and the copy
appears at the top (the list is already `created_at desc`). From the detail
header, the page stays exactly where it is and shows a brief inline confirmation
naming the copy. Rationale: the copy is the *backup*; he keeps working on the
thing he was working on. Being teleported into a copy you made for safekeeping is
the wrong default, and it is the one that loses unsaved attention.

i18n: new keys under `curricula.actions` in both `el.json` and `en.json`.
Greek is the product, so the Greek string is the real one and the English is the
translation — not the other way round.

---

## Section B — The truncated module title

`block-card.tsx:375` gives the title span `sm:truncate` and no `title` attribute,
so a long module name ends in an ellipsis with no way to read it. The pattern is
already in this file nine lines below (`:409`, the segment-status chip), so this
is `title={node.title}` and nothing else.

Native `title` tooltips work in WebKitGTK. A JS tooltip component would be more
code, would need portal positioning — which is the very thing Section C is
fixing — and would not work on the `.dmg` any better.

The curricula index cards get the same check; if they truncate, they get the same
attribute.

---

## Section C — Zoom, and why it breaks every floating element

### C1. What is actually wrong

`desktop/src-tauri/src/main.rs:245` sets `document.documentElement.style.zoom`.

Dropdowns are a Base UI `Menu.Positioner` inside a `Portal`
(`ui/dropdown-menu.tsx:22-25`). Base UI 1.6.0 bundles `@floating-ui/dom` 1.7.6,
which positions by reading the trigger's `getBoundingClientRect()` and writing
coordinates onto the portaled element.

Under CSS `zoom`, that rect comes back in **post-zoom** pixels. The portal target
is inside the zoomed root, so whatever coordinate is written to it gets
multiplied by the zoom factor **again**. The error is
`(zoom − 1) × distance-from-viewport-origin` — zero at zoom 1, worse the further
right the trigger sits, worse the more you zoom. Which is exactly the report: the
menu is "stuck on the 3 dots only if I am on the default zoom".

**This is not a dropdown bug.** It is every portaled floating element in the app —
dropdowns, dialogs, the confirm box, tooltips. A ⋯ button near the right edge is
merely where it shows first.

**And the test suite cannot see it.** Playwright drives Chromium; the `.deb`
ships WebKitGTK via wry and the `.dmg` ships WKWebView. `@floating-ui/dom` takes
explicit `isWebKit()` branches (`floating-ui.dom.mjs:62`, `:233`), so the two
engines do not run the same code. A green Playwright suite is not evidence about
this bug in either direction.

### C2. The fix

Tauri 2 exposes webview zoom as a core permission —
`core:webview:allow-set-webview-zoom`, confirmed present in this project's own
generated ACL manifest (`gen/schemas/acl-manifests.json`). So `Webview::set_zoom`
is *not* Rust-only, and the whole premise of the current design ("only Rust can
call it, and Rust cannot see a `wheel` event") is obsolete.

`ZOOM_JS` keeps everything that gives it its behaviour — the discrete `STEPS`
array, the `localStorage` persistence, the Ctrl+wheel/pinch listener, the
keyboard handler, the `capture: true`/`passive: false` reasoning. **One function
changes:**

```js
function paint() {
  // was: document.documentElement.style.zoom = ...
  invoke("plugin:webview|set_webview_zoom", { value: current });
}
```

New `desktop/src-tauri/capabilities/main.json`:

```json
{
  "identifier": "main-zoom",
  "windows": ["main"],
  "remote": { "urls": ["http://localhost:*"] },
  "permissions": ["core:webview:allow-set-webview-zoom"]
}
```

The `remote.urls` wildcard is required, not lazy: the port is picked at boot by
`firstrun::pick_app_ports` and is not knowable when the capability is written.
The grant is **one** permission that accepts one clamped float — not a general
IPC opening.

**The pattern was verified, not assumed.** Tauri matches `remote.urls` with
`urlpattern` 0.3.0 (`ipc/authority.rs:62`, `url_pattern.test(url)`), where
unspecified components default to wildcards. Compiled against that exact crate:

| Pattern | `localhost:8790/` | `localhost:8790/el/curricula` | `localhost:8791/el/curricula/abc-123` | `evil.com/` |
|---|---|---|---|---|
| `http://localhost:*` | ✅ | ✅ | ✅ | ❌ |

So one entry covers every port and every in-app path, and still refuses a
foreign origin. `127.0.0.1` is deliberately **not** listed — the shell always
navigates to `http://localhost:<port>` (`main.rs`, "main window at EXACTLY
http://localhost:<web>"), and a second spelling of the origin would be a second
thing to keep in sync for no gain.

The command itself is `set_webview_zoom(webview, label: Option<String>, value:
f64)` (`tauri-2.11.5/src/webview/plugin.rs:123`), so the invoke payload is
`{ value: <float> }` — `label` is optional and omitted, which targets the
calling webview.

`zoom_command` in Rust is unchanged; it still evals `window.__angelZoom.*`, which
still exists.

### C3. Consequences to handle

- **`paint()` becomes async and can fail.** At document-start the Tauri IPC
  bridge may not be injected yet. `paint()` must tolerate a missing
  `__TAURI_INTERNALS__` and retry on `DOMContentLoaded` — the existing code
  already calls `paint()` twice for the analogous "`documentElement` may not
  exist yet" reason, so the shape is there; only the failure mode is new.
- **It falls back to CSS zoom rather than to nothing.** If the invoke is
  unavailable or rejects, `paint()` writes `documentElement.style.zoom` exactly
  as today. That is the *current* behaviour, bug included — so the worst case of
  this change is the status quo, never a zoom control that silently does
  nothing. The fallback is also the diagnostic: if the built `.deb` still
  mispositions menus, the fallback is what ran, and the capability is what to
  look at. The two paths are mutually exclusive — whichever path sets the zoom,
  the other's mechanism is explicitly cleared, so they can never both apply and
  multiply into a 2.25× page.
- **Native zoom is per-webview, not per-origin, and is not persisted by Tauri.**
  `localStorage` remains the source of truth for the level, exactly as now. This
  is still why boot bothers to remember the port pair.
- **The webapp is unaffected.** `ZOOM_JS` is injected only by the desktop shell;
  in a browser, browser zoom already does the right thing.
- **The docstring must be rewritten.** It currently argues *for* CSS zoom and
  *against* opening IPC, and both positions are being reversed. A docstring that
  argues against the code beneath it is worse than none. The new one records why
  CSS zoom was tried, what it broke, and that the permission is deliberately one
  entry wide.

---

## Section D — Testing and verification

### D1. What automation covers

- **pytest** (`apps/api/tests/`): full-fidelity clone including segment prose,
  blueprint and `source_ids` carried on the root, artifacts re-pointed at cloned
  blocks, `drafting` → `queued` normalisation, `copied_from` stamped, the source
  left byte-identical, the title suffix in both languages, and the 300-char
  truncation.
- **Playwright** (`apps/web/tests/`): the menu item exists, duplicating refreshes
  the list, the copy is named, the original is still there.

**Run pytest serialized.** `guitar_test` is a singleton database and concurrent
runs deadlock each other. Same for Playwright on port 3100.

### D2. What automation cannot cover

The zoom fix. See C1. It is verified by hand in a real built `.deb`:

| Check | At |
|---|---|
| ⋯ menu on a right-edge module opens attached to its button | 0.8 / 1.0 / 1.5 / 2.0 |
| A dialog (Rename) centres correctly | 1.5, 2.0 |
| The confirm box (Delete) centres correctly | 1.5, 2.0 |
| Ctrl+wheel and trackpad pinch still step | 1.0 → 2.0 → 1.0 |
| View menu Zoom In/Out/Actual Size still work | — |
| The level survives an app restart | set 1.5, quit, relaunch |
| Duplicate end-to-end, then open the copy | 1.0 |
| Long module title shows a tooltip | 1.0 |

Photographed, the way the original zoom work was — that standard was set by the
code being replaced and there is no reason to lower it while replacing it.

### D3. Release

A `desktop-v*` tag drives CI to build the `dmg` and `deb`. The `.dmg` gets the
same zoom fix and cannot be verified here; WKWebView is the same engine family as
WebKitGTK, and native zoom removes the engine-specific coordinate question
entirely rather than papering over it, which is the main reason this approach was
chosen over patching the positioner.

---

## Phase 2, recorded here so it is not re-litigated

Two features, already scoped by the decisions above, to be specced separately.

**The "what changed" panel.** An inspector, not a gate. A chip next to Undo opens
a floating panel with old-vs-new. `meta.prev_body`/`prev_title` already exist and
are already serialized to the client (`lib/api.ts:720`) — for refine and, by
deliberate contract-copying, for surgical `edit_segment` (`revise.py:975-983`).
Whole-lesson and whole-module redraft need **new** snapshots, because
`modify_lesson` does not edit text: it flips `draft_status` to `queued` and a
worker rewrites every segment from scratch. The open design problem is the one
Chris named — a regenerated block has no textual relationship to its predecessor,
so a naive word diff is a wall of red and green. The intended answer is
paragraph-level matching by *similarity* rather than equality, with word-level
diffing only inside matched pairs, and Greek-aware normalisation throughout
(accents, final sigma) because an English-only tokenizer has silently broken
retrieval and the fabrication guard in this codebase before, with all tests
passing.

**Module-scoped AI restructure.** The capability already exists: the planner's op
vocabulary has `insert_lesson`, `modify_lesson`, `move_lesson`, `remove_lesson`
(`revise.py:195-206`), so "make this module 5 lessons instead of 3" is expressible
today through Revise with AI. What is missing is a module-shaped door, and scoping
— `plan_revision` currently feeds the planner `compact_tree_text(db, course)`, the
*entire* course, so restructuring one module means reading 24 lessons to edit 3.
A `scope_module_id` shrinks the prompt, sharpens attention, and makes collateral
edits to other modules structurally impossible via `validate_ops`' existing
id-resolution check rather than merely unlikely.

"Extend with AI" on a module is working as designed and is not the door: it is
`refine_block`, which rewrites a block's own `title`/`body` and never sees its
children.
