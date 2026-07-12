# Copilot Rebuild — Implementation Plan (Plan 11)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the chat trustworthy: it retrieves from his library *by force*, cites the page it used, never free-types a tab, renders markdown, and streams.

**Architecture:** Move anti-hallucination pressure out of the prompt and into the code. A forced-retrieval pre-hop in the ReAct loop; provenance carried on the message and rendered as a chip that opens the Reader at the cited scan; a post-turn guard that catches free-typed tablature; markdown + SSE in the UI. The HITL gate is untouched.

**Tech Stack:** FastAPI (SSE) · SQLAlchemy · Next.js 16 · react-markdown · Playwright · pytest

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-07-13-copilot-rebuild-design.md`. C1–C6 binding.
- **C6 — DO NOT WEAKEN THE HITL GATE.** Every `kind="mutation"` tool must still suspend for approval. It has been reviewed as airtight twice. You will be editing `loop.py`; leave the suspend mechanism alone and prove it still works.
- **The SYSTEM_PROMPT stays ONE PARAGRAPH.** Read `app/agent/prompts.py`'s docstring first: a longer, "helpful assistant"-style prompt **empirically suppresses tool-calling on this exact model** — it writes prose instead of calling a tool. This is a measured property of the deployment, not a style opinion. Do not editorialise it.
- **TDD throughout.** Failing test → RED → implement → GREEN → commit.
- **i18n:** every user string via next-intl in BOTH `messages/en.json` and `el.json`.
- **DO NOT WRITE TO the app DB (`guitar`).** It holds the tutor's OCR'd book (77 pages, 194,671 chars), his collection, and the demo lesson. It must stay demo-ready. Tests use `guitar_test`.
- Host-side pytest needs `LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1`.

## Verified Facts (do not re-derive)

- `app/brain/retrieve.py`: `search()` returns hits carrying `text`, `page` (real page number), `page_id`, `source_id`, and a score. The `outerjoin(Page)` is in place and regression-guarded.
- The book is live in the app DB: source "Getting Great Guitar Sounds", 77 pages, the pick-thickness passage is on **`Page.page_no=21`**.
- `GET /media/pages/{page_id}.jpg` serves the scan. The Reader accepts `?page=N`.
- `app/agent/loop.py`: `run_agent_turn(db, messages, *, max_steps=6) -> AgentResult(status, content, messages, pending_tool)`. Suspends on the first `kind="mutation"` tool.
- `app/agent/tools.py`: registry of `ToolEntry(fn, schema, kind, async_job, job_kind)`. `generate_artifact` exists and produces schema-validated, AlphaTab-rendered, **playable** artifacts (Plan 4).
- `app/routers/chat.py`: `POST /chat`, `POST /chat/{id}/messages`, `POST /chat/{id}/approvals/{aid}/resolve`, `GET /chat/{id}`, `GET /chat/{id}/pending`.
- **Known model weakness (measured):** it is unreliable at id-plumbing — Plan 10's live run needed 3 attempts to split the right session because nothing resolves "that lesson" by name. C5 fixes this.

---

### Task 1: Forced retrieval + provenance (C1, C2)

**Files:** modify `app/agent/loop.py`, `app/agent/prompts.py`, `app/schemas/chat.py`, `app/models/chat.py`; test `apps/api/tests/test_agent_grounding.py` (create)

**Produces:** `run_agent_turn` injects retrieved context before the model's first call on a content-bearing turn; `AgentResult` and the persisted `Message` carry `citations: list[{source_id, source_title, page_no, page_id, snippet}]`.

**Design:** on a user turn, run `search(db, user_text, k=5)` **in code**. If hits clear a relevance floor, inject them as a system/context message ("GROUNDING — from the tutor's library: …") with their source+page ids, and instruct the model to answer from it and cite. If there are no hits, inject an explicit "his library has nothing on this — say so and label your answer as general knowledge" note. **The model never chooses whether to retrieve.**

Persisting citations needs a column on `Message` (JSON, nullable) — that is the one migration in this plan. Name every constraint explicitly; strip the false `op.drop_index('ix_chunk_embedding_hnsw', ...)` that autogenerate always emits.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_agent_grounding.py
def test_retrieval_runs_even_when_the_model_would_not_have_asked(db, monkeypatch):
    """C1: the model must not get the chance to skip retrieval. A fake provider
    that calls NO tools must STILL produce a grounded, cited answer."""
    searched = []
    monkeypatch.setattr("app.agent.loop.search",
                        lambda db_, q, k=5: searched.append(q) or [_hit("pick thickness...", page=21)])
    provider = _FakeProvider(replies=["The book says a heavy pick is darker."])  # calls zero tools
    monkeypatch.setattr("app.agent.loop.get_provider", lambda: provider)

    result = run_agent_turn(db, [{"role": "user", "content": "what does pick thickness do to tone?"}])

    assert searched, "retrieval did not run — the model was allowed to skip it"
    assert result.citations and result.citations[0]["page_no"] == 21

def test_the_retrieved_passage_actually_reaches_the_model(db, monkeypatch):
    """A 'grounded' answer whose grounding never reached the prompt is not grounded."""
    ...  # assert the hit's text appears in the messages sent to the provider

def test_an_empty_library_is_stated_plainly_not_papered_over(db, monkeypatch):
    """C2: no hits -> the model is TOLD to say his material doesn't cover this."""
    monkeypatch.setattr("app.agent.loop.search", lambda *a, **k: [])
    ...  # assert the injected context tells it to say so, and citations == []

def test_hitl_still_suspends_on_a_mutation(db, monkeypatch):
    """C6 regression: forced retrieval must not disturb the approval gate."""
    ...  # a mutation-proposing turn still returns status="awaiting_approval", nothing written
```

