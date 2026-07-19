# Curriculum Control (Parts 1–4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Revise-with-AI chat actually work (guard misfire fix), declutter citations into a per-lesson "Πηγές" modal, add curriculum rename/delete, and add clean DOCX export.

**Architecture:** Spec: `docs/superpowers/specs/2026-07-20-curriculum-control-design.md`. Part 5 (planning chat) is deliberately NOT in this plan — it gets its own plan after these land. Backend is FastAPI (`apps/api`), frontend Next.js (`apps/web`). A curriculum is a `Block` tree: course → module → lesson → segment; citations live in `meta.citations` JSON, never in prose.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / pytest; TypeScript / Next.js / next-intl / Playwright; python-docx (new).

## Global Constraints

- Default locale is Greek (`el`) — every user-facing string needs `el` + `en` entries in `apps/web/src/messages/{el,en}.json`. Greek is the primary copy, not the translation.
- CPU-only, no GPU, ever. New Python deps must be pure-Python or lightweight C wheels (python-docx is pure Python — OK).
- Backend tests: `cd apps/api && python -m pytest tests/ -x -q` (needs the `guitar_test` Postgres on port 5434 — conftest pins `DATABASE_URL` itself).
- Frontend checks: `cd apps/web && npx tsc --noEmit` and `npm run build`. Playwright: `npx playwright test` (needs the dev stack running; if unavailable, note it and continue — do not fake a pass).
- Never mutate `wire`/message dicts in place — `app/routers/chat.py` diffs against the same dict objects (`loop.py:632-643` explains why). Always `{**old, ...}` replacement.
- Commit after every task, message style: `fix(scope): ...` / `feat(scope): ...` as in `git log`.

---

### Task 1: `strip_curriculum_context` helper + sentinel constant

**Files:**
- Modify: `apps/api/app/agent/guards.py` (append after `NAMED_SONG_DECLINE_MESSAGE`, ~line 349)
- Modify: `apps/api/app/routers/chat.py:272-283` (`_inject_curriculum_context` uses the shared sentinel)
- Test: `apps/api/tests/test_agent_guards.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `CURRICULUM_CONTEXT_SENTINEL: str` (value `"[CURRICULUM CONTEXT"`) and `strip_curriculum_context(text: str) -> str`, both importable from `app.agent.guards`. Task 2 depends on both.

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_agent_guards.py`:

```python
from app.agent.guards import (  # extend the module's existing import if present
    CURRICULUM_CONTEXT_SENTINEL,
    strip_curriculum_context,
    looks_like_named_song_request,
)

_CTX_TAIL = (
    "\n\n" + CURRICULUM_CONTEXT_SENTINEL + " — this conversation is about "
    'curriculum abc titled "Guitar Tone & Amps".\nCurrent structure:\n'
    "[uuid-1] Intro to Tone — what makes an amp sing\n"
    "[uuid-2] Solo riffs and sustain — Gilmour-style bends]"
)


def test_strip_curriculum_context_removes_injected_tail():
    raw = "μπορείς να αφαιρέσεις όλα τα inline citations από αυτό το curricula;"
    assert strip_curriculum_context(raw + _CTX_TAIL) == raw


def test_strip_curriculum_context_is_identity_without_tail():
    assert strip_curriculum_context("plain question") == "plain question"
    assert strip_curriculum_context("") == ""


def test_enriched_text_trips_guard_but_stripped_text_does_not():
    # Pins the EXACT production bug: the raw Greek request is innocent, the
    # injected course tree (full of "Intro"/"Solo" titles) is what triggers.
    raw = "μπορείς να αφαιρέσεις όλα τα inline citations από αυτό το curricula;"
    assert looks_like_named_song_request(raw + _CTX_TAIL) is True   # the bug
    assert looks_like_named_song_request(strip_curriculum_context(raw + _CTX_TAIL)) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && python -m pytest tests/test_agent_guards.py -q -k curriculum_context`
Expected: FAIL — `ImportError: cannot import name 'CURRICULUM_CONTEXT_SENTINEL'`

- [ ] **Step 3: Implement** — append to `apps/api/app/agent/guards.py` after `NAMED_SONG_DECLINE_MESSAGE`:

```python
# `app/routers/chat.py`'s `_inject_curriculum_context` appends a transient
# "[CURRICULUM CONTEXT — ...]" block (this exact prefix) onto the LAST user
# message so the model can reason about the bound curriculum. The G5 guard
# above must NEVER scan that block: a guitar course tree reliably contains
# trigger words ("Intro to Tone", "Solo riffs"), so scanning it declines
# EVERY revise-drawer turn with NAMED_SONG_DECLINE_MESSAGE, deterministically,
# no matter what the tutor typed — the 2026-07-19 "remove the inline
# citations" bug. The loop passes the raw tutor text explicitly
# (`raw_user_text`); this strip is the defense-in-depth fallback for any
# caller that doesn't.
CURRICULUM_CONTEXT_SENTINEL = "[CURRICULUM CONTEXT"


def strip_curriculum_context(text: str) -> str:
    """Return `text` without the trailing injected curriculum-context block.

    The block is always APPENDED (never prepended/interleaved — see
    `_inject_curriculum_context`), so everything from the first sentinel
    occurrence onward is injection, not tutor words.
    """
    idx = text.find(CURRICULUM_CONTEXT_SENTINEL)
    if idx == -1:
        return text
    return text[:idx].rstrip()
```