- [ ] **Step 2: RED** — `AttributeError: module 'app.agent.loop' has no attribute 'search'`
- [ ] **Step 3:** implement the pre-hop + citations; add `Message.citations` (JSON) + migration.
- [ ] **Step 4:** GREEN + FULL non-integration suite (the HITL tests MUST still pass) + commit.

---

### Task 2: No free-typed tabs (C3) + `find_lesson` (C5)

**Files:** create `app/agent/guards.py`; modify `app/agent/prompts.py`, `app/agent/tools.py`; test `apps/api/tests/test_agent_guards.py` (create)

**This is the exact bug Chris hit.** Asked for a G major scale tab, the model typed ASCII into a code fence — every string reading `0-2-4-5-7-8-10`, which is not a G major scale — instead of calling `generate_artifact`, which produces a schema-validated, **playable** tab via AlphaTab (already built and shipped in Plan 4).

**Produces:**
- `looks_like_tablature(text: str) -> bool` — detects ASCII tab in prose (the `e|--` / `B|--` string-line shape, or a code fence of dash-and-digit rows).
- The loop uses it post-turn: if the assistant free-typed a tab, **do not show it**. Replace the turn with a `generate_artifact` proposal (the real renderer), or, failing that, an honest "let me generate that properly" retry. It must never reach the tutor as prose.
- `SYSTEM_PROMPT`: add an explicit ban on ASCII tablature — *"never write tablature or chord diagrams as text; call generate_artifact"* — inside the existing single paragraph.
- `find_lesson(title_query) -> [{id, title, provenance}]`, `kind="read"` (C5). The model keeps guessing lesson/session uuids and getting them wrong; give it a way to look one up by name.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_agent_guards.py
from app.agent.guards import looks_like_tablature

def test_detects_the_exact_bluff_chris_got():
    bluff = "Here's a G major scale:\n```\ne|-----0-2-4-5-7-8-10-\nB|-----0-2-4-5-7-8-10-\n```"
    assert looks_like_tablature(bluff)

def test_does_not_false_positive_on_ordinary_prose_or_code():
    assert not looks_like_tablature("Use a heavier pick for a darker tone.")
    assert not looks_like_tablature("```python\nx = [0, 2, 4]\n```")

def test_a_free_typed_tab_never_reaches_the_tutor(db, monkeypatch):
    """The model types a tab instead of calling generate_artifact. The guard must
    intercept it — the tutor must NOT be shown a hand-typed (and wrong) tab."""
    ...  # run_agent_turn with a provider that returns the bluff; assert the ASCII
         # never appears in result.content

def test_find_lesson_resolves_by_title(db):
    ...  # a lesson titled "Pick Gauge and Tone" is findable by "pick gauge"
```

- [ ] **Step 2: RED** → **Step 3:** implement → **Step 4:** GREEN + full suite + commit.

---

### Task 3: Markdown + citation chips + streaming (C4)

**Files:** modify `apps/web/src/components/chat/message-list.tsx`, `chat-panel.tsx`, `lib/api.ts`, `messages/{en,el}.json`; add `app/routers/chat.py` SSE endpoint; test `apps/web/tests/chat.spec.ts`

- Render assistant messages as **markdown** (`react-markdown` + `remark-gfm`; sanitize). Chris: *"no markdown viewer"*.
- Render **citation chips** under a grounded answer: *"Getting Great Guitar Sounds · p.21"* → links to `/[locale]/library/{source_id}?page=21`, opening the real scan. **This is the payoff of Plans 9+10 landing in the chat.**
- **Stream** tokens over SSE (`GET /chat/{id}/stream` or a POST that returns `text/event-stream`). Keep the existing REST turn endpoint working — the whole test suite and the approval flow depend on it. The approval card flow must be **unchanged**.

Tests: markdown renders (a `**bold**` becomes `<strong>`, a list becomes `<li>`); a citation chip links to the right page; streaming appends tokens; **the approval card still appears and still gates a mutation** (regression).

- [ ] Steps: failing Playwright tests → RED → build → GREEN (`npx playwright test` — do not break the existing 39) → `tsc --noEmit` → lint → rebuild web → LOOK AT IT → commit.

---

### Task 4: Live acceptance

**Files:** `apps/api/tests/test_copilot_live.py` (`@pytest.mark.integration`); update `progress.md`, `README.md`

**Drive the REAL model against the REAL book. Do not force anything green.**

1. *"what does pick thickness do to my tone?"* → **retrieval runs without being asked**; the answer comes from **his book**; a **citation chip to p.21** opens the real scan.
2. *"give me a G major scale tab"* → calls `generate_artifact` → a **real, playable** tab renders. **No ASCII in a code fence.** (This is the precise failure Chris reported — prove it is gone.)
3. Ask something his library does not cover → it **says so** and labels the answer as general knowledge. (An agent that cannot say "your material doesn't cover this" will invent something.)
4. A mutation still suspends for approval (C6 regression, in the real UI).

Print the real transcript. **Read it.** If the model still bluffs, report it honestly — that is a real finding about this 9B model's limits, not something to hide.

Screenshot to `copilot-e2e.png`. Full regression: backend `-m "not integration"` + `npx playwright test`.

---

## Self-Review

| Spec | Task |
|---|---|
| C1 forced retrieval | T1 |
| C2 provenance rendered + labelled general knowledge | T1 (backend), T3 (chips), T4 (live) |
| C3 no free-typed tabs | T2 (guard), T4 (live proof) |
| C4 markdown + streaming | T3 |
| C5 find_lesson | T2 |
| C6 HITL untouched | T1 (regression test), T3 (approval card), T4 (live) |

**Risk to watch:** forcing retrieval on EVERY turn would be wrong — "hello" should not trigger a library search, and a follow-up like "split session 2" is not a content question. T1 must decide what counts as a content-bearing turn and test both sides. Getting this wrong makes the agent slow and stupid; skipping it makes it a liar.