Then in `apps/api/app/routers/chat.py`: add `CURRICULUM_CONTEXT_SENTINEL` to the existing `from app.agent.guards import ...` (or add the import if none exists — check the file's imports; `NAMED_SONG_DECLINE_MESSAGE` is imported by `loop.py`, chat.py may not import guards yet), and in `_inject_curriculum_context` change the literal prefix:

```python
    ctx = (
        f"\n\n{CURRICULUM_CONTEXT_SENTINEL} — this conversation is about curriculum "
        f"{course.id} titled \"{course.title}\". ANSWER QUESTIONS ABOUT IT (what a "
        # ... rest of the f-string chain UNCHANGED ...
```

(Only the first line changes: `[CURRICULUM CONTEXT` literal → `{CURRICULUM_CONTEXT_SENTINEL}`. One source of truth so the strip can never drift from the injection.)

- [ ] **Step 4: Run tests**

Run: `cd apps/api && python -m pytest tests/test_agent_guards.py tests/test_chat_router.py -q`
Expected: PASS (chat router tests confirm the ctx string is byte-identical).

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agent/guards.py apps/api/app/routers/chat.py apps/api/tests/test_agent_guards.py
git commit -m "fix(guards): shared curriculum-context sentinel + strip helper"
```

---

### Task 2: Loop judges the raw tutor text (the actual bug fix)

**Files:**
- Modify: `apps/api/app/agent/loop.py:598-599` and `:656-665` (`run_agent_turn`), `:853-855` and `:892-901` (`stream_plain_turn`)
- Modify: `apps/api/app/routers/chat.py:623` and `:686`
- Test: `apps/api/tests/test_agent_loop.py` (append — this module already has `_FakeProvider`, the registry stub, and `search` monkeypatch patterns; reuse them)

**Interfaces:**
- Consumes: `strip_curriculum_context` from Task 1.
- Produces: `run_agent_turn(db, messages, *, locale=..., max_steps=6, raw_user_text: str | None = None)` and `stream_plain_turn(db, messages, *, locale=..., raw_user_text: str | None = None)`. Callers not passing `raw_user_text` keep today's behavior minus the injected-tail scan (the strip fallback).

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_agent_loop.py` (mirror the module's existing setup: `_FakeProvider`, `monkeypatch.setattr(agent_loop, "search", ...)`, `run_agent_turn(None, messages)`):

```python
_REVISE_CTX = (
    "\n\n[CURRICULUM CONTEXT — this conversation is about curriculum abc "
    'titled "Guitar Tone & Amps".\nCurrent structure:\n'
    "[uuid-1] Intro to Tone — what makes an amp sing\n"
    "[uuid-2] Solo riffs and sustain — Gilmour-style bends]"
)
_RAW_REVISE_REQUEST = "μπορείς να αφαιρέσεις όλα τα inline citations από αυτό το curricula;"


def test_revise_turn_with_injected_curriculum_reaches_the_model(monkeypatch):
    """2026-07-19 regression: the injected course tree ("Intro", "Solo" titles)
    must NOT trip the named-song guard — the model must see the request."""
    fake_provider = _FakeProvider([AssistantTurn(content="Έγινε.", tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(agent_loop, "search", lambda db, q, k=5: [])

    messages = [{"role": "user", "content": _RAW_REVISE_REQUEST + _REVISE_CTX}]
    result = run_agent_turn(None, messages, locale="el", raw_user_text=_RAW_REVISE_REQUEST)

    assert result.content == "Έγινε."
    assert len(fake_provider.calls) == 1  # the model WAS called


def test_revise_turn_without_raw_text_still_reaches_model_via_strip_fallback(monkeypatch):
    fake_provider = _FakeProvider([AssistantTurn(content="Έγινε.", tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(agent_loop, "search", lambda db, q, k=5: [])

    messages = [{"role": "user", "content": _RAW_REVISE_REQUEST + _REVISE_CTX}]
    result = run_agent_turn(None, messages, locale="el")  # no raw_user_text

    assert result.content == "Έγινε."


def test_genuine_named_song_request_still_declined_inside_revise_session(monkeypatch):
    """The injection must not MASK a real fabrication request either."""
    fake_provider = _FakeProvider([AssistantTurn(content="never reached", tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)
    monkeypatch.setattr(agent_loop, "search", lambda db, q, k=5: [])  # library miss

    raw = "γράψε μου το tab για το Nothing Else Matters"
    messages = [{"role": "user", "content": raw + _REVISE_CTX}]
    result = run_agent_turn(None, messages, locale="el", raw_user_text=raw)

    assert result.content == NAMED_SONG_DECLINE_MESSAGE
    assert len(fake_provider.calls) == 0  # pre-model short-circuit intact


def test_search_query_is_the_raw_text_not_the_enriched_blob(monkeypatch):
    """Retrieval pollution half of the bug: the BM25/dense query must be the
    tutor's words, not words + the whole serialized course tree."""
    fake_provider = _FakeProvider([AssistantTurn(content="ok", tool_calls=[])])
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)
    seen_queries = []
    monkeypatch.setattr(
        agent_loop, "search",
        lambda db, q, k=5: (seen_queries.append(q), [])[1],
    )

    raw = "τι είναι το ρελέ του ενισχυτή;"  # content-bearing → triggers the pre-hop
    messages = [{"role": "user", "content": raw + _REVISE_CTX}]
    run_agent_turn(None, messages, locale="el", raw_user_text=raw)

    assert seen_queries == [raw]
```

Add a streaming twin (same file) for `stream_plain_turn`:

```python
def test_stream_revise_turn_with_injected_curriculum_reaches_the_model(monkeypatch):
    fake_provider = _FakeStreamProvider(  # if the module has no stream fake, see
        content="Έγινε.")                  # tests/test_chat_stream_router.py and
    monkeypatch.setattr(agent_loop, "get_provider", lambda: fake_provider)  # reuse its fake
    monkeypatch.setattr(agent_loop, "search", lambda db, q, k=5: [])

    messages = [{"role": "user", "content": _RAW_REVISE_REQUEST + _REVISE_CTX}]
    events = list(agent_loop.stream_plain_turn(None, messages, locale="el",
                                               raw_user_text=_RAW_REVISE_REQUEST))

    done = [e for e in events if e["event"] == "done"]
    assert done and done[0]["content"] == "Έγινε."
    assert done[0]["content"] != NAMED_SONG_DECLINE_MESSAGE
```

(Before writing it, check `tests/test_chat_stream_router.py` / `tests/test_agent_loop.py` for the existing streaming fake-provider class and use that one verbatim — do not invent a second fake.)

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd apps/api && python -m pytest tests/test_agent_loop.py -q -k "revise or raw_text"`
Expected: FAIL — `TypeError: run_agent_turn() got an unexpected keyword argument 'raw_user_text'`

- [ ] **Step 3: Implement** — in `apps/api/app/agent/loop.py`:

3a. Extend the guards import at the top of the file with `strip_curriculum_context`.

3b. `run_agent_turn` signature (line 598):

```python
def run_agent_turn(
    db, messages: list[dict], *, locale: str = DEFAULT_LOCALE, max_steps: int = 6,
    raw_user_text: str | None = None,
) -> AgentResult:
```

3c. Replace the C1 pre-hop block (lines 656–665) — `guard_text` is the tutor's own words, used for the G5 guard, the content-bearing gate, AND the retrieval query; `last_text` (possibly enriched by the router) stays what the model sees:

```python
    last = messages[-1]
    hits: list = []
    is_user_turn = last.get("role") == "user"
    last_text = last.get("content") or ""
    # G5's invariant ("judge the tutor's own words" — see the comment above)
    # now holds against the ROUTER's injection too, not just this loop's own
    # grounding tail: `app/routers/chat.py` appends the whole curriculum tree
    # to `last_text` before we ever run, and a guitar course tree contains
    # trigger words in its lesson titles. The router passes the raw text
    # explicitly; the strip is the fallback for callers that don't.
    guard_text = raw_user_text if raw_user_text is not None else strip_curriculum_context(last_text)
    named_song = is_user_turn and looks_like_named_song_request(guard_text)
    if is_user_turn and (named_song or _is_content_bearing(guard_text)):
        hits = search(db, guard_text, k=5)
        citations = [_to_citation(hit) for hit in hits]
        grounded_content = f"{last_text}\n\n{_grounding_block(hits, locale, db)}"
        messages[-1] = {**last, "content": grounded_content}
```

3d. `stream_plain_turn` (line 853): add the same `raw_user_text: str | None = None` keyword, and apply the identical `guard_text` replacement to its pre-hop block (lines 892–901) — the module's documented "small deliberate duplication" precedent applies; keep the two blocks textually parallel.

3e. In `apps/api/app/routers/chat.py`, thread the raw text through both endpoints:

```python
        result = run_agent_turn(db, wire, locale=session.locale, raw_user_text=payload.content)
```

(line 623) and

```python
            for event in stream_plain_turn(db, wire, locale=session.locale, raw_user_text=payload.content):
```

(inside `event_stream`, line 686).

- [ ] **Step 4: Run the full agent + chat suites**

Run: `cd apps/api && python -m pytest tests/test_agent_loop.py tests/test_agent_guards.py tests/test_agent_grounding.py tests/test_chat_router.py tests/test_chat_stream_router.py -q`
Expected: PASS. If any pre-existing grounding test pinned the ENRICHED text as the search query, that test encoded the bug — update it to expect the raw text and say so in the commit message.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agent/loop.py apps/api/app/routers/chat.py apps/api/tests/test_agent_loop.py
git commit -m "fix(agent): G5 guard + retrieval judge the raw tutor text, never the injected curriculum tree"
```

---

### Task 3: Citations — remove per-section pills, add per-lesson "Πηγές" modal

**Files:**
- Create: `apps/web/src/components/curriculum/lesson-sources.tsx`
- Modify: `apps/web/src/components/curriculum/block-card.tsx:506` (and its `ProvenanceChips` import)
- Modify: `apps/web/src/messages/el.json`, `apps/web/src/messages/en.json` (under `curricula.tree`)
- Modify: `apps/web/tests/interview.spec.ts:745-770` (the two `provenance-chip` assertions)
- Keep: `apps/web/src/components/curriculum/provenance-chips.tsx` (unused by the board after this; delete only if nothing else imports it — `grep -rn "curriculum/provenance-chips" apps/web/src` and remove the file when the only hit was block-card)

**Interfaces:**
- Consumes: `Citation` type from `@/lib/api` (`{source_id, source_ref, source_title, page}`), `Dialog` primitives from `@/components/ui/dialog`, lesson `meta.citations` (already the union of all its sections' citations — `draft.py:581-589`).
- Produces: `<LessonSources citations={Citation[]} locale={string} lessonTitle={string} />` — button + modal, self-contained.

- [ ] **Step 1: Create the component** — `apps/web/src/components/curriculum/lesson-sources.tsx`:

```tsx
"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { BookOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import type { Citation } from "@/lib/api";

interface LessonSourcesProps {
  citations: Citation[] | undefined;
  locale: string;
  lessonTitle: string;
}

/** One compact "Πηγές" button per lesson → modal listing the lesson's sources,
 * grouped by book/article with its cited pages as Reader deep-links.
 *
 * Replaces the per-SECTION ProvenanceChips rows: the grounding data is the
 * tutor's trust signal, but spread under every section it drowned the lessons
 * it was meant to support. Same data (`lesson.meta.citations`, the union
 * `draft.py` already persists), same validated (source, page) pairs, same
 * deep-links — one click away instead of everywhere. */
export function LessonSources({ citations, locale, lessonTitle }: LessonSourcesProps) {
  const t = useTranslations("curricula.tree");
  const [open, setOpen] = useState(false);

  const grouped = useMemo(() => {
    const bySource = new Map<string, { title: string; pages: number[] }>();
    for (const c of citations ?? []) {
      const entry = bySource.get(c.source_id) ?? { title: c.source_title ?? c.source_ref, pages: [] };
      if (!entry.pages.includes(c.page)) entry.pages.push(c.page);
      bySource.set(c.source_id, entry);
    }
    return [...bySource.entries()].map(([sourceId, e]) => ({
      sourceId, title: e.title, pages: [...e.pages].sort((a, b) => a - b),
    }));
  }, [citations]);

  if (grouped.length === 0) return null;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="ghost" data-testid="lesson-sources-trigger">
          <BookOpen className="size-3.5 text-primary" aria-hidden />
          {t("sourcesButton")}
        </Button>
      </DialogTrigger>
      <DialogContent data-testid="lesson-sources-modal">
        <DialogHeader>
          <DialogTitle>{t("sourcesTitle", { lesson: lessonTitle })}</DialogTitle>
        </DialogHeader>
        <ul className="flex flex-col gap-3">
          {grouped.map((s) => (
            <li key={s.sourceId} data-testid="lesson-source-item" className="flex flex-col gap-1">
              <span className="text-sm font-medium">{s.title}</span>
              <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                {s.pages.map((page) => (
                  <Link
                    key={page}
                    href={`/${locale}/library/${s.sourceId}?page=${page}`}
                    data-testid="lesson-source-page"
                    className="rounded-full border border-primary/25 bg-primary/5 px-2 py-0.5 transition-colors hover:border-primary/60 hover:text-foreground"
                    onClick={() => setOpen(false)}
                  >
                    {t("sourcesPage", { page })}
                  </Link>
                ))}
              </span>
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
```

(Check `apps/web/src/components/ui/dialog.tsx` export names first and match them exactly — if it exports e.g. `DialogTrigger` differently or requires a `DialogDescription`, follow the file. Other dialogs in `components/curriculum/` show the house pattern.)

- [ ] **Step 2: Rewire `block-card.tsx`** — replace line 506:

```tsx
      {isSegment && !editingBody && <ProvenanceChips citations={meta.citations} locale={locale} />}
```

with

```tsx
      {isLesson && !editingBody && (
        <LessonSources citations={meta.citations} locale={locale} lessonTitle={node.title} />
      )}
```

and swap the import: remove `ProvenanceChips`, import `{ LessonSources } from "./lesson-sources"`. (Lesson blocks already carry the citation union in `meta.citations`; segments keep theirs in data, just unrendered.)

- [ ] **Step 3: i18n** — add to `apps/web/src/messages/el.json` under the existing `curricula.tree` object (which already holds `citation`):

```json
"sourcesButton": "Πηγές",
"sourcesTitle": "Πηγές: {lesson}",
"sourcesPage": "σελ. {page}"
```

and to `en.json`:

```json
"sourcesButton": "Sources",
"sourcesTitle": "Sources: {lesson}",
"sourcesPage": "p. {page}"
```

- [ ] **Step 4: Update the Playwright spec** — `apps/web/tests/interview.spec.ts` lines ~751 and ~765 assert `provenance-chip` visibility inside an expanded segment. Rewrite those assertions to the new flow (the surrounding test's setup stays):

```ts
await expect(page.getByTestId("lesson-sources-trigger").first()).toBeVisible();
await page.getByTestId("lesson-sources-trigger").first().click();
await expect(page.getByTestId("lesson-sources-modal")).toBeVisible();
const pageLink = page.getByTestId("lesson-source-page").first();
await expect(pageLink).toHaveAttribute("href", /\/library\/.+\?page=\d+/);
```

Read the two touched tests fully first — keep whatever they assert about the Reader landing page, driven through `lesson-source-page` instead of `provenance-chip`.

- [ ] **Step 5: Verify**

Run: `cd apps/web && npx tsc --noEmit && npm run build`
Expected: clean. Then `npx playwright test tests/interview.spec.ts` if the stack is up.

- [ ] **Step 6: Commit**

```bash
git add apps/web/src/components/curriculum/lesson-sources.tsx apps/web/src/components/curriculum/block-card.tsx apps/web/src/messages/el.json apps/web/src/messages/en.json apps/web/tests/interview.spec.ts
git commit -m "feat(curriculum): per-lesson Πηγές modal replaces per-section citation pills"
```

---

### Task 4: Rename & delete endpoints (backend)

**Files:**
- Modify: `apps/api/app/routers/curriculum.py` (new routes after `get_curriculum`, ~line 356)
- Modify: `apps/api/app/schemas/curriculum.py` (add `CurriculumRenameRequest`)
- Test: `apps/api/tests/test_curricula_manage.py` (new)

**Interfaces:**
- Consumes: `Block`, `ChatSession`, `CurriculumInterview`, `GenerationJob` models; `edit_service.delete_block` (already renormalizes sibling order); `_get_block_or_404`.
- Produces: `PATCH /curricula/{root_id}` (body `{"title": str}`, returns `CurriculumListItem`) and `DELETE /curricula/{root_id}` (204). Task 5's frontend wrappers call exactly these.

- [ ] **Step 1: Write the failing tests** — `apps/api/tests/test_curricula_manage.py`:

```python
"""Rename/delete for curriculum roots — the dedicated /curricula management
routes (spec 2026-07-20 §3). The generic /blocks routes stay untouched; these
add course-root validation and delete-side cleanup of the FK-less pointers
(ChatSession.root_id, CurriculumInterview.root_id)."""
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.block import Block
from app.models.chat import ChatSession, Message
from app.models.interview import CurriculumInterview
from app.models.generation_job import GenerationJob

client = TestClient(app)


def _mk_course(db, title="Ήχος και Ενισχυτές"):
    course = Block(kind="course", title=title, is_template=True, order=0, plane="content")
    db.add(course); db.flush()
    module = Block(kind="module", title="Ενότητα 1", parent_id=course.id, order=0, plane="content")
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Μάθημα 1", parent_id=module.id, order=0, plane="content",
                   meta={"draft_status": "ready"})
    db.add(lesson); db.commit()
    return course, module, lesson


def test_rename_curriculum():
    db = SessionLocal()
    try:
        course, _, _ = _mk_course(db)
        r = client.patch(f"/curricula/{course.id}", json={"title": "Νέος τίτλος"})
        assert r.status_code == 200
        assert r.json()["title"] == "Νέος τίτλος"
        db.expire_all()
        assert db.get(Block, course.id).title == "Νέος τίτλος"
    finally:
        db.close()


def test_rename_rejects_empty_title_and_non_roots():
    db = SessionLocal()
    try:
        course, module, _ = _mk_course(db)
        assert client.patch(f"/curricula/{course.id}", json={"title": ""}).status_code == 422
        assert client.patch(f"/curricula/{module.id}", json={"title": "x"}).status_code == 404
    finally:
        db.close()


def test_delete_curriculum_cascades_and_cleans_bound_rows():
    db = SessionLocal()
    try:
        course, module, lesson = _mk_course(db)
        chat = ChatSession(root_id=course.id, locale="el")
        db.add(chat); db.flush()
        db.add(Message(session_id=chat.id, role="user", content="γεια"))
        db.add(CurriculumInterview(root_id=course.id))
        db.commit()
        chat_id, course_id = chat.id, course.id

        r = client.delete(f"/curricula/{course_id}")
        assert r.status_code == 204
        db.expire_all()
        assert db.get(Block, course_id) is None
        assert db.get(Block, module.id) is None and db.get(Block, lesson.id) is None
        assert db.get(ChatSession, chat_id) is None
        assert db.query(CurriculumInterview).filter_by(root_id=course_id).count() == 0
    finally:
        db.close()


def test_delete_refuses_while_drafting():
    db = SessionLocal()
    try:
        course, _, lesson = _mk_course(db)
        lesson.meta = {**(lesson.meta or {}), "draft_status": "drafting"}
        db.commit()
        r = client.delete(f"/curricula/{course.id}")
        assert r.status_code == 409
        db.expire_all()
        assert db.get(Block, course.id) is not None
    finally:
        db.close()


def test_delete_non_root_is_404():
    db = SessionLocal()
    try:
        _, module, _ = _mk_course(db)
        assert client.delete(f"/curricula/{module.id}").status_code == 404
    finally:
        db.close()
```

Before running: check `CurriculumInterview`'s NOT NULL constructor requirements (`apps/api/app/models/interview.py` — it has `answers` defaulting to dict etc.; if a `title`/`step` field is required, set it in the test) and `Message`'s required fields (`session_id, role, content` — see `models/chat.py`). Adjust ONLY constructor kwargs, not assertions.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/api && python -m pytest tests/test_curricula_manage.py -q`
Expected: FAIL — 405/404 on PATCH/DELETE `/curricula/{id}` (routes don't exist).

- [ ] **Step 3: Implement.** In `apps/api/app/schemas/curriculum.py` (near `CurriculumListItem`):

```python
class CurriculumRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
```

In `apps/api/app/routers/curriculum.py`, after `get_curriculum` (~line 356) — imports for `ChatSession`, `Message`, `CurriculumInterview` follow the file's existing import style:

```python
def _get_course_root_or_404(db: Session, root_id: UUID) -> Block:
    """The management routes below act on CURRICULUM ROOTS only — a module or
    lesson id must 404 here (the generic /blocks routes handle those), so a
    frontend bug can never cascade-delete a whole course through this door
    while claiming to remove one lesson."""
    block = db.get(Block, root_id)
    if block is None or block.kind != "course" or block.parent_id is not None:
        raise HTTPException(status_code=404, detail="curriculum not found")
    return block


@router.patch("/curricula/{root_id}", response_model=CurriculumListItem)
def rename_curriculum(
    root_id: UUID, payload: CurriculumRenameRequest, db: Session = Depends(get_db)
) -> CurriculumListItem:
    course = _get_course_root_or_404(db, root_id)
    course.title = payload.title.strip()
    if not course.title:
        raise HTTPException(status_code=422, detail="title cannot be empty")
    db.commit()
    return CurriculumListItem.model_validate(course, from_attributes=True)


@router.delete("/curricula/{root_id}", status_code=204, response_model=None)
def delete_curriculum(root_id: UUID, db: Session = Depends(get_db)) -> None:
    course = _get_course_root_or_404(db, root_id)

    # Refuse while lessons are actively being written: deleting the tree from
    # under the draft worker strands the job mid-write (it re-reads its lesson
    # block between sections). "queued" alone doesn't block — a freshly
    # materialized-but-never-drafted course must be deletable.
    drafting = db.scalars(
        select(Block).where(
            Block.parent_id.in_(select(Block.id).where(Block.parent_id == course.id)),
            Block.kind == "lesson",
        )
    ).all()
    if any((b.meta or {}).get("draft_status") == "drafting" for b in drafting):
        raise HTTPException(
            status_code=409,
            detail="lessons are still being drafted — wait for the draft to finish before deleting",
        )

    # The FK-less pointers (deliberate — see models/chat.py's root_id comment):
    # an EXPLICIT curriculum delete takes its bound revise-chat sessions and
    # interviews with it. That comment's concern is board edits wiping
    # conversations as a SIDE effect; this is the tutor saying "delete this
    # course", and a revise chat about a course that no longer exists is
    # noise in his sidebar, not a record worth keeping.
    session_ids = db.scalars(
        select(ChatSession.id).where(ChatSession.root_id == course.id)
    ).all()
    if session_ids:
        db.query(Message).filter(Message.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(ChatSession).filter(ChatSession.id.in_(session_ids)).delete(synchronize_session=False)
    db.query(CurriculumInterview).filter(CurriculumInterview.root_id == course.id).delete(
        synchronize_session=False
    )
    db.commit()

    edit_service.delete_block(db, course.id)
```

Adjustments to verify while implementing: (a) whether `Message` rows FK-cascade from `ChatSession` (check `models/chat.py` — if `cascade="all, delete-orphan"` exists on the relationship, drop the manual `Message` delete); (b) whether `PendingApproval` (or similarly named) rows reference sessions — if yes, delete those the same way; grep `models/chat.py` for other `session_id` FKs. (c) `edit_service.delete_block` commits internally (the existing `DELETE /blocks` route calls it bare) — keep the cleanup commit BEFORE it so a failure never leaves a half-deleted state with the course gone but sessions kept.

- [ ] **Step 4: Run**

Run: `cd apps/api && python -m pytest tests/test_curricula_manage.py tests/test_chat_models.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/routers/curriculum.py apps/api/app/schemas/curriculum.py apps/api/tests/test_curricula_manage.py
git commit -m "feat(curricula): dedicated rename/delete routes with course-root validation + bound-row cleanup"
```

---

### Task 5: Rename & delete UI

**Files:**
- Create: `apps/web/src/components/curriculum/curriculum-actions-menu.tsx`
- Modify: `apps/web/src/lib/api.ts` (wrappers near `listCurricula`, line ~860)
- Modify: `apps/web/src/app/[locale]/(cockpit)/curricula/page.tsx` (card layout + menu)
- Modify: `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx` (header menu; read the file first — place the menu beside the title, navigate to the index on delete)
- Modify: `apps/web/src/messages/el.json`, `en.json` (under `curricula`)

**Interfaces:**
- Consumes: Task 4's endpoints; `useConfirm` from `@/components/ui/confirm`; `DropdownMenu` primitives from `@/components/ui/dropdown-menu`; `Dialog` from `@/components/ui/dialog`.
- Produces: `renameCurriculum(rootId, title): Promise<CurriculumListItem>`, `deleteCurriculum(rootId): Promise<void>` in `api.ts`; `<CurriculumActionsMenu rootId title onRenamed(next) onDeleted() />`.

- [ ] **Step 1: api.ts wrappers** — after `listCurricula` (line ~861):

```ts
export function renameCurriculum(rootId: string, title: string): Promise<CurriculumListItem> {
  return request<CurriculumListItem>(`/curricula/${rootId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export function deleteCurriculum(rootId: string): Promise<void> {
  return request<void>(`/curricula/${rootId}`, { method: "DELETE" });
}
```

(Match the file's actual `request` helper signature — neighbors like `updateBlock` at line 1055 show the exact shape, including whether headers/body serialization are implicit.)

- [ ] **Step 2: The menu component** — `apps/web/src/components/curriculum/curriculum-actions-menu.tsx`:

```tsx
"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { MoreHorizontal, Pencil, Trash2, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useConfirm } from "@/components/ui/confirm";
import { ApiError, deleteCurriculum, renameCurriculum } from "@/lib/api";

interface CurriculumActionsMenuProps {
  rootId: string;
  title: string;
  onRenamed: (nextTitle: string) => void;
  onDeleted: () => void;
}

/** The "⋯" on a curriculum — Rename (dialog) and Delete (confirm, destructive).
 * Used on the index cards and the detail header; the CALLER decides what
 * follows (refresh the list / navigate away). */
export function CurriculumActionsMenu({ rootId, title, onRenamed, onDeleted }: CurriculumActionsMenuProps) {
  const t = useTranslations("curricula.actions");
  const confirm = useConfirm();
  const [renameOpen, setRenameOpen] = useState(false);
  const [draftTitle, setDraftTitle] = useState(title);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submitRename = async (e: React.FormEvent) => {
    e.preventDefault();
    const next = draftTitle.trim();
    if (!next || next === title) { setRenameOpen(false); return; }
    setBusy(true); setError(null);
    try {
      await renameCurriculum(rootId, next);
      setRenameOpen(false);
      onRenamed(next);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("renameError"));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    const ok = await confirm({
      title: t("deleteTitle"),
      description: t("deleteDescription", { title }),
      confirmLabel: t("deleteConfirm"),
      destructive: true,
    });
    if (!ok) return;
    setBusy(true); setError(null);
    try {
      await deleteCurriculum(rootId);
      onDeleted();
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("deleteError"));
      setBusy(false);
    }
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" size="sm" variant="ghost" data-testid="curriculum-actions-trigger"
                  aria-label={t("menuLabel")} disabled={busy}>
            {busy ? <Loader2 className="animate-spin" /> : <MoreHorizontal />}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem data-testid="curriculum-rename"
                            onSelect={() => { setDraftTitle(title); setRenameOpen(true); }}>
            <Pencil /> {t("rename")}
          </DropdownMenuItem>
          <DropdownMenuItem data-testid="curriculum-delete" variant="destructive" onSelect={handleDelete}>
            <Trash2 /> {t("delete")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent data-testid="curriculum-rename-dialog">
          <DialogHeader><DialogTitle>{t("renameTitle")}</DialogTitle></DialogHeader>
          <form onSubmit={submitRename} className="flex flex-col gap-3">
            <Input value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)}
                   data-testid="curriculum-rename-input" autoFocus disabled={busy} />
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={busy} onClick={() => setRenameOpen(false)}>
                {t("cancel")}
              </Button>
              <Button type="submit" disabled={busy || !draftTitle.trim()} data-testid="curriculum-rename-save">
                {busy && <Loader2 className="animate-spin" />} {t("renameSave")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
```

(Verify against the actual exports: `ui/confirm.tsx`'s `useConfirm` option names — read its `ConfirmFn` type and match; `ui/dropdown-menu.tsx` may not have a `variant="destructive"` item prop — if not, use `className="text-destructive"`. The board's `block-card.tsx` ⋯ menu is the house reference.)

- [ ] **Step 3: Index page wiring** — in `curricula/page.tsx`, the card is currently a whole-`<Link>` (lines 102–117). Restructure so the menu doesn't navigate: wrap in a relative `div`, keep the `Link` as the card body, absolutely position the menu top-right:

```tsx
{shown.map((item) => (
  <div key={item.id} className="relative" data-testid="template-item">
    <Link
      href={`/${locale}/curricula/${item.id}`}
      className="flex w-56 flex-col gap-1 rounded-xl border border-border bg-card p-3 pr-10 text-left text-sm ring-1 ring-foreground/10 transition-colors hover:bg-muted/50"
    >
      <span className="truncate font-medium" data-testid="template-title">{item.title}</span>
      <span className="flex items-center gap-2 text-xs text-muted-foreground">
        <Badge variant="outline">{item.language}</Badge>
        {typeof item.target_profile?.level === "string" && <span>{item.target_profile.level}</span>}
      </span>
    </Link>
    <div className="absolute right-1 top-1">
      <CurriculumActionsMenu
        rootId={item.id}
        title={item.title}
        onRenamed={() => fetchTemplates()}
        onDeleted={() => fetchTemplates()}
      />
    </div>
  </div>
))}
```

(`data-testid="template-item"` moves to the wrapper so existing Playwright selectors keep matching; `template-title` stays on the span.) Import `CurriculumActionsMenu`.

- [ ] **Step 4: Detail page header** — read `curricula/[rootId]/page.tsx`, place `<CurriculumActionsMenu>` beside the title with `onRenamed` updating the local tree/title state and `onDeleted={() => router.push(`/${locale}/curricula`)}` (the index). Follow the page's existing state patterns.

- [ ] **Step 5: i18n** — `el.json`, new `curricula.actions` object:

```json
"actions": {
  "menuLabel": "Ενέργειες προγράμματος",
  "rename": "Μετονομασία",
  "renameTitle": "Μετονομασία προγράμματος",
  "renameSave": "Αποθήκευση",
  "renameError": "Η μετονομασία απέτυχε — δοκίμασε ξανά.",
  "delete": "Διαγραφή",
  "deleteTitle": "Διαγραφή προγράμματος;",
  "deleteDescription": "Θα διαγραφεί οριστικά το «{title}» και όλα τα μαθήματά του. Δεν υπάρχει αναίρεση.",
  "deleteConfirm": "Διαγραφή οριστικά",
  "deleteError": "Η διαγραφή απέτυχε — δοκίμασε ξανά.",
  "cancel": "Άκυρο"
}
```

`en.json`:

```json
"actions": {
  "menuLabel": "Curriculum actions",
  "rename": "Rename",
  "renameTitle": "Rename curriculum",
  "renameSave": "Save",
  "renameError": "Rename failed — try again.",
  "delete": "Delete",
  "deleteTitle": "Delete curriculum?",
  "deleteDescription": "“{title}” and all of its lessons will be permanently deleted. This cannot be undone.",
  "deleteConfirm": "Delete permanently",
  "deleteError": "Delete failed — try again.",
  "cancel": "Cancel"
}
```

- [ ] **Step 6: Verify + commit**

Run: `cd apps/web && npx tsc --noEmit && npm run build` — clean.

```bash
git add apps/web/src/components/curriculum/curriculum-actions-menu.tsx apps/web/src/lib/api.ts "apps/web/src/app/[locale]/(cockpit)/curricula/page.tsx" "apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx" apps/web/src/messages/el.json apps/web/src/messages/en.json
git commit -m "feat(curricula): rename/delete via ⋯ menu with Greek confirm modal"
```

---

### Task 6: DOCX export (backend)

**Files:**
- Modify: `apps/api/pyproject.toml` (add dependency)
- Create: `apps/api/app/curriculum/export_docx.py`
- Modify: `apps/api/app/routers/curriculum.py` (route after `get_curriculum`)
- Test: `apps/api/tests/test_export_docx.py` (new)

**Interfaces:**
- Consumes: the Block tree (course → module → lesson → segment, ordered by `order`; segment `title` = blueprint label, `body` = teaching prose; lesson `meta.draft_status`).
- Produces: `build_curriculum_docx(db, course: Block) -> io.BytesIO` and `GET /curricula/{root_id}/export.docx`. Task 7 calls the route.

- [ ] **Step 1: Dependency** — in `apps/api/pyproject.toml` dependencies list:

```toml
  # DOCX export (spec 2026-07-20 §4) — pure Python, no native deps, works in
  # the future bundled desktop app. Backend-side on purpose: one source of
  # truth for the document, testable with pytest, identical output in Tauri.
  "python-docx>=1.1",
```

Then `cd apps/api && pip install -e .[dev]` (or the project's usual sync command — check how the venv is managed, e.g. `uv pip install -e .`).

- [ ] **Step 2: Write the failing test** — `apps/api/tests/test_export_docx.py`:

```python
import io

from docx import Document
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models.block import Block

client = TestClient(app)


def _mk_tree(db):
    course = Block(kind="course", title="Ήχος και Ενισχυτές", is_template=True,
                   order=0, plane="content", target_profile={"level": "μεσαίο"})
    db.add(course); db.flush()
    m1 = Block(kind="module", title="Βασικές αρχές ήχου", parent_id=course.id, order=0, plane="content")
    db.add(m1); db.flush()
    l1 = Block(kind="lesson", title="Τι είναι το gain", parent_id=m1.id, order=0,
               plane="content", est_minutes=55,
               meta={"draft_status": "ready",
                     "citations": [{"source_id": "s1", "source_ref": "b1",
                                    "source_title": "Getting Great Guitar Sounds", "page": 19}]})
    db.add(l1); db.flush()
    db.add(Block(kind="segment", title="Θεωρία", parent_id=l1.id, order=0, plane="content",
                 body="Το gain καθορίζει την προενίσχυση του σήματος.",
                 meta={"section": "theory"}))
    db.add(Block(kind="segment", title="Ασκήσεις", parent_id=l1.id, order=1, plane="content",
                 body="Άσκηση 1: σύγκρινε clean και overdriven ήχο.",
                 meta={"section": "exercises"}))
    l2 = Block(kind="lesson", title="Ακόμα γράφεται", parent_id=m1.id, order=1,
               plane="content", meta={"draft_status": "queued"})
    db.add(l2); db.commit()
    return course


def test_export_docx_structure_and_greek_content():
    db = SessionLocal()
    try:
        course = _mk_tree(db)
        r = client.get(f"/curricula/{course.id}/export.docx")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert "attachment" in r.headers["content-disposition"]

        doc = Document(io.BytesIO(r.content))
        headings = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith("Heading")]
        texts = [p.text for p in doc.paragraphs]

        assert ("Heading 1", "Βασικές αρχές ήχου") in headings
        assert any(s == "Heading 2" and "Τι είναι το gain" in t for s, t in headings)
        assert ("Heading 3", "Θεωρία") in headings
        assert ("Heading 3", "Ασκήσεις") in headings
        assert "Το gain καθορίζει την προενίσχυση του σήματος." in texts
        # heading ORDER: module before its lesson before its sections
        h_texts = [t for _, t in headings]
        assert h_texts.index("Βασικές αρχές ήχου") < h_texts.index(next(t for t in h_texts if "Τι είναι το gain" in t))

        # spec §4: NO sources anywhere in the document
        joined = "\n".join(texts)
        assert "Getting Great Guitar Sounds" not in joined
        assert "Πηγές" not in joined

        # undrafted lesson: present with a placeholder, not silently missing
        assert any("Ακόμα γράφεται" in t for t in h_texts)
    finally:
        db.close()


def test_export_docx_404_for_non_roots():
    db = SessionLocal()
    try:
        course = _mk_tree(db)
        module_id = next(b.id for b in course.children)
        assert client.get(f"/curricula/{module_id}/export.docx").status_code == 404
    finally:
        db.close()
```

(If `course.children` isn't an eager relationship, query `Block.parent_id == course.id` instead — check `models/block.py` for the relationship name.)

- [ ] **Step 3: Run to verify failure**

Run: `cd apps/api && python -m pytest tests/test_export_docx.py -q`
Expected: FAIL — 404 (route missing).

- [ ] **Step 4: Implement** — `apps/api/app/curriculum/export_docx.py`:

```python
"""Clean DOCX export of a curriculum (spec 2026-07-20 §4).

Title page, then Heading 1 per module / Heading 2 per lesson / Heading 3 per
blueprint section with the section prose. Deliberately NO citation or source
data anywhere — the tutor asked for a clean teaching document; sources live in
the app's per-lesson Πηγές modal only. Built-in Word styles only ("Heading 1"
... "Title"), which is what keeps the file opening cleanly in both Word and
Apple Pages."""
from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.block import Block

# Rendered when a lesson has no drafted sections yet ("queued"/"drafting"/
# "failed"): the exported document must mirror the curriculum's real shape —
# a silently missing lesson reads as "covered everything" when it didn't.
_UNDRAFTED_NOTE = "— Το μάθημα δεν έχει συνταχθεί ακόμα. —"


def _children(db: Session, parent_id) -> list[Block]:
    return list(db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ))


def filename_for(course: Block) -> str:
    """`<title>.docx`, filesystem-safe."""
    safe = re.sub(r"[^\w\s\-]", "", course.title, flags=re.UNICODE).strip() or "curriculum"
    return f"{safe}.docx"


def build_curriculum_docx(db: Session, course: Block) -> io.BytesIO:
    doc = Document()

    # --- title page -------------------------------------------------------
    title_p = doc.add_paragraph(style="Title")
    title_p.add_run(course.title)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    modules = _children(db, course.id)
    lesson_count = sum(len([b for b in _children(db, m.id) if b.kind == "lesson"]) for m in modules)
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    level = (course.target_profile or {}).get("level")
    parts = [p for p in (
        level,
        f"{len(modules)} ενότητες",
        f"{lesson_count} μαθήματα",
    ) if p]
    subtitle.add_run(" · ".join(parts))
    if course.body:
        doc.add_paragraph(course.body)

    # --- modules → lessons → sections ------------------------------------
    for module in modules:
        doc.add_page_break()
        doc.add_heading(module.title, level=1)
        if module.body:
            doc.add_paragraph(module.body)
        for lesson in _children(db, module.id):
            if lesson.kind != "lesson":
                continue
            heading = lesson.title
            if lesson.est_minutes:
                heading = f"{heading} ({lesson.est_minutes}′)"
            doc.add_heading(heading, level=2)
            if lesson.body:
                doc.add_paragraph(lesson.body)
            segments = [b for b in _children(db, lesson.id) if b.kind == "segment"]
            if not segments:
                doc.add_paragraph(_UNDRAFTED_NOTE)
                continue
            for segment in segments:
                doc.add_heading(segment.title, level=3)
                for chunk in (segment.body or "").split("\n\n"):
                    if chunk.strip():
                        doc.add_paragraph(chunk.strip())

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
```

Route in `apps/api/app/routers/curriculum.py` (uses Task 4's `_get_course_root_or_404`; imports follow the file's style — `from urllib.parse import quote`, `from fastapi.responses import StreamingResponse` may already be imported for other routes, check):

```python
@router.get("/curricula/{root_id}/export.docx")
def export_curriculum_docx(root_id: UUID, db: Session = Depends(get_db)) -> StreamingResponse:
    course = _get_course_root_or_404(db, root_id)
    buf = build_curriculum_docx(db, course)
    fname = filename_for(course)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            # ASCII fallback + RFC 5987 UTF-8 name, because the real name is Greek.
            "Content-Disposition":
                f"attachment; filename=\"curriculum.docx\"; filename*=UTF-8''{quote(fname)}",
        },
    )
```

**Route-order caution:** FastAPI matches in registration order and `GET /curricula/{root_id}` (line ~350) uses `root_id: UUID` — `"export.docx"` is not a UUID so there is no capture conflict, but register this route NEXT TO `get_curriculum` anyway and confirm `GET /curricula/{uuid}` still works (`tests/test_curricula_manage.py` and the existing curriculum tests cover it).

- [ ] **Step 5: Run**

Run: `cd apps/api && python -m pytest tests/test_export_docx.py tests/test_curricula_manage.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/pyproject.toml apps/api/app/curriculum/export_docx.py apps/api/app/routers/curriculum.py apps/api/tests/test_export_docx.py
git commit -m "feat(export): clean DOCX export — title page, module/lesson/section headings, no sources"
```

---

### Task 7: DOCX download button (frontend)

**Files:**
- Modify: `apps/web/src/lib/api.ts` (download helper)
- Modify: `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx` (header button)
- Modify: `apps/web/src/messages/el.json`, `en.json`

**Interfaces:**
- Consumes: Task 6's route; the `API_BASE`/fetch conventions in `api.ts` (find how `request` builds URLs and credentials — the download must send the same session cookie, so `credentials` must match).
- Produces: `downloadCurriculumDocx(rootId: string): Promise<void>` — fetches the blob and triggers a browser download.

- [ ] **Step 1: api.ts helper** (adapt the URL prefix + credentials to exactly what `request` uses — read that helper first):

```ts
/** Fetches the clean DOCX export and hands it to the browser as a download.
 * A plain <a href> would drop the auth cookie behavior `request` guarantees,
 * so this goes through fetch + object URL like any other authenticated call. */
export async function downloadCurriculumDocx(rootId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/curricula/${rootId}/export.docx`, {
    credentials: "include",
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  const blob = await res.blob();
  const disposition = res.headers.get("content-disposition") ?? "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/);
  const filename = utf8Match ? decodeURIComponent(utf8Match[1]) : "curriculum.docx";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
```

(`ApiError`'s constructor signature: check its class in `api.ts` and match.)

- [ ] **Step 2: Button** — in `curricula/[rootId]/page.tsx`'s header, next to the Task 5 actions menu:

```tsx
<Button
  type="button" size="sm" variant="outline"
  data-testid="curriculum-export-docx"
  disabled={exporting}
  onClick={async () => {
    setExporting(true);
    try { await downloadCurriculumDocx(rootId); }
    catch { setExportError(t("exportError")); }
    finally { setExporting(false); }
  }}
>
  {exporting ? <Loader2 className="animate-spin" /> : <FileDown />}
  {t("exportDocx")}
</Button>
```

with `const [exporting, setExporting] = useState(false)` / `const [exportError, setExportError] = useState<string | null>(null)` and an inline `{exportError && <p role="alert" className="text-sm text-destructive">{exportError}</p>}` — wired into the page's existing state/translation namespace conventions (read the page; `t` here means the page's `curricula`-scope translator).

- [ ] **Step 3: i18n** — under `curricula` in `el.json`: `"exportDocx": "Λήψη DOCX"`, `"exportError": "Η εξαγωγή απέτυχε — δοκίμασε ξανά."`; in `en.json`: `"exportDocx": "Download DOCX"`, `"exportError": "Export failed — try again."`.

- [ ] **Step 4: Verify + commit**

Run: `cd apps/web && npx tsc --noEmit && npm run build` — clean. Manually: open a curriculum, click Λήψη DOCX, open the file in LibreOffice/Word if available.

```bash
git add apps/web/src/lib/api.ts "apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx" apps/web/src/messages/el.json apps/web/src/messages/en.json
git commit -m "feat(export): Λήψη DOCX button on the curriculum detail page"
```

---

### Task 8: Full verification pass

- [ ] **Step 1: Backend suite** — `cd apps/api && python -m pytest tests/ -q` — ALL green (was 1,073+; must not shrink except tests deliberately rewritten in Tasks 2–3, each named in its commit message).
- [ ] **Step 2: Frontend** — `cd apps/web && npx tsc --noEmit && npm run build && npx playwright test` (Playwright only if the stack is running; otherwise record that it was skipped and why).
- [ ] **Step 3: The original repro, end-to-end** — with the dev stack up: open a generated curriculum → Revise with AI → send «μπορείς να αφαιρέσεις όλα τα inline citations από αυτό το curricula;» → the answer must be a REAL model reply (in Greek, and — pleasingly — it should now explain that citations aren't in the text, they're app chrome, which §2/§3 of this very plan removed). Verify the lesson Πηγές modal, a rename, a delete-with-confirm, and a DOCX download.
- [ ] **Step 4: Commit anything outstanding; do NOT deploy** — deployment is a separate decision (there may be in-flight draft jobs; the 2026-07-19 restart-during-draft lesson applies). Report readiness to Chris instead.
