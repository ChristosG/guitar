# Lesson-scoped AI, hand-edit propagation, guided add-lesson — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the revise chat survive a Greek 25-lesson course, record the tutor's hand edits, give every lesson row an AI panel that propagates those edits section by section with preview→approve, and turn «Προσθήκη μαθήματος» into a brief-driven generator.

**Architecture:** Four units. Unit 0 repairs the chat's tool-result path and moves drawer turns onto a `chat_turn` job. Unit 1 adds `meta.tutor_edited` on segment saves and teaches `persist_lesson`/`build_lesson_schema` to write *around* fixed sections. Unit 2 builds the `lesson_ai` job (plan/apply) + the side panel + plan card on that foundation. Unit 3 adds `lesson_generate` (plan title/objective from a brief → `_add_lesson` → chained single-lesson draft) and two new draft prompt blocks. Unit 4 deploys to the webapp, verifies live, tags `desktop-v0.5.0`.

**Tech Stack:** FastAPI + SQLAlchemy (plain `sa.JSON` meta → whole-dict reassignment), `GenerationJob` + `BackgroundTasks` (no worker), guided-JSON via `get_provider().guided_json(messages, schema, role=...)`, Next.js 15 + next-intl (`apps/web/src/messages/{el,en}.json`), Base UI dialog, Playwright (port 3100, `workers: 1`), pytest against `guitar_test` on 127.0.0.1:5434 (NEVER run two pytest processes at once).

**Spec:** `docs/superpowers/specs/2026-09-12-lesson-ai-and-guided-add-lesson-design.md`

## Global Constraints

- `Block.meta` and `GenerationJob.progress` are plain `sa.JSON` with no `MutableDict`: **always reassign the whole dict** (`block.meta = {**(block.meta or {}), ...}`), never mutate a key in place.
- Every new content-writing prompt renders `curriculum_style(language, source)` + `language_directive(language, source)` + `answer_in(language, source)` (from `app.i18n`), and body text passes through `app.curriculum.sanitize.strip_inline_citations` before it lands on `Block.body`.
- Every new `guided_json`/`chat` call site outside `app/llm/` must be registered in `app/prompts/registry.py` (`PromptEntry` with `call_sites=("path/file.py:LINE",)`) or `tests/test_prompts_registry.py` fails. When lines shift in a file that already has pinned `call_sites`, re-pin them.
- `tests/test_prompts_byte_identity.py` pins every prompt render. A task that intentionally changes prompt bytes ends with: diff, then `cd apps/api && python -m tests.prompt_baseline`, then commit the fixture with the change. Never regenerate blindly.
- Enqueue pattern (verbatim): create `GenerationJob(kind=..., status="pending", params=...)`, `db.add`, `db.commit()`, `db.refresh(job)`, THEN `background_tasks.add_task(runner, job.id)`, return `JobAccepted(job_id=job.id, status=job.status)` with `status_code=202`. Runners are imported at MODULE level in the router (tests monkeypatch them).
- Greek is the product: default locale `el`; every new UI string in BOTH `apps/web/src/messages/el.json` and `en.json`. Never show JSON, stack traces, status codes or English to the tutor.
- `apps/api` tests: `cd apps/api && pytest tests/<file> -q` — serialized, one process at a time. Web: `cd apps/web && npx playwright test tests/<file>.spec.ts` (port 3100 must be free) and `npx tsc --noEmit` / `npm run lint` before committing web code.
- Commit after every task with the trailer:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01HagMpuSFq2TABVdQG3WTxH
  ```

## File map

| File | Responsibility (new = ★) |
|---|---|
| `apps/api/app/agent/loop.py` | `_stringify` non-ASCII; `TOOL_RESULT_MAX_CHARS` cap in `_dispatch_read_call` |
| `apps/api/app/agent/tools.py` | body-free `get_curriculum`; ★ `get_lesson` read tool |
| `apps/api/app/agent/transcript.py` | `window_wire(max_chars=...)` |
| `apps/api/app/llm/errors.py`, `llm/claude.py`, `tools/claude_bridge/bridge.py` | `too_long` kind |
| ★ `apps/api/app/jobs/chat_turn.py` | the drawer's turn as a job |
| `apps/api/app/routers/chat.py` | `?async=1`, 413 for `too_long`, `_run_turn_core` shared with the job |
| `apps/api/app/routers/curriculum.py` | tutor-edit marker on PATCH; ★ `/blocks/{lesson}/ai/plan`, `/ai/apply`, `/blocks/{module}/lessons/generate` |
| ★ `apps/api/app/curriculum/tutor_edit.py` | `mark_tutor_edit`, `clear_tutor_edit`, `recompute_lesson_words` |
| `apps/api/app/curriculum/draft.py` | `LESSON_FIXED_BLOCK`, `LESSON_SECTION_BRIEFS_BLOCK`, `LESSON_NEIGHBOURS_BLOCK`, `LESSON_TUTOR_BRIEF_BLOCK`; `build_lesson_messages(fixed_sections, section_briefs, neighbours, tutor_brief)`; `persist_lesson(keep=...)` in-place writes |
| `apps/api/app/curriculum/blueprint.py` | `build_lesson_schema(bp, exclude=())` |
| `apps/api/app/jobs/curriculum_draft.py` | `lesson_ids` filter; `keep`/fixed for tutor-edited; neighbours in `plan` |
| ★ `apps/api/app/curriculum/lesson_ai.py` | `plan_lesson_change`, `apply_lesson_change`, `LESSON_PLAN_SCHEMA`, prompts |
| ★ `apps/api/app/jobs/lesson_ai.py` | `run_lesson_ai_job` (plan / apply) |
| `apps/api/app/curriculum/extend.py` | ★ `plan_lesson_json`, `LESSON_PLAN_TAIL` |
| ★ `apps/api/app/jobs/lesson_generate.py` | `run_lesson_generate_job` |
| `apps/api/app/jobs/sweep.py` | interrupted apply → `ready` |
| `apps/api/app/prompts/registry.py` | new entries |
| `apps/web/src/lib/api.ts` | new endpoints/types |
| `apps/web/src/components/curriculum/block-card.tsx` | row button, badges, tutor-edited chip |
| ★ `apps/web/src/components/curriculum/lesson-ai-scope.tsx` | context provider (counter pattern) |
| ★ `apps/web/src/components/curriculum/lesson-ai-panel.tsx` | the side panel |
| ★ `apps/web/src/components/curriculum/lesson-plan-card.tsx` | per-section plan card |
| ★ `apps/web/src/components/curriculum/add-lesson-dialog.tsx` | brief dialog |
| `apps/web/src/components/chat/chat-panel.tsx` | async drawer turns; 413 message |
| `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx` | mount providers + panel |

---

# Unit 0 — Revise chat repairs

### Task 0.1: Tool results — no ASCII escaping, hard size cap

**Files:**
- Modify: `apps/api/app/agent/loop.py:544-595` (`_stringify`, `_dispatch_read_call`)
- Test: `apps/api/tests/test_agent_tool_results.py` (new)

**Interfaces:**
- Produces: `loop.TOOL_RESULT_MAX_CHARS: int = 60_000`, `loop.TOOL_RESULT_TRUNCATION_MARKER: str`, `loop._stringify(result) -> str` (now capped + non-ASCII).

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_agent_tool_results.py
"""Tool results reach the model as the tutor wrote them, and never unbounded.

2026-09-11: `get_curriculum` on a 25-lesson Greek course returned 488,708 chars
of prose; `json.dumps(..., ensure_ascii=True)` made that 2,310,878 chars of
`\\uXXXX`, and the next model call was "~1,826,053 tokens (limit 1,000,000)".
"""
from app.agent import loop


def test_stringify_keeps_greek_as_greek():
    out = loop._stringify({"title": "Η θεωρία του ήχου"})
    assert "Η θεωρία του ήχου" in out
    assert "\\u0397" not in out


def test_stringify_caps_at_limit_with_an_honest_marker():
    big = {"body": "α" * (loop.TOOL_RESULT_MAX_CHARS + 5_000)}
    out = loop._stringify(big)
    assert len(out) <= loop.TOOL_RESULT_MAX_CHARS + len(loop.TOOL_RESULT_TRUNCATION_MARKER)
    assert out.endswith(loop.TOOL_RESULT_TRUNCATION_MARKER)


def test_stringify_below_limit_is_untouched():
    out = loop._stringify({"x": "μικρό"})
    assert loop.TOOL_RESULT_TRUNCATION_MARKER not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_agent_tool_results.py -q`
Expected: FAIL — `AttributeError: module 'app.agent.loop' has no attribute 'TOOL_RESULT_MAX_CHARS'`.

- [ ] **Step 3: Implement**

In `apps/api/app/agent/loop.py`, replace `_stringify`:

```python
# One read tool's result, as the model sees it and as the transcript stores it.
# 60K chars ≈ 15-20K tokens of Greek — a whole lesson with room to spare, and
# never a whole course (that is what `get_curriculum`'s body-free shape and
# `get_lesson` are for). The marker names the cut in Greek, because the model is
# writing Greek and must not mistake a cut for "that is all there was".
TOOL_RESULT_MAX_CHARS = 60_000
TOOL_RESULT_TRUNCATION_MARKER = "\n…[το αποτέλεσμα περικόπηκε — ζήτα ένα μικρότερο κομμάτι]"


def _stringify(result) -> str:
    """Every tool `fn` returns a plain JSON-serializable Python object (see
    `tools.py`'s docstring) — turning that into the tool message's `content`
    string is this loop's job, not each tool's. `default=str` covers the
    handful of non-JSON-native types the registry's results carry (`UUID`,
    `datetime`) without every tool needing to stringify its own fields.

    `ensure_ascii=False` IS LOAD-BEARING. The default escapes every Greek letter
    to six ASCII characters (`Η` -> `\\u0397`): 5x the characters and ~10x the
    tokens, which is how one `get_curriculum` call blew a 1M-token window on
    2026-09-11. Capped at `TOOL_RESULT_MAX_CHARS` for the same reason — the
    persisted transcript stores exactly this string, so a cap here bounds every
    later turn too.
    """
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > TOOL_RESULT_MAX_CHARS:
        return text[:TOOL_RESULT_MAX_CHARS] + TOOL_RESULT_TRUNCATION_MARKER
    return text
```

- [ ] **Step 4: Run tests**

Run: `cd apps/api && pytest tests/test_agent_tool_results.py tests/test_agent_loop.py tests/test_agent_tools.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agent/loop.py apps/api/tests/test_agent_tool_results.py
git commit -m "fix(agent): tool results stay Greek and stay bounded — the 1.8M-token turn"
```

### Task 0.2: `get_curriculum` goes body-free; new `get_lesson` read tool

**Files:**
- Modify: `apps/api/app/agent/tools.py:336-359` (`_block_tree`), `:940-960` (registration), plus a new `_get_lesson` fn and registry entry
- Test: `apps/api/tests/test_agent_tool_results.py` (extend)

**Interfaces:**
- Produces: tool `get_lesson(lesson_id: str) -> {id, title, objective, module_title, sections: [{id, section, title, body, tutor_edited: bool}]}`; `get_curriculum` result no longer carries `body` below the course level.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_agent_tool_results.py`:

```python
import uuid
import pytest
from sqlalchemy import text
from app.db import Base, SessionLocal, engine
from app.models.block import Block
from app.agent.tools import TOOLS

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


@pytest.fixture
def course_with_lesson():
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", language="el", is_template=True, meta={})
    db.add(course); db.flush()
    module = Block(kind="module", title="Ξύλα", parent_id=course.id, order=0, language="el",
                   meta={"objective": "τα ξύλα"})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Το μπράτσο", parent_id=module.id, order=0,
                   language="el", meta={"objective": "μπράτσο", "draft_status": "ready"})
    db.add(lesson); db.flush()
    seg = Block(kind="segment", title="Θεωρία", body="Ο σφένδαμος είναι σκληρός. " * 50,
                parent_id=lesson.id, order=0, language="el",
                meta={"section": "theory", "tutor_edited": {"at": "2026-09-11T15:36:59Z",
                                                            "prev_body": "παλιό", "count": 1}})
    db.add(seg); db.commit()
    yield db, course, module, lesson, seg
    db.close()


def test_get_curriculum_carries_no_bodies_below_the_course(course_with_lesson):
    db, course, module, lesson, seg = course_with_lesson
    out = TOOLS["get_curriculum"].fn(db, root_id=str(course.id))
    lesson_node = out["children"][0]["children"][0]
    assert lesson_node["title"] == "Το μπράτσο"
    assert "body" not in lesson_node
    section = lesson_node["children"][0]
    assert section["title"] == "Θεωρία" and "body" not in section
    assert "σφένδαμος" not in str(out)


def test_get_lesson_returns_sections_with_bodies_and_the_tutor_edit_flag(course_with_lesson):
    db, course, module, lesson, seg = course_with_lesson
    out = TOOLS["get_lesson"].fn(db, lesson_id=str(lesson.id))
    assert out["title"] == "Το μπράτσο" and out["module_title"] == "Ξύλα"
    assert out["sections"][0]["section"] == "theory"
    assert "σφένδαμος" in out["sections"][0]["body"]
    assert out["sections"][0]["tutor_edited"] is True


def test_get_lesson_on_a_non_lesson_is_an_error_dict(course_with_lesson):
    db, course, module, lesson, seg = course_with_lesson
    out = TOOLS["get_lesson"].fn(db, lesson_id=str(module.id))
    assert "error" in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_agent_tool_results.py -q`
Expected: FAIL — `KeyError: 'get_lesson'` and the body assertion.

- [ ] **Step 3: Implement**

In `apps/api/app/agent/tools.py` replace `_block_tree` and add `_get_lesson`:

```python
def _block_tree(block: Block) -> dict:
    """Recursive STRUCTURE for `get_curriculum`: ids, kinds, titles, objectives,
    section keys — no bodies below the course. A 25-lesson Greek course is
    ~490K chars of prose; sending it as one tool result is what overflowed the
    model on 2026-09-11. Prose is fetched per lesson with `get_lesson`."""
    meta = block.meta or {}
    node: dict = {"id": block.id, "kind": block.kind, "title": block.title}
    if block.kind in ("course",) and block.body:
        node["body"] = block.body
    if meta.get("objective"):
        node["objective"] = meta["objective"]
    if block.kind == "segment" and meta.get("section"):
        node["section"] = meta["section"]
    if block.kind == "lesson" and meta.get("draft_status"):
        node["draft_status"] = meta["draft_status"]
    node["children"] = [_block_tree(c) for c in sorted(block.children, key=lambda b: b.order)]
    return node


def _get_lesson(db, lesson_id: str) -> dict:
    """ONE lesson with its section bodies — the only tool that returns prose,
    bounded by construction (one lesson, and `loop.TOOL_RESULT_MAX_CHARS`)."""
    try:
        lid = uuid.UUID(str(lesson_id))
    except ValueError:
        return {"error": f"not a lesson id: {lesson_id!r}"}
    lesson = db.get(Block, lid)
    if lesson is None or lesson.kind != "lesson":
        return {"error": f"no lesson with id {lesson_id}"}
    module = db.get(Block, lesson.parent_id) if lesson.parent_id else None
    segments = db.scalars(
        select(Block)
        .where(Block.parent_id == lesson.id, Block.kind == "segment")
        .order_by(Block.order)
    ).all()
    meta = lesson.meta or {}
    return {
        "id": lesson.id,
        "title": lesson.title,
        "objective": meta.get("objective") or lesson.body or "",
        "module_title": module.title if module else None,
        "sections": [
            {
                "id": s.id,
                "section": (s.meta or {}).get("section"),
                "title": s.title,
                "body": s.body or "",
                "tutor_edited": bool((s.meta or {}).get("tutor_edited")),
            }
            for s in segments
        ],
    }
```

Update the `get_curriculum` registration description to:
`"Get the STRUCTURE of one curriculum (course -> modules -> lessons -> section titles) by its root id — ids, titles, objectives, no prose. Use get_lesson for a lesson's text. Call list_curricula first if you don't already have the id."`

Register `get_lesson` right after it:

```python
    "get_lesson": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "get_lesson",
                "description": (
                    "Get ONE lesson's full text, section by section (theory, "
                    "exercises, ...), with a flag on sections the tutor edited by "
                    "hand. Use the lesson id from get_curriculum or find_lesson."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lesson_id": {"type": "string", "description": "the lesson block id (UUID)"},
                    },
                    "required": ["lesson_id"],
                },
            },
        },
        fn=_get_lesson,
        kind="read",
    ),
```

Make sure `select` and `uuid` are imported at the top of `tools.py` (they are used elsewhere in the file; verify with `grep -n "^from sqlalchemy import\|^import uuid" apps/api/app/agent/tools.py`).

- [ ] **Step 4: Run tests**

Run: `cd apps/api && pytest tests/test_agent_tool_results.py tests/test_agent_tools.py tests/test_prompts_registry.py -q`
Expected: PASS. If `test_prompts_registry` complains about `tools.descriptions` render bytes, that entry is auto-generated from `TOOLS` — regenerate the baseline: `python -m tests.prompt_baseline`, then `git diff tests/fixtures/prompt_renders_baseline.json` and confirm ONLY the `tools.descriptions` render changed.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agent/tools.py apps/api/tests/test_agent_tool_results.py apps/api/tests/fixtures/prompt_renders_baseline.json
git commit -m "feat(agent): get_curriculum is structure-only; get_lesson returns one lesson's prose"
```

### Task 0.3: Transcript window gets a character budget

**Files:**
- Modify: `apps/api/app/agent/transcript.py:31-49`
- Test: `apps/api/tests/test_agent_transcript.py` (extend)

**Interfaces:**
- Produces: `window_wire(wire, limit=MAX_WIRE_MESSAGES, max_chars=MAX_WIRE_CHARS)`; `MAX_WIRE_CHARS = 240_000`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_agent_transcript.py`:

```python
from app.agent.transcript import window_wire, MAX_WIRE_CHARS


def _turn(i: int, tool_chars: int = 0) -> list[dict]:
    msgs = [{"role": "user", "content": f"ερώτηση {i}"}]
    if tool_chars:
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "get_lesson", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "α" * tool_chars})
    msgs.append({"role": "assistant", "content": f"απάντηση {i}"})
    return msgs


def test_window_drops_old_turns_when_chars_exceed_budget():
    wire = [{"role": "system", "content": "sys"}]
    for i in range(5):
        wire += _turn(i, tool_chars=100_000)
    out = window_wire(wire, max_chars=250_000)
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"                      # clean left edge
    assert sum(len(m.get("content") or "") for m in out) <= 250_000 + len("sys")
    assert out[-1]["content"] == "απάντηση 4"          # newest kept


def test_window_never_splits_a_tool_call_from_its_result():
    wire = [{"role": "system", "content": "sys"}] + _turn(0, tool_chars=10) + _turn(1, tool_chars=200_000)
    out = window_wire(wire, max_chars=150_000)
    ids_called = {c["id"] for m in out if m.get("tool_calls") for c in m["tool_calls"]}
    ids_answered = {m["tool_call_id"] for m in out if m.get("role") == "tool"}
    assert ids_called == ids_answered


def test_window_keeps_the_last_user_turn_even_if_alone_over_budget():
    wire = [{"role": "user", "content": "α" * 300_000}]
    assert window_wire(wire, max_chars=10) == wire


def test_default_budget_is_240k():
    assert MAX_WIRE_CHARS == 240_000
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_agent_transcript.py -q`
Expected: FAIL — `ImportError: cannot import name 'MAX_WIRE_CHARS'`.

- [ ] **Step 3: Implement**

Replace `window_wire` in `apps/api/app/agent/transcript.py`:

```python
# A CHARACTER budget beside the message cap. 60 messages was the wrong unit:
# one 2.3M-char tool result (2026-09-11) fit the cap and poisoned every later
# turn until it aged out. 240K chars ≈ 60-80K tokens of Greek — comfortably
# inside every provider window with the cached prefix on top.
MAX_WIRE_CHARS = 240_000


def window_wire(
    wire: list[dict], limit: int = MAX_WIRE_MESSAGES, max_chars: int = MAX_WIRE_CHARS,
) -> list[dict]:
    """The transcript's most recent messages, starting at a clean USER turn,
    within BOTH `limit` messages and `max_chars` characters. Starting anywhere
    else can orphan a tool result from the assistant tool_call it answers —
    `anthropic_wire.py` raises `DanglingToolUseError` on exactly that — so the
    window's left edge advances to the next plain user message. A leading
    system message is always kept and never counted. The newest user turn is
    always kept, even alone over budget (better an oversized prompt the
    provider can reject loudly than an empty one). Old turns fall out of the
    model's context; they remain in the DB and the UI untouched."""
    system: list[dict] = []
    body = wire
    if body and body[0].get("role") == "system":
        system, body = [body[0]], body[1:]

    # Left edge by message count (the old rule).
    start = max(0, len(body) - limit)
    # Left edge by characters: walk newest -> oldest accumulating content.
    total = 0
    char_start = len(body)
    for i in range(len(body) - 1, -1, -1):
        total += len(body[i].get("content") or "")
        if total > max_chars:
            break
        char_start = i
    start = max(start, char_start)
    # Snap to a clean user turn (never inside a tool_call/result pair).
    while start < len(body) and body[start].get("role") != "user":
        start += 1
    if start >= len(body):
        # Degenerate tail (no user row after the cut): keep the LAST user turn
        # and everything after it, whatever its size.
        last_user = next((i for i in range(len(body) - 1, -1, -1)
                          if body[i].get("role") == "user"), None)
        start = last_user if last_user is not None else 0
    return system + body[start:]
```

- [ ] **Step 4: Run tests**

Run: `cd apps/api && pytest tests/test_agent_transcript.py tests/test_chat_router.py tests/test_chat_history.py -q`
Expected: PASS (existing tests that call `window_wire(wire)` or `window_wire(wire, limit)` are unaffected — the old behaviour is a subset).

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/agent/transcript.py apps/api/tests/test_agent_transcript.py
git commit -m "fix(chat): the transcript window budgets characters, not just messages"
```

### Task 0.4: `too_long` in the error taxonomy → HTTP 413 with a Greek message

**Files:**
- Modify: `apps/api/app/llm/errors.py` (docstring list), `apps/api/app/llm/claude.py:369-398` (`_mapped_errors`), `tools/claude_bridge/bridge.py:203-245` (`_classify`), `apps/api/app/routers/chat.py:744-751`, `apps/web/src/lib/job-errors.ts`, `apps/web/src/components/chat/chat-panel.tsx:481-484`, `apps/web/src/messages/{el,en}.json`
- Test: `tools/claude_bridge/test_bridge.py` (extend), `apps/api/tests/test_claude_provider.py` (extend), `apps/api/tests/test_chat_router.py` (extend)

**Interfaces:**
- Produces: `LLMError(kind="too_long")`; chat `POST /messages` → 413 `{"detail": {"code": "conversation_too_long", "message": ...}}`; job `error_kind="too_long"`; web key `chat.conversationTooLong`, `jobErrors.too_long`.

- [ ] **Step 1: Failing tests**

`tools/claude_bridge/test_bridge.py` — add:

```python
def test_classify_prompt_too_long_is_its_own_kind():
    from bridge import _classify
    assert _classify(1, "Prompt is too long · the request is ~1826053 tokens (limit 1000000)", None) == "too_long"
```

`apps/api/tests/test_claude_provider.py` — add (find the existing `_mapped_errors` test for `rate_limit` and copy its shape; the SDK error type is `anthropic.BadRequestError`):

```python
def test_bad_request_prompt_too_long_maps_to_too_long(monkeypatch):
    import anthropic, httpx
    from app.llm.claude import ClaudeProvider
    from app.llm.errors import LLMError
    p = ClaudeProvider(api_key="sk-ant-test", model="claude-sonnet-5")
    resp = httpx.Response(400, request=httpx.Request("POST", "https://x"),
                          json={"error": {"message": "prompt is too long: 1200000 tokens > 1000000 maximum"}})
    err = anthropic.BadRequestError("prompt is too long: 1200000 tokens > 1000000 maximum", response=resp, body=None)
    with pytest.raises(LLMError) as ei:
        with p._mapped_errors():
            raise err
    assert ei.value.kind == "too_long"
```

`apps/api/tests/test_chat_router.py` — add:

```python
def test_too_long_turn_is_a_413_with_a_code(monkeypatch):
    from app.llm.errors import LLMError
    import app.routers.chat as chat_router
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]

    def boom(*a, **k):
        raise LLMError("too_long", "prompt is too long")
    monkeypatch.setattr(chat_router, "run_agent_turn", boom)
    r = client.post(f"/chat/{session}/messages", json={"content": "γεια"})
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "conversation_too_long"
```

- [ ] **Step 2: Run to verify failures**

Run: `cd tools/claude_bridge && python -m pytest test_bridge.py -q -k too_long; cd ../../apps/api && pytest tests/test_claude_provider.py tests/test_chat_router.py -q -k too_long`
Expected: 3 FAIL.

- [ ] **Step 3: Implement**

`bridge.py::_classify` — before the `rate limit` check inside the `exit_code != 0` branch:

```python
        # A context overflow is neither transient nor the tutor's key: the
        # conversation itself is too big. Its own kind, so the app can tell him
        # to start a new chat instead of "the model failed".
        if any(s in blob for s in ("prompt is too long", "context length",
                                   "too many tokens", "exceeds the model")):
            return "too_long"
```

`apps/api/app/llm/errors.py` — add to the kind list in the docstring:
`"too_long"   -> the conversation/prompt exceeds the model window. 413. The fix is a new chat, or a smaller ask.`

`apps/api/app/llm/claude.py::_mapped_errors` — add a branch before the generic `BadRequestError`/`APIStatusError` mapping:

```python
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "too long" in msg or "too many tokens" in msg or "context" in msg and "exceed" in msg:
                raise LLMError("too_long", str(e)) from e
            raise
```
(Insert it as the FIRST `except` clause of the context manager so it takes precedence; keep the existing clauses unchanged after it.)

`apps/api/app/routers/chat.py::post_message` — replace the status map:

```python
        if e.kind == "too_long":
            raise HTTPException(
                status_code=413,
                detail={"code": "conversation_too_long",
                        "message": "the conversation is too large for the model — start a new chat"},
            ) from e
        status = {"rate_limit": 429, "auth": 409, "timeout": 504}.get(e.kind, 502)
```

`apps/web/src/lib/job-errors.ts` — `LOCALIZED_KINDS = new Set(["auth", "rate_limit", "timeout", "too_long"])` and widen the `t` key union to include `"too_long"`.

`apps/web/src/messages/el.json` — under `jobErrors`: `"too_long": "Η συζήτηση ή το κείμενο είναι πολύ μεγάλο για το μοντέλο. Ξεκίνα νέα συζήτηση ή ζήτα κάτι μικρότερο."`; under `chat`: `"conversationTooLong": "Η συζήτηση μεγάλωσε πολύ για το μοντέλο. Πάτησε «Εκκαθάριση συνομιλίας» για να ξεκινήσεις νέα."`. `en.json`: `"too_long": "The conversation or text is too large for the model. Start a new chat or ask for something smaller."`, `"conversationTooLong": "This conversation grew too large for the model. Use “Clear chat” to start a new one."`.

`chat-panel.tsx` in the `catch (err)` of `sendContent`: before `setComposerError(err.detail || t("error"))` add
```ts
      if (err instanceof ApiError && err.code === "conversation_too_long") {
        setComposerError(t("conversationTooLong"));
        return;
      }
```
(`ApiError.code` is populated by `parseError` from `{"detail": {"code", "message"}}`.) Note the `return` skips the slow-turn poll.

- [ ] **Step 4: Run tests**

Run the three test files as in Step 2, then `cd apps/web && npx tsc --noEmit`.
Expected: PASS, tsc clean.

- [ ] **Step 5: Commit**

```bash
git add tools/claude_bridge/bridge.py tools/claude_bridge/test_bridge.py apps/api/app/llm apps/api/app/routers/chat.py apps/api/tests apps/web/src/lib/job-errors.ts apps/web/src/components/chat/chat-panel.tsx apps/web/src/messages
git commit -m "fix(llm): a context overflow is 'too_long', not 'upstream' — and the tutor is told to start a new chat"
```

### Task 0.5: The drawer's turn as a `chat_turn` job

**Files:**
- Create: `apps/api/app/jobs/chat_turn.py`
- Modify: `apps/api/app/routers/chat.py` (extract `_run_turn_core`; add `?async=1`; 409 dedupe), `apps/api/app/routers/jobs.py` (no change expected), `apps/api/app/schemas/chat.py` (`ChatTurnOut` docstring: add `"job_pending"` on `POST /messages` too)
- Test: `apps/api/tests/test_chat_turn_job.py` (new)

**Interfaces:**
- Consumes: `run_agent_turn(db, wire, locale=..., raw_user_text=..., precomputed_first=None) -> AgentResult` (`app/agent/loop.py`); `_respond_to_turn(db, session_id, prior_wire, result) -> ChatTurnOut`; `_inject_curriculum_context`, `_inject_interview_context`, `window_wire`, `messages_to_wire`, `_ordered_messages`, `_open_pending_approval` — all already in `routers/chat.py`.
- Produces: `chat.run_turn_core(db, session, content) -> ChatTurnOut` (module-level, importable by the job); job kind `"chat_turn"`, params `{session_id, content}`, progress `{"phase": "done", "turn": <ChatTurnOut.model_dump(mode="json")>}`; `POST /chat/{id}/messages?async=1` → `202 JobAccepted`; `chat.has_running_turn(db, session_id) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_chat_turn_job.py
"""The revise drawer's turns run as a job — nothing in front of a job can cut it.

Live evidence (2026-09-11): the planner ran inside the HTTP request, behind
nginx (60/300s) and Cloudflare (~100s); a 6-minute claude -p turn never made it
back to the browser. `jobs/curriculum_revise.py` already had a plan-mode job the
chat never used. This makes the whole turn the job.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent.loop import AgentResult
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.generation_job import GenerationJob
from app.models.chat import ApprovalRequest, Message
import app.routers.chat as chat_router
import app.jobs.chat_turn as chat_turn_job

client = TestClient(app)

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _answer(content: str):
    def fake(db, wire, **kw):
        return AgentResult(status="answer", content=content,
                           messages=[*wire, {"role": "assistant", "content": content}],
                           citations=[])
    return fake


def test_async_flag_returns_202_and_persists_the_user_row(monkeypatch):
    monkeypatch.setattr(chat_router, "run_agent_turn", _answer("ok"))
    # Do not let TestClient run the background task here — we want the 202 shape alone.
    monkeypatch.setattr(chat_router, "run_chat_turn_job", lambda job_id: None)
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "γεια"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending" and body["job_id"]
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, uuid.UUID(body["job_id"]))
        assert job.kind == "chat_turn" and job.params["session_id"] == session
        rows = db.query(Message).filter_by(session_id=uuid.UUID(session)).all()
        assert [m.role for m in rows] == ["user"]
    finally:
        db.close()


def test_job_runs_the_turn_and_records_the_answer(monkeypatch):
    monkeypatch.setattr(chat_router, "run_agent_turn", _answer("η απάντηση"))
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "γεια"})
    job_id = r.json()["job_id"]          # TestClient ran the background task after the response
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert job["progress"]["turn"]["status"] == "answer"
    history = client.get(f"/chat/{session}").json()
    assert history[-1]["role"] == "assistant" and history[-1]["content"] == "η απάντηση"


def test_job_turn_can_open_an_approval(monkeypatch):
    def suspend(db, wire, **kw):
        return AgentResult(
            status="awaiting_approval", content="Πρόταση:",
            messages=[*wire, {"role": "assistant", "content": "Πρόταση:", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "generate_curriculum", "arguments": "{}"}}]}],
            pending_tool={"tool_call_id": "c1", "name": "generate_curriculum", "arguments": {}},
            citations=[],
        )
    monkeypatch.setattr(chat_router, "run_agent_turn", suspend)
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "φτιάξε"})
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["progress"]["turn"]["status"] == "awaiting_approval"
    pending = client.get(f"/chat/{session}/pending").json()
    assert pending and pending["tool_name"] == "generate_curriculum"


def test_second_async_turn_while_one_runs_is_409(monkeypatch):
    monkeypatch.setattr(chat_router, "run_chat_turn_job", lambda job_id: None)  # stays pending
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    assert client.post(f"/chat/{session}/messages?async=1", json={"content": "α"}).status_code == 202
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "β"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "turn_running"
    # the sync endpoint refuses too
    r2 = client.post(f"/chat/{session}/messages", json={"content": "γ"})
    assert r2.status_code == 409


def test_llm_error_in_the_job_is_recorded_with_its_kind(monkeypatch):
    from app.llm.errors import LLMError
    def boom(db, wire, **kw):
        raise LLMError("too_long", "prompt is too long")
    monkeypatch.setattr(chat_router, "run_agent_turn", boom)
    session = client.post("/chat", json={"locale": "el"}).json()["session_id"]
    r = client.post(f"/chat/{session}/messages?async=1", json={"content": "γεια"})
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and job["error_kind"] == "too_long"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_chat_turn_job.py -q`
Expected: FAIL — `ModuleNotFoundError: app.jobs.chat_turn`.

- [ ] **Step 3: Implement — extract the core in `routers/chat.py`**

Move the body of `post_message` after the 409 guard into a module-level function the job can call (keep `post_message` as the sync door):

```python
def has_running_turn(db: Session, session_id: UUID) -> bool:
    """A `chat_turn` job for this session that has not finished. Same reason
    `_open_pending_approval` guards a new turn: two turns interleaving on one
    transcript is an out-of-protocol shape that gets PERSISTED."""
    return db.scalar(
        select(GenerationJob.id).where(
            GenerationJob.kind == "chat_turn",
            GenerationJob.status.in_(("pending", "running")),
            GenerationJob.params["session_id"].as_string() == str(session_id),
        ).limit(1)
    ) is not None


def run_turn_core(db: Session, session: ChatSession, content: str, *, precomputed=None) -> ChatTurnOut:
    """Everything a turn does AFTER its user row is persisted: window, inject,
    run the loop, persist the tail, open an approval if the loop suspended.
    Shared verbatim by the synchronous `post_message` and the `chat_turn` job
    (`app/jobs/chat_turn.py`) — the job is the same turn off the request path,
    so this is the one place a turn is defined. Raises `LLMError` through."""
    wire = window_wire(messages_to_wire(_ordered_messages(db, session.id)))
    wire = _inject_curriculum_context(db, session, wire)
    wire = _inject_interview_context(db, session, wire)
    result = run_agent_turn(
        db, wire, locale=session.locale, raw_user_text=content,
        precomputed_first=precomputed,
    )
    return _respond_to_turn(db, session.id, wire, result)
```

Rewrite `post_message` (keep its docstring/comments about the 409):

```python
@router.post("/chat/{session_id}/messages", response_model=ChatTurnOut | JobAccepted)
def post_message(
    session_id: UUID, payload: ChatMessageIn, background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    async_: bool = Query(False, alias="async"),
):
    session = _get_session_or_404(db, session_id)
    if _open_pending_approval(db, session_id) is not None:
        FIRST_TURN_HANDOFF.discard(session_id)
        raise HTTPException(status_code=409,
                            detail="an approval is pending — resolve it before sending a new message")
    if has_running_turn(db, session_id):
        raise HTTPException(status_code=409,
                            detail={"code": "turn_running",
                                    "message": "the previous message is still being answered"})

    _ensure_title(session, payload.content)
    persist_new_messages(db, session_id, [{"role": "user", "content": payload.content}])

    if async_:
        # THE DRAWER'S DOOR. A revise turn runs the planner inline and can take
        # minutes under claude -p; every proxy in front of this process cuts a
        # request long before that. Off the request path, nothing can cut it.
        FIRST_TURN_HANDOFF.discard(session_id)
        job = GenerationJob(kind="chat_turn", status="pending",
                            params={"session_id": str(session_id), "content": payload.content})
        db.add(job)
        db.commit()
        db.refresh(job)
        background_tasks.add_task(run_chat_turn_job, job.id)
        return JSONResponse(status_code=202,
                            content=JobAccepted(job_id=job.id, status=job.status).model_dump(mode="json"))

    precomputed = FIRST_TURN_HANDOFF.claim(session_id, payload.content)
    try:
        return run_turn_core(db, session, payload.content, precomputed=precomputed)
    except LLMError as e:
        if e.kind == "too_long":
            raise HTTPException(status_code=413, detail={"code": "conversation_too_long",
                "message": "the conversation is too large for the model — start a new chat"}) from e
        status = {"rate_limit": 429, "auth": 409, "timeout": 504}.get(e.kind, 502)
        raise HTTPException(status_code=status,
                            detail=str(e) or f"the model provider failed ({e.kind}) — try again") from e
```

Add the imports: `from fastapi import Query, BackgroundTasks`, `from fastapi.responses import JSONResponse`, `from app.schemas.jobs import JobAccepted`, `from app.models.generation_job import GenerationJob`, `from app.jobs.chat_turn import run_chat_turn_job` (module level — the tests monkeypatch `chat_router.run_chat_turn_job`), `from sqlalchemy import select` if missing. Because `response_model` is now a union, FastAPI serialises whichever you return; returning a `JSONResponse` for the 202 keeps the status code exact.

- [ ] **Step 4: Implement the job**

```python
# apps/api/app/jobs/chat_turn.py
"""One chat turn, as a `GenerationJob`.

The revise drawer's turn calls `propose_curriculum_revision` INLINE (a
`role="plan"` guided-JSON call over the course, 20s-6min depending on the
provider). On the webapp that request sits behind nginx and Cloudflare, which
cut it long before the model finishes — the answer was persisted, billed, and
never seen (2026-07-21, 2026-09-11). Here the SAME turn (`routers/chat.py::
run_turn_core`) runs off the request path; the browser polls the job and then
re-hydrates history + pending approval exactly as a page reload would.

The turn's outcome is stored on `progress["turn"]` (the `ChatTurnOut` dict) so a
poller can branch on it without a second history read.
"""
from __future__ import annotations

import logging
import uuid

from app.db import SessionLocal
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.chat import ChatSession
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def run_chat_turn_job(job_id: uuid.UUID) -> None:
    # Imported here, not at module level: `routers/chat.py` imports THIS module
    # at import time (for `background_tasks.add_task`), so a top-level import
    # back into the router would be circular.
    from app.routers.chat import run_turn_core

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_chat_turn_job: job %s not found", job_id)
            return
        job.status = "running"
        job.progress = {"phase": "answering"}
        db.commit()

        session = db.get(ChatSession, uuid.UUID(str(job.params["session_id"])))
        if session is None:
            _fail(db, job_id, "internal", "That conversation no longer exists.")
            return
        turn = run_turn_core(db, session, str(job.params.get("content") or ""))
        job = db.get(GenerationJob, job_id)
        job.status = "succeeded"
        job.progress = {"phase": "done", "turn": turn.model_dump(mode="json")}
        db.commit()
    except LLMNotConfigured:
        _fail(db, job_id, "auth", "No API key is configured. Open Settings, add your key, then try again.")
    except LLMError as e:
        _fail(db, job_id, e.kind, str(e) or e.kind)
    except Exception:
        log.exception("run_chat_turn_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The answer could not be written. Try again.")
    finally:
        db.close()


def _fail(db, job_id: uuid.UUID, kind: str, message: str) -> None:
    try:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_kind = kind
        job.error = message
        db.commit()
    except Exception:
        log.warning("could not record failure for job %s", job_id, exc_info=True)
```

`error_kind` is `String(20)`; `too_long` fits. Check `GenerationJob.error_kind` has no `Enum` constraint (`grep -n error_kind apps/api/app/models/generation_job.py`).

- [ ] **Step 5: Run tests**

Run: `cd apps/api && pytest tests/test_chat_turn_job.py tests/test_chat_router.py tests/test_chat_stream_router.py tests/test_agent_hitl.py tests/test_revise_chat.py -q`
Expected: PASS. `test_chat_router.py` tests that used `chat_router.run_agent_turn` keep working because `run_turn_core` resolves it through the module's globals.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/jobs/chat_turn.py apps/api/app/routers/chat.py apps/api/app/schemas/chat.py apps/api/tests/test_chat_turn_job.py
git commit -m "feat(chat): a turn can run as a job — the drawer's planner is no longer cut by the edge"
```

### Task 0.6: The drawer sends async and hydrates when the job lands

**Files:**
- Modify: `apps/web/src/lib/api.ts` (`sendChatMessageAsync`), `apps/web/src/components/chat/chat-panel.tsx` (`sendContent`, `pollJob`), `apps/web/src/messages/{el,en}.json`
- Test: `apps/web/tests/revise-async.spec.ts` (new)

**Interfaces:**
- Consumes: `POST /chat/{id}/messages?async=1` → `JobAccepted`; `GET /jobs/{id}` → `JobOut` with `progress.turn`.
- Produces: `sendChatMessageAsync(sessionId, content): Promise<JobAccepted>`; panel behaviour: when `rootId` is set, every turn goes async and shows `t("planning")` until the job finishes, then `hydrate()`.

- [ ] **Step 1: Write the failing Playwright test**

```ts
// apps/web/tests/revise-async.spec.ts
import { test, expect, type Route } from "@playwright/test";

const API_ORIGIN = "http://localhost:8791";
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "http://localhost:3100",
  "Access-Control-Allow-Credentials": "true",
  "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "content-type,x-app-locale",
};
const ROOT = "11111111-1111-4111-8111-111111111111";
const SESSION = "22222222-2222-4222-8222-222222222222";
const JOB = "33333333-3333-4333-8333-333333333333";

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, headers: { ...CORS_HEADERS, "content-type": "application/json" }, body: JSON.stringify(body) });
}

const tree = {
  id: ROOT, kind: "course", title: "Ήχος", body: null, est_minutes: null, order: 0, language: "el",
  plane: "content", student_id: null, meta: {}, children: [],
};

test("a drawer turn goes async, polls the job, then shows the persisted answer", async ({ page }) => {
  let polls = 0;
  const history: unknown[] = [];
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS_HEADERS });
    if (url.pathname === "/auth/me") return json(route, 200, { authenticated: true });
    if (url.pathname === "/settings") return json(route, 200, { model: "claude-sonnet-5", configured: true, key_hint: "abcd" });
    if (url.pathname === "/curricula/interview/open") return json(route, 200, null);
    if (url.pathname === `/curricula/${ROOT}`) return json(route, 200, tree);
    if (url.pathname === `/curricula/${ROOT}/progress`) return json(route, 200, { total: 0, ready: 0, drafting: 0, queued: 0, failed: 0, done: true });
    if (url.pathname === `/curricula/${ROOT}/chat-session`) return json(route, 200, { session_id: SESSION });
    if (url.pathname === `/chat/${SESSION}/pending`) return json(route, 200, null);
    if (url.pathname === `/chat/${SESSION}` && req.method() === "GET") return json(route, 200, history);
    if (url.pathname === "/chat" && req.method() === "GET") return json(route, 200, []);
    if (url.pathname === `/chat/${SESSION}/messages` && req.method() === "POST") {
      expect(url.searchParams.get("async")).toBe("1");
      history.push({ id: "m1", role: "user", content: "άλλαξε το", created_at: new Date().toISOString(), citations: null });
      return json(route, 202, { job_id: JOB, status: "pending" });
    }
    if (url.pathname === `/jobs/${JOB}`) {
      polls++;
      if (polls < 2) return json(route, 200, { id: JOB, kind: "chat_turn", status: "running", params: {}, result_root_id: null, error: null, error_kind: null, progress: { phase: "answering" }, created_at: "", updated_at: "" });
      history.push({ id: "m2", role: "assistant", content: "Έγινε η αλλαγή.", created_at: new Date().toISOString(), citations: null });
      return json(route, 200, { id: JOB, kind: "chat_turn", status: "succeeded", params: {}, result_root_id: null, error: null, error_kind: null, progress: { phase: "done", turn: { status: "answer", content: "Έγινε η αλλαγή." } }, created_at: "", updated_at: "" });
    }
    if (url.pathname === `/chat/${SESSION}/suggestions`) return json(route, 200, { suggestions: [] });
    return route.fulfill({ status: 500, headers: CORS_HEADERS, body: `unexpected ${req.method()} ${url.pathname}` });
  });

  await page.goto(`/el/curricula/${ROOT}`);
  await page.getByRole("button", { name: "Αναθεώρηση με AI" }).click();
  const composer = page.getByTestId("chat-composer-input");
  await composer.fill("άλλαξε το");
  await page.getByTestId("chat-send").click();
  await expect(page.getByText("Έγινε η αλλαγή.")).toBeVisible({ timeout: 15_000 });
  expect(polls).toBeGreaterThanOrEqual(2);
});
```

Check the composer/send testids in `chat-panel.tsx` (`grep -n 'data-testid="chat-' apps/web/src/components/chat/chat-panel.tsx`) and use the real ones.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/web && npx playwright test tests/revise-async.spec.ts`
Expected: FAIL — the POST is made without `?async=1` (the `expect` inside the route throws → 500 → composer error).

- [ ] **Step 3: Implement**

`api.ts`, next to `sendChatMessage`:

```ts
/** The revise drawer's door: the SAME turn as `sendChatMessage`, run as a
 * `chat_turn` job so nothing between the browser and the process can cut a
 * multi-minute planner call. Poll `getJob(job_id)`; on `succeeded` re-hydrate
 * history + pending approval (`progress.turn` carries the `ChatTurnOut`). */
export function sendChatMessageAsync(sessionId: string, content: string): Promise<JobAccepted> {
  return request<JobAccepted>(`/chat/${sessionId}/messages?async=1`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
}
```

`chat-panel.tsx::sendContent` — at the top of the `try`, before the streaming attempt:

```ts
      if (rootId) {
        // Drawer turns never stream (they are tool-heavy) and can run for
        // minutes under claude -p: send as a job, poll, hydrate. The placeholder
        // bubble becomes the "planning…" status the drawer already renders.
        setMessages((prev) => prev.filter((m) => m.id !== streamId));
        setJobPending(true);
        try {
          const accepted = await sendChatMessageAsync(sessionId, content);
          const job = await waitForJob(accepted.job_id);
          if (job.status === "failed") {
            setComposerError(jobErrorText(job, tJobErrors));
          } else if (job.status !== "succeeded") {
            setComposerError(t("job.stillGenerating"));
          }
          await hydrate();
          const turn = job.progress?.turn as ChatTurnOut | undefined;
          if (turn?.status === "job_pending" && turn.job_id) void pollJob(turn.job_id);
          if (turn?.status === "answer") fetchSuggestions();
        } finally {
          setJobPending(false);
        }
        return;
      }
```

Import `sendChatMessageAsync` and `type ChatTurnOut` from `@/lib/api`. `hydrate()` already moves a trailing assistant row into the approval card when `/pending` returns one, so the approval flow needs nothing more. Keep `MAX_POLLS`/`POLL_INTERVAL_MS` (5 min) — raise `MAX_POLLS` to `300` (10 min) since a claude -p planner turn was measured at 6-8 min.

- [ ] **Step 4: Run tests**

Run: `cd apps/web && npx tsc --noEmit && npm run lint && npx playwright test tests/revise-async.spec.ts tests/curricula-revise.spec.ts`
Expected: PASS. If `curricula-revise.spec.ts` mocks `POST /chat/{id}/messages` synchronously, update its handler to answer `?async=1` with a 202 + a `jobs/{id}` mock returning `succeeded` with `progress.turn` = the old sync body (the test's assertions about the approval card stay).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/lib/api.ts apps/web/src/components/chat/chat-panel.tsx apps/web/tests/revise-async.spec.ts apps/web/tests/curricula-revise.spec.ts
git commit -m "feat(web): the revise drawer sends its turn as a job and polls it home"
```

# Unit 1 — Hand edits become first-class

### Task 1.1: `tutor_edit.py` — marker on save, Greek-safe word recount

**Files:**
- Create: `apps/api/app/curriculum/tutor_edit.py`
- Modify: `apps/api/app/routers/curriculum.py:1069-1103` (`update_block`), `apps/api/app/curriculum/revise.py:960-973` (`_recompute_lesson_word_count` delegates)
- Test: `apps/api/tests/test_tutor_edited.py` (new)

**Interfaces:**
- Produces:
  - `tutor_edit.mark_tutor_edit(block: Block, previous_body: str | None, now: datetime | None = None) -> None` — sets `meta.tutor_edited = {"at": iso, "prev_body": <baseline>, "count": n}`; baseline is the FIRST previous body since the last AI write.
  - `tutor_edit.clear_tutor_edit(block: Block) -> None` — removes `meta.tutor_edited` (whole-dict reassignment).
  - `tutor_edit.recompute_lesson_words(db, lesson: Block) -> int` — `depth.count_words` over the lesson's segments + the lesson body (summary); sets `word_count`, `meets_floor` (when `floor_words` stored). Returns the count.
  - `tutor_edit.tutor_edited_sections(db, lesson: Block) -> dict[str, dict]` — `{section_key_or_title: {"body": current, "prev_body": baseline, "at": iso}}`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_tutor_edited.py
"""A hand edit leaves a trace. Until now `PATCH /blocks/{id}` was a bare
setattr: no baseline, no marker, no word-count refresh — so «3.056 λέξεις»
never moved and every redraft silently overwrote the tutor's work."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.curriculum import tutor_edit
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block

client = TestClient(app)

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


@pytest.fixture
def lesson_with_two_segments():
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", language="el", is_template=True, meta={})
    db.add(course); db.flush()
    module = Block(kind="module", title="Ξύλα", parent_id=course.id, order=0, language="el", meta={})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Μπράτσο", parent_id=module.id, order=0, language="el",
                   body="Περίληψη τριών λέξεων.",
                   meta={"draft_status": "ready", "word_count": 999, "floor_words": 5, "meets_floor": True})
    db.add(lesson); db.flush()
    theory = Block(kind="segment", title="Θεωρία", body="Ο σφένδαμος είναι σκληρός.", order=0,
                   parent_id=lesson.id, language="el", meta={"section": "theory"})
    warm = Block(kind="segment", title="Ζέσταμα", body="Μία δύο τρεις.", order=1,
                 parent_id=lesson.id, language="el", meta={"section": "warm_up"})
    db.add_all([theory, warm]); db.commit()
    yield db, lesson, theory, warm
    db.close()


def test_patch_body_marks_the_segment_and_keeps_the_first_baseline(lesson_with_two_segments):
    db, lesson, theory, warm = lesson_with_two_segments
    r = client.patch(f"/blocks/{theory.id}", json={"body": "Ο σφένδαμος είναι πολύ σκληρός."})
    assert r.status_code == 200
    r2 = client.patch(f"/blocks/{theory.id}", json={"body": "Ο σφένδαμος είναι εξαιρετικά σκληρός."})
    assert r2.status_code == 200
    db.expire_all()
    te = db.get(Block, theory.id).meta["tutor_edited"]
    assert te["prev_body"] == "Ο σφένδαμος είναι σκληρός."     # first baseline, not the second
    assert te["count"] == 2 and te["at"]


def test_patch_body_recomputes_the_lesson_word_count_greek_safe(lesson_with_two_segments):
    db, lesson, theory, warm = lesson_with_two_segments
    client.patch(f"/blocks/{theory.id}", json={"body": "Μία δύο τρεις τέσσερις πέντε έξι επτά οκτώ."})
    db.expire_all()
    meta = db.get(Block, lesson.id).meta
    # summary 3 + theory 8 + warm 3 = 14, counted with \w+ (Greek-safe), not .split()
    assert meta["word_count"] == 14
    assert meta["meets_floor"] is True


def test_patch_title_only_does_not_mark(lesson_with_two_segments):
    db, lesson, theory, warm = lesson_with_two_segments
    client.patch(f"/blocks/{theory.id}", json={"title": "Θεωρία (νέα)"})
    db.expire_all()
    assert "tutor_edited" not in (db.get(Block, theory.id).meta or {})


def test_clear_and_sections_helpers(lesson_with_two_segments):
    db, lesson, theory, warm = lesson_with_two_segments
    tutor_edit.mark_tutor_edit(theory, previous_body=theory.body)
    theory.body = "νέο"
    db.commit()
    sections = tutor_edit.tutor_edited_sections(db, lesson)
    assert set(sections) == {"theory"}
    assert sections["theory"]["prev_body"] == "Ο σφένδαμος είναι σκληρός."
    tutor_edit.clear_tutor_edit(theory)
    db.commit()
    assert tutor_edit.tutor_edited_sections(db, lesson) == {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_tutor_edited.py -q`
Expected: FAIL — `ModuleNotFoundError: app.curriculum.tutor_edit`.

- [ ] **Step 3: Implement the module**

```python
# apps/api/app/curriculum/tutor_edit.py
"""The tutor's hand edits, as data the rest of the engine can see.

`meta.tutor_edited = {"at": iso, "prev_body": baseline, "count": n}` on a
segment (or a lesson, for its summary). `prev_body` is the body AS THE AI LAST
WROTE IT — the first previous body since the last AI write — so «Τι άλλαξε;»
and the propagation planner always diff the tutor's version against the
model's, never against his own earlier save. Every AI write path
(`refine`, `segment_generate`, `persist_lesson`, `restore`) calls
`clear_tutor_edit` on the segments it rewrites.

Whole-dict reassignment throughout: `Block.meta` is plain sa.JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.curriculum.depth import count_words
from app.models.block import Block


def mark_tutor_edit(block: Block, previous_body: str | None, now: datetime | None = None) -> None:
    meta = dict(block.meta or {})
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    current = meta.get("tutor_edited")
    if isinstance(current, dict) and "prev_body" in current:
        entry = {**current, "at": stamp, "count": int(current.get("count") or 0) + 1}
    else:
        entry = {"at": stamp, "prev_body": previous_body or "", "count": 1}
    block.meta = {**meta, "tutor_edited": entry}


def clear_tutor_edit(block: Block) -> None:
    meta = block.meta or {}
    if "tutor_edited" not in meta:
        return
    block.meta = {k: v for k, v in meta.items() if k != "tutor_edited"}


def _segments(db, lesson: Block) -> list[Block]:
    return db.scalars(
        select(Block)
        .where(Block.parent_id == lesson.id, Block.kind == "segment")
        .order_by(Block.order)
    ).all()


def recompute_lesson_words(db, lesson: Block) -> int:
    """`word_count`/`meets_floor` from the segments AS THEY STAND — Greek-safe
    (`depth.count_words`, `\\w+`), the same counter `measure()` uses at draft
    time, so a hand edit and a redraft agree on what a word is."""
    total = count_words(lesson.body) + sum(count_words(s.body) for s in _segments(db, lesson))
    meta = {**(lesson.meta or {}), "word_count": total}
    floor = meta.get("floor_words")
    if floor is not None:
        meta["meets_floor"] = total >= int(floor)
    lesson.meta = meta
    return total


def tutor_edited_sections(db, lesson: Block) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for seg in _segments(db, lesson):
        te = (seg.meta or {}).get("tutor_edited")
        if not isinstance(te, dict):
            continue
        key = (seg.meta or {}).get("section") or seg.title
        out[key] = {"body": seg.body or "", "prev_body": te.get("prev_body") or "",
                    "at": te.get("at"), "title": seg.title}
    return out
```

- [ ] **Step 4: Wire the PATCH route and the old recount**

In `routers/curriculum.py::update_block`, replace the `for field, value in updates.items(): setattr(...)` + `db.commit()` with:

```python
    previous_body = block.body
    for field, value in updates.items():
        setattr(block, field, value)
    if "body" in updates and (updates["body"] or "") != (previous_body or ""):
        if block.kind in ("segment", "lesson"):
            mark_tutor_edit(block, previous_body)
        lesson = block if block.kind == "lesson" else (
            db.get(Block, block.parent_id) if block.kind == "segment" else None
        )
        if lesson is not None and lesson.kind == "lesson":
            recompute_lesson_words(db, lesson)
    db.commit()
```

Import: `from app.curriculum.tutor_edit import mark_tutor_edit, recompute_lesson_words`.

In `revise.py`, make `_recompute_lesson_word_count` a thin delegate so every existing caller gets the Greek-safe counter:

```python
def _recompute_lesson_word_count(db, lesson: Block) -> None:
    """Delegates to `tutor_edit.recompute_lesson_words` — one counter for the
    whole engine (`depth.count_words`). Kept under its old name for the callers
    in this module, `segment_generate.py` and `restore.py`."""
    from app.curriculum.tutor_edit import recompute_lesson_words
    recompute_lesson_words(db, lesson)
```

- [ ] **Step 5: Run tests**

Run: `cd apps/api && pytest tests/test_tutor_edited.py tests/test_curriculum_editing.py tests/test_surgical_revise.py tests/test_revise_snapshots.py -q`
Expected: PASS. If an existing test pinned a `.split()` count on ASCII text, the `\w+` count is identical for ASCII; if a test used punctuation-glued tokens, update the expectation to the Greek-safe number.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/curriculum/tutor_edit.py apps/api/app/routers/curriculum.py apps/api/app/curriculum/revise.py apps/api/tests/test_tutor_edited.py
git commit -m "feat(curriculum): a hand edit leaves a trace — baseline, marker, live word count"
```

### Task 1.2: AI writes reset the baseline

**Files:**
- Modify: `apps/api/app/curriculum/refine.py:168-177`, `apps/api/app/curriculum/segment_generate.py:249-266`, `apps/api/app/curriculum/restore.py:87-104` (recreated segments carry no marker — already true; add a comment), `apps/api/app/curriculum/draft.py::persist_lesson` (in Task 1.3, new rows carry no marker)
- Test: `apps/api/tests/test_tutor_edited.py` (extend)

- [ ] **Step 1: Failing tests**

Append to `apps/api/tests/test_tutor_edited.py`:

```python
def test_refine_clears_the_tutor_marker(lesson_with_two_segments, monkeypatch):
    from app.curriculum import refine as refine_mod
    db, lesson, theory, warm = lesson_with_two_segments
    tutor_edit.mark_tutor_edit(theory, previous_body=theory.body); db.commit()

    class P:
        def guided_json(self, messages, schema, role="draft"):
            return {"title": "Θεωρία", "body": "Ξαναγραμμένο από το AI."}
    monkeypatch.setattr(refine_mod, "get_provider", lambda: P())
    monkeypatch.setattr(refine_mod, "search", lambda *a, **k: [])
    refine_mod.refine_block(db, theory, "κάν' το πιο απλό"); db.commit()
    db.expire_all()
    meta = db.get(Block, theory.id).meta
    assert "tutor_edited" not in meta and meta["prev_body"]


def test_generate_segment_clears_the_tutor_marker(lesson_with_two_segments, monkeypatch):
    from app.curriculum import segment_generate as sg
    db, lesson, theory, warm = lesson_with_two_segments
    tutor_edit.mark_tutor_edit(warm, previous_body=warm.body)
    warm.meta = {**warm.meta, "segment_status": "queued", "segment_instruction": "πιο ζωντανό"}
    db.commit()

    class P:
        def guided_json(self, messages, schema, role="draft"):
            return {"title": "Ζέσταμα", "body": "Νέο ζέσταμα."}
    monkeypatch.setattr(sg, "get_provider", lambda: P())
    monkeypatch.setattr(sg, "ground_topic", lambda *a, **k: [])
    sg.generate_segment(db, warm.id)
    db.expire_all()
    assert "tutor_edited" not in db.get(Block, warm.id).meta
```

(Check `generate_segment`'s real signature with `grep -n "^def generate_segment" apps/api/app/curriculum/segment_generate.py` and adapt the call; it takes the segment id and commits itself.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_tutor_edited.py -q -k clears`
Expected: 2 FAIL (marker still present).

- [ ] **Step 3: Implement**

`refine.py` — in `refine_block`, build the meta without the marker:

```python
    block.meta = {
        **{k: v for k, v in meta.items() if k != "tutor_edited"},
        "prev_body": prev_body,
        "prev_title": prev_title,
        "refined": True,
        "refine_instruction": instruction,
    }
```

`segment_generate.py` — same filter in the final `segment.meta = {...}`:

```python
    segment.meta = {
        **{k: v for k, v in meta.items() if k not in ("segment_instruction", "tutor_edited")},
        "segment_status": "done",
        "citations": citations,
    }
```

`restore.py` — the recreated rows already start from `{"segment_status": "ready"}`; add above step 3: `# A restored body is an AI-era body: no tutor_edited marker rides along.`

- [ ] **Step 4: Run tests**

Run: `cd apps/api && pytest tests/test_tutor_edited.py tests/test_surgical_revise.py tests/test_revise_snapshots.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/curriculum/refine.py apps/api/app/curriculum/segment_generate.py apps/api/app/curriculum/restore.py apps/api/tests/test_tutor_edited.py
git commit -m "feat(curriculum): every AI write resets the hand-edit baseline"
```

### Task 1.3: Draft *around* fixed sections — schema exclude, `LESSON_FIXED_BLOCK`, in-place persist

**Files:**
- Modify: `apps/api/app/curriculum/blueprint.py:167-189` (`build_lesson_schema`), `apps/api/app/curriculum/draft.py` (new block constants; `build_lesson_messages`; `draft_lesson`; `persist_lesson`), `apps/api/app/prompts/registry.py` (entry `lesson.fixed`)
- Test: `apps/api/tests/test_persist_keep.py` (new), `apps/api/tests/test_blueprint_schema_golden.py` (must stay green)

**Interfaces:**
- Produces:
  - `blueprint.build_lesson_schema(bp, exclude: set[str] | frozenset[str] = frozenset()) -> dict` — omits those keys from `properties` and `required`.
  - `draft.LESSON_FIXED_BLOCK`, `draft.LESSON_FIXED_SLICE_ID = "lesson.fixed"`, `draft.FIXED_SECTION_CHAR_LIMIT = 20_000`.
  - `draft.build_lesson_messages(..., fixed_sections: dict[str, str] | None = None)` — renders the block after `revise_block`, before `deepen_block`; empty when None/{} (byte-identical).
  - `draft.draft_lesson(..., fixed_sections: dict[str, str] | None = None, exclude_sections: set[str] = frozenset())` — passes both through; `measure()` is computed on the drafted dict PLUS the fixed sections (so word counts stay honest).
  - `draft.persist_lesson(..., keep: set[str] = frozenset())` — segments whose `meta.section` is in `keep` are neither deleted nor regenerated; regenerated sections are written IN PLACE when a segment with that `meta.section` exists (id kept; body/citations/est_minutes replaced; `tutor_edited` cleared), created otherwise; order follows the blueprint, customs after.

- [ ] **Step 1: Write the failing tests**

```python
# apps/api/tests/test_persist_keep.py
"""`persist_lesson` writes AROUND the tutor's sections and keeps segment ids.

Two things a redraft used to do that made hand edits impossible to keep: delete
every non-custom segment (so a `tutor_edited` theory vanished) and recreate rows
(so an artifact pinned to a segment lost its parent)."""
import uuid

import pytest
from sqlalchemy import text

from app.curriculum import blueprint as bp_mod
from app.curriculum.corpus import LibraryContext
from app.curriculum.depth import Measurement
from app.curriculum.draft import build_lesson_messages, persist_lesson, LessonContext, LESSON_FIXED_SLICE_ID
from app.db import Base, SessionLocal, engine
from app.models.block import Block

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


def _empty_library() -> LibraryContext:
    # Copy the constructor used by tests/test_lesson_draft.py's empty-library fixture.
    from tests.test_lesson_draft import empty_library  # noqa: F401  (reuse if exported)
    return empty_library()


@pytest.fixture
def drafted_lesson():
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", language="el", is_template=True,
                   meta={"blueprint": bp_mod.default_blueprint()})
    db.add(course); db.flush()
    module = Block(kind="module", title="Ξύλα", parent_id=course.id, order=0, language="el", meta={})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Μπράτσο", parent_id=module.id, order=0, language="el",
                   meta={"draft_status": "drafting"})
    db.add(lesson); db.flush()
    segs = {}
    for i, key in enumerate(bp_mod.section_keys(bp_mod.default_blueprint())):
        s = Block(kind="segment", title=key, body=f"παλιό {key}", order=i, parent_id=lesson.id,
                  language="el", meta={"section": key})
        db.add(s); segs[key] = s
    segs["theory"].meta = {"section": "theory", "tutor_edited": {"at": "x", "prev_body": "ai", "count": 1}}
    db.commit()
    yield db, lesson, segs
    db.close()


def test_schema_exclude_drops_keys_from_properties_and_required():
    bp = bp_mod.default_blueprint()
    schema = bp_mod.build_lesson_schema(bp, exclude={"theory", "recap"})
    assert "theory" not in schema["properties"] and "recap" not in schema["properties"]
    assert "theory" not in schema["required"] and "recap" not in schema["required"]
    assert "exercises" in schema["properties"]
    assert bp_mod.build_lesson_schema(bp) == bp_mod.build_lesson_schema(bp, exclude=set())


def test_fixed_block_is_empty_when_absent_and_present_when_given():
    ctx = LessonContext(lesson_title="Μ", lesson_objective="ο", module_title="Ξ", module_objective="",
                        course_title="Ή", tier="general_knowledge", position="lesson 1 of 1 in module 1 of 1",
                        minutes=50, teaching_minutes=50, target_words=2750, floor_words=2200)
    lib = _empty_library()
    base = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None, course_brief=None)
    same = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None, course_brief=None,
                                 fixed_sections={})
    assert base[-1]["content"] == same[-1]["content"]
    fixed = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None, course_brief=None,
                                  fixed_sections={"theory": "Ο σφένδαμος."})
    assert "ΤΟΥ ΚΑΘΗΓΗΤΗ" in fixed[-1]["content"] and "Ο σφένδαμος." in fixed[-1]["content"]


def test_persist_keep_preserves_kept_rows_and_rewrites_others_in_place(drafted_lesson):
    db, lesson, segs = drafted_lesson
    ids_before = {k: s.id for k, s in segs.items()}
    drafted = {"title": "Μπράτσο", "summary": "νέα περίληψη"}
    for key in bp_mod.section_keys(bp_mod.default_blueprint()):
        if key == "theory":
            continue
        drafted[key] = {"body": f"νέο {key}", "citations": []} if key not in ("exercises", "qa_prompts") \
            else {"body": f"νέο {key}", "items": [], "citations": []}
    m = Measurement(total_words=100, target=2750, floor=2200, per_section={}, thin_sections=[])
    persist_lesson(db, lesson, drafted, m, _empty_library(), bp_mod.default_blueprint(),
                   teaching_minutes=50, keep={"theory"})
    db.commit(); db.expire_all()
    now = {(s.meta or {}).get("section"): s for s in db.query(Block).filter_by(parent_id=lesson.id).all()}
    assert now["theory"].body == "παλιό theory"
    assert now["theory"].id == ids_before["theory"]
    assert "tutor_edited" in now["theory"].meta
    assert now["warm_up"].body == "νέο warm_up"
    assert now["warm_up"].id == ids_before["warm_up"]          # in place, id kept
    assert "tutor_edited" not in (now["warm_up"].meta or {})
    assert [s.order for s in sorted(now.values(), key=lambda s: s.order)] == list(range(len(now)))
    assert db.get(Block, lesson.id).meta["draft_status"] == "ready"
```

If `tests/test_lesson_draft.py` has no importable `empty_library`, build one inline the way that file constructs its `LibraryContext` (read it first: `grep -n "LibraryContext(" apps/api/tests/test_lesson_draft.py`).

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && pytest tests/test_persist_keep.py -q`
Expected: FAIL — `TypeError: build_lesson_schema() got an unexpected keyword argument 'exclude'`.

- [ ] **Step 3: Implement `build_lesson_schema(exclude)`**

```python
def build_lesson_schema(bp: dict, exclude: set[str] | frozenset[str] = frozenset()) -> dict:
    """The guided-json schema for `bp` — the replacement for the module-level
    `depth.LESSON_DRAFT_SCHEMA`. For the default blueprint (and an empty
    `exclude`) it reproduces that schema byte-for-byte (the golden test).

    `exclude`: section keys the model must NOT write this time — the tutor's own
    sections (`tutor_edited`) on a redraft, or the sections he left unticked on
    the lesson panel's plan card. They are removed from `properties` AND
    `required`, so the model physically cannot overwrite them; their text
    reaches it as `LESSON_FIXED_BLOCK` instead."""
    props: dict = {
        "title": copy.deepcopy(depth._TITLE_PROP),
        "summary": copy.deepcopy(depth._SUMMARY_PROP),
    }
    keys: list[str] = []
    for s in enabled_sections(bp):
        key = s["key"]
        if key in exclude:
            continue
        keys.append(key)
        if s["kind"] == _KIND_PROSE:
            props[key] = depth._prose_section(s["description"])
        else:
            props[key] = depth._KIND_BUILDERS[s["kind"]](s["description"])
    return {
        "type": "object",
        "properties": props,
        "required": ["title", "summary", *keys],
        "additionalProperties": False,
    }
```

- [ ] **Step 4: Implement the block and the builder parameter in `draft.py`**

After `LESSON_REVISE_SLICE_ID`:

```python
# THE TUTOR'S SECTIONS, SHOWN AS FIXED. On a redraft that must keep his hand-
# edited theory (or on the lesson panel, the sections he left unticked), those
# sections are removed from the guided-json schema and shown here instead, so
# the model writes the others TO FIT them. Per-section cap is generous — a
# hand-written theory can run 16K chars and must be seen whole.
LESSON_FIXED_BLOCK = (
    "\n\nΟι παρακάτω ενότητες είναι ΤΟΥ ΚΑΘΗΓΗΤΗ και ΔΕΝ αλλάζουν — δεν τις "
    "γράφεις ξανά. Γράψε τις υπόλοιπες ενότητες ώστε να δένουν απόλυτα με αυτές: "
    "ίδια ορολογία, ίδια παραδείγματα, ίδια σειρά ιδεών, καμία αντίφαση.\n{fixed}"
)
LESSON_FIXED_SLICE_ID = "lesson.fixed"
FIXED_SECTION_CHAR_LIMIT = 20_000
FIXED_SECTION_TRUNCATION_MARKER = "\n…[το υπόλοιπο περικόπηκε — υπάρχει και ισχύει]"


def _fixed_body(text: str | None) -> str:
    t = (text or "").strip()
    if len(t) <= FIXED_SECTION_CHAR_LIMIT:
        return t
    return t[:FIXED_SECTION_CHAR_LIMIT] + FIXED_SECTION_TRUNCATION_MARKER
```

In `build_lesson_messages` add the parameter `fixed_sections: dict[str, str] | None = None` after `revise_current`, and render:

```python
    fixed_block = ""
    if fixed_sections:
        import json

        fixed_block = resolve(source, LESSON_FIXED_SLICE_ID, LESSON_FIXED_BLOCK).format(
            fixed=json.dumps({k: _fixed_body(v) for k, v in fixed_sections.items()}, ensure_ascii=False),
        )
```

and in the `.format(...)` call pass `revise_block=revise_block + fixed_block` (appending to the existing placeholder keeps `LESSON_TAIL` byte-identical — no new placeholder, so a tutor override of `lesson.draft` written before today still renders it).

`draft_lesson`: add `fixed_sections: dict[str, str] | None = None, exclude_sections: set[str] | frozenset[str] = frozenset()`; use `schema = _bp.build_lesson_schema(bp, exclude=set(exclude_sections))`; pass `fixed_sections=fixed_sections` to every `build_lesson_messages` call (first draft, repair reuses `messages`, deepen). For measurement, merge the fixed text back before `measure`:

```python
    def _with_fixed(d: dict) -> dict:
        if not fixed_sections:
            return d
        return {**d, **{k: {"body": v} for k, v in fixed_sections.items() if k not in d}}

    m = measure(_with_fixed(lesson), bp, teaching_minutes=ctx.teaching_minutes)
```
and the same `_with_fixed(deeper)` for the deepen measurement.

- [ ] **Step 5: Implement `persist_lesson(keep=...)`**

Replace the delete-all + create loop with in-place writes:

```python
def persist_lesson(
    db, lesson_block, lesson: dict, m: Measurement, library: LibraryContext,
    blueprint: dict | None = None, *, teaching_minutes: int,
    keep: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Write the drafted sections onto `lesson_block`'s segments and flip it to
    `ready`. Caller commits.

    IN PLACE, BY `meta.section`. A segment that already exists for a blueprint
    key is UPDATED (same row, same id — an artifact attached to it stays
    attached); a key with no row gets a new one; a row whose key the blueprint
    no longer has is deleted. `keep` names sections that are NOT touched at all
    (the tutor's hand-edited sections on a redraft; the unticked sections on the
    lesson panel) — they keep body, meta and `tutor_edited`. Custom segments
    (`meta.custom`) are never deleted and are re-ordered after the blueprint
    sections. Every rewritten section loses its `tutor_edited` marker: the AI
    just wrote it.
    """
    from sqlalchemy import select
    from app.models.block import Block
    from app.curriculum import blueprint as _bp

    bp = blueprint if blueprint is not None else _bp.default_blueprint()
    children = db.scalars(
        select(Block).where(Block.parent_id == lesson_block.id).order_by(Block.order)
    ).all()
    customs = [b for b in children if (b.meta or {}).get("custom")]
    by_key: dict[str, Block] = {}
    for b in children:
        key = (b.meta or {}).get("section")
        if key and not (b.meta or {}).get("custom") and key not in by_key:
            by_key[key] = b

    lang = lesson_block.language if lesson_block.language in SECTION_LABELS else "el"
    labels = _bp.section_labels(bp, lang)
    sections_by_key = {s["key"]: s for s in bp["sections"]}
    citations_all: list[dict] = []
    seen: set[str] = set()

    order = 0
    for name in _bp.section_keys(bp):
        if name in keep and name in by_key:
            existing = by_key[name]
            existing.order = order
            citations_all.extend((existing.meta or {}).get("citations") or [])
            seen.add(name)
            order += 1
            continue
        section = lesson.get(name)
        if not isinstance(section, dict):
            continue
        body = _render_section(name, section)
        if not body:
            continue
        cites = [
            {
                "source_id": str(library.ref_to_source_id.get(str(c["source_id"]))),
                "source_ref": c["source_id"],
                "source_title": next(
                    (s["title"] for s in library.sources if s["ref"] == c["source_id"]), None,
                ),
                "page": c["page"],
            }
            for c in section.get("citations") or []
            if isinstance(c, dict) and str(c.get("source_id")) in library.ref_to_source_id
        ]
        citations_all.extend(cites)
        new_meta = {
            "section": name,
            "audience": sections_by_key[name].get("audience"),
            "citations": cites,
        }
        existing = by_key.get(name)
        if existing is not None:
            existing.title = labels[name]
            existing.body = body
            existing.est_minutes = _section_minutes(name, teaching_minutes, bp)
            existing.order = order
            existing.meta = new_meta          # drops tutor_edited/prev_body: the AI wrote it
        else:
            db.add(Block(
                kind="segment", title=labels[name], body=body,
                est_minutes=_section_minutes(name, teaching_minutes, bp),
                order=order, parent_id=lesson_block.id, language=lesson_block.language,
                meta=new_meta,
            ))
        seen.add(name)
        order += 1

    # Rows for keys the blueprint no longer has (or that came back empty) go —
    # unless kept. Customs never go.
    for key, row in by_key.items():
        if key not in seen and key not in keep:
            db.delete(row)
    for custom in customs:
        custom.order = order
        order += 1
    db.flush()

    if lesson.get("summary"):
        lesson_block.body = strip_inline_citations(lesson["summary"])
    db.expire(lesson_block, ["children"])
    lesson_block.meta = {
        **(lesson_block.meta or {}),
        "draft_status": "ready",
        "word_count": m.total_words,
        "target_words": m.target,
        "floor_words": m.floor,
        "meets_floor": m.meets_floor,
        "thin_sections": m.thin_sections,
        "citations": citations_all,
        "error": None,
    }
```

Keep the original docstring's three traps (query-not-`.children`, `expire`, customs) in the new docstring.

- [ ] **Step 6: Register the prompt fragment**

In `registry.py`, after the `lesson.revise` entry, add a `lesson.fixed` `PromptEntry` with `kind="fragment"`, `flow="lesson"`, `language_from_course=True`, `source_ref="app/curriculum/draft.py:<line of LESSON_FIXED_BLOCK>"`, Greek `title_el="Όταν κάποιες ενότητες είναι δικές σου: τι μένει σταθερό"`, `what_it_does_el` (explain: the tutor's hand-edited sections are shown as fixed and the rest are written to fit them), `when_it_runs_el` («Όταν ξαναγράφεται μάθημα που έχει ενότητες αλλαγμένες από σένα, ή όταν στο πλαίσιο AI του μαθήματος αφήνεις ενότητες ατσεκάριστες»), `source_of_truth=lambda: build_lesson_messages`, `build=_build_lesson_fixed` (copy `_build_lesson_revise` and render with `fixed_sections={"theory": "…"}`), `slices=(Slice(id=LESSON_FIXED_SLICE_ID, label_el="Το κείμενο της οδηγίας", default=LESSON_FIXED_BLOCK, kind="replace"),)`.

- [ ] **Step 7: Run tests and regenerate the baseline (new entry only)**

Run: `cd apps/api && pytest tests/test_persist_keep.py tests/test_blueprint_schema_golden.py tests/test_lesson_draft.py tests/test_curriculum_draft_job.py tests/test_surgical_revise.py tests/test_prompts_registry.py -q`
Then `python -m tests.prompt_baseline && git diff --stat tests/fixtures/prompt_renders_baseline.json` — the diff must ADD the `lesson.fixed` renders and change nothing else. `pytest tests/test_prompts_byte_identity.py -q`.

- [ ] **Step 8: Commit**

```bash
git add apps/api/app/curriculum/blueprint.py apps/api/app/curriculum/draft.py apps/api/app/prompts/registry.py apps/api/tests/test_persist_keep.py apps/api/tests/fixtures/prompt_renders_baseline.json
git commit -m "feat(draft): a lesson can be written AROUND fixed sections — schema exclude, in-place persist, ids kept"
```

### Task 1.4: Redraft/Deepen/modify_lesson keep the tutor's sections

**Files:**
- Modify: `apps/api/app/jobs/curriculum_draft.py:255-386` (`_draft_one`), `apps/web/src/messages/el.json:95` + `en.json` (`redraftConfirmBody`)
- Test: `apps/api/tests/test_curriculum_draft_job.py` (extend)

- [ ] **Step 1: Failing test**

Append to `apps/api/tests/test_curriculum_draft_job.py` (reuse that file's fixtures for a course with a queued lesson and its `draft_lesson` stub pattern — read the file's existing `monkeypatch.setattr(curriculum_draft, "draft_lesson", ...)` test first and copy its shape):

```python
def test_draft_one_keeps_tutor_edited_sections_and_shows_them_as_fixed(monkeypatch, ...):
    # Arrange: a `queued` lesson (deepen) with an existing theory segment marked tutor_edited.
    # Act: run `_draft_one(lesson_id, plan)` with `draft_lesson` stubbed to capture kwargs.
    # Assert: captured kwargs have exclude_sections == {"theory"} and
    #         fixed_sections == {"theory": <its body>}; after persist the theory row
    #         still has its old body, its id, and its tutor_edited marker.
```

Write the concrete test body against the fixture names in that file.

- [ ] **Step 2: Run** — `cd apps/api && pytest tests/test_curriculum_draft_job.py -q -k tutor_edited` → FAIL.

- [ ] **Step 3: Implement** — in `_draft_one`, after `revise_current` is built (still before `db.close()`):

```python
        # THE TUTOR'S SECTIONS SURVIVE A REDRAFT. Any segment he edited by hand
        # (`meta.tutor_edited`, Unit 1) is taken out of the schema and shown as
        # fixed text; `persist_lesson(keep=...)` then leaves its row alone.
        from app.curriculum.tutor_edit import tutor_edited_sections
        edited = tutor_edited_sections(db, lesson)
        keep = set(edited)
        fixed_sections = {k: v["body"] for k, v in edited.items()} or None
```
and pass `fixed_sections=fixed_sections, exclude_sections=keep` to `draft_lesson(...)`, and `keep=keep` to `persist_lesson(...)`. Custom-key sections (`custom:*`) that were hand-edited are already spared by the customs rule; `keep` covers blueprint keys.

`el.json` `redraftConfirmBody`: replace the last sentence with «Ό,τι έχεις αλλάξει με το χέρι σε μια ενότητα ΚΡΑΤΙΕΤΑΙ — οι υπόλοιπες ενότητες ξαναγράφονται ώστε να δένουν με αυτό.» and the English equivalent.

- [ ] **Step 4: Run** — `pytest tests/test_curriculum_draft_job.py tests/test_redraft_endpoint.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/jobs/curriculum_draft.py apps/api/tests/test_curriculum_draft_job.py apps/web/src/messages
git commit -m "feat(draft): redraft, deepen and revise write around the tutor's hand-edited sections"
```

### Task 1.5: Row badges → one AI button; live word count; tutor-edited chip

**Files:**
- Modify: `apps/web/src/components/curriculum/block-card.tsx:404-449` (badges), `:455-464` (action bar), segment body area (`:682-691`), `apps/web/src/lib/api.ts:700-754` (`BlockMeta.tutor_edited`), `apps/web/src/messages/{el,en}.json`
- Create: `apps/web/src/components/curriculum/lesson-ai-scope.tsx`
- Test: `apps/web/tests/tutor-edited.spec.ts` (new)

**Interfaces:**
- Produces: `LessonAiScopeProvider`, `useLessonAiScope(): {lesson: {id,title,moduleTitle} | null, openRequest: number, openForLesson(id,title,moduleTitle), close()}`; row button `data-testid="lesson-ai"`; segment chip `data-testid="segment-tutor-edited"` opening `WhatChanged` with `before=meta.tutor_edited.prev_body`; expanded-lesson line `data-testid="lesson-words-line"`; `BlockMeta.tutor_edited?: {at: string; prev_body: string; count: number}`.

- [ ] **Step 1: Failing Playwright test**

```ts
// apps/web/tests/tutor-edited.spec.ts
import { test, expect, type Route } from "@playwright/test";
// (same API_ORIGIN / CORS_HEADERS / json() helpers as revise-async.spec.ts)

test("the lesson row shows one AI button, and a hand-edited section shows its chip and diff", async ({ page }) => {
  const LESSON = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", SEG = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const tree = { id: ROOT, kind: "course", title: "Ήχος", body: null, est_minutes: null, order: 0, language: "el", plane: "content", student_id: null, meta: {}, children: [
    { id: "m1", kind: "module", title: "Ξύλα", body: null, est_minutes: null, order: 0, language: "el", plane: "content", student_id: null, meta: {}, children: [
      { id: LESSON, kind: "lesson", title: "Μπράτσο", body: "Περίληψη", est_minutes: 50, order: 0, language: "el", plane: "content", student_id: null,
        meta: { draft_status: "ready", word_count: 14, target_words: 2750, meets_floor: false }, children: [
        { id: SEG, kind: "segment", title: "Θεωρία", body: "Ο σφένδαμος είναι πολύ σκληρός.", est_minutes: 12, order: 0, language: "el", plane: "content", student_id: null,
          meta: { section: "theory", tutor_edited: { at: "2026-09-11T15:36:59Z", prev_body: "Ο σφένδαμος είναι σκληρός.", count: 1 } }, children: [] },
      ] },
    ] },
  ] };
  // route mocks as in revise-async.spec.ts, serving `tree` for /curricula/{ROOT}
  await page.goto(`/el/curricula/${ROOT}`);
  const row = page.getByTestId("block-card").filter({ hasText: "Μπράτσο" }).first();
  await expect(row.getByTestId("lesson-ai")).toBeVisible();
  await expect(row.getByTestId("lesson-word-count")).toHaveCount(0);
  await expect(row.getByTestId("lesson-status")).toHaveCount(0);      // ready → no pill
  await row.click();                                                    // expand
  await expect(page.getByTestId("lesson-words-line")).toContainText("14");
  await page.getByText("Θεωρία").first().click();
  await page.getByTestId("segment-tutor-edited").click();
  await expect(page.getByRole("dialog")).toContainText("πολύ σκληρός");
});
```
(Find the row testid with `grep -n 'data-testid="block-card' apps/web/src/components/curriculum/block-card.tsx`.)

- [ ] **Step 2: Run** → FAIL (no `lesson-ai`).

- [ ] **Step 3: Implement**

`lesson-ai-scope.tsx` — copy `revise-scope.tsx` verbatim and rename: state `lesson: {id, title, moduleTitle} | null`, `openRequest`, `openForLesson(id, title, moduleTitle)` (sets + bumps), `close()` (clears). Export `LessonAiScopeProvider`, `useLessonAiScope()` (null outside a provider).

`block-card.tsx`:
- status pill: render only when `draftStatus && draftStatus !== "ready"`.
- delete the `lesson-word-count` badge block.
- action bar, before Deepen:
```tsx
        {isLesson && lessonAi && (
          <Button type="button" variant="ghost" size="sm" data-testid="lesson-ai"
            disabled={draftStatus === "drafting" || draftStatus === "queued"}
            onClick={() => lessonAi.openForLesson(node.id, node.title, parentTitle ?? "")}>
            <Sparkles />
            {t("lessonAi")}
          </Button>
        )}
```
  where `const lessonAi = useLessonAiScope();` and `parentTitle` is a new optional prop threaded from the recursive render (`<BlockCard … parentTitle={node.title} />` for children) — add `parentTitle?: string` to the props interface.
- expanded lesson body (next to the summary): 
```tsx
          {isLesson && typeof wordCount === "number" && (
            <p data-testid="lesson-words-line" className={cn("text-xs text-muted-foreground", meta.meets_floor === false && "text-amber-600 dark:text-amber-400")}>
              {t("wordsLine", { count: wordCount, target: meta.target_words ?? 0 })}
            </p>
          )}
```
- segment body: when `meta.tutor_edited`, render a chip + `WhatChanged` (controlled `open` state local to the card):
```tsx
          {isSegment && meta.tutor_edited && (
            <>
              <Button type="button" size="sm" variant="ghost" data-testid="segment-tutor-edited" onClick={() => setTutorDiffOpen(true)}>
                <Pencil /> {t("tutorEdited")}
              </Button>
              <WhatChanged open={tutorDiffOpen} onOpenChange={setTutorDiffOpen}
                before={meta.tutor_edited.prev_body} after={node.body ?? ""} />
            </>
          )}
```

`api.ts` `BlockMeta`: add `tutor_edited?: { at: string; prev_body: string; count: number } | null;`.

Strings (`curricula.tree`): el `"lessonAi": "AI στο μάθημα"`, `"wordsLine": "{count, number} λέξεις · στόχος ~{target, number}"`, `"tutorEdited": "Επεξεργασμένο από σένα — τι άλλαξε;"`; en `"lessonAi": "AI on this lesson"`, `"wordsLine": "{count, number} words · target ~{target, number}"`, `"tutorEdited": "Edited by you — what changed?"`.

Mount `<LessonAiScopeProvider>` in `curricula/[rootId]/page.tsx` around the same subtree as `ReviseScopeProvider` (Task 2.5 mounts the panel inside it).

- [ ] **Step 4: Run** — `cd apps/web && npx tsc --noEmit && npm run lint && npx playwright test tests/tutor-edited.spec.ts tests/what-changed.spec.ts tests/curricula-resume.spec.ts` → PASS. Update any existing spec that asserted `lesson-word-count` or the «Έτοιμο» pill on a ready lesson.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src apps/web/tests/tutor-edited.spec.ts
git commit -m "feat(web): the lesson row gets one AI button; hand-edited sections say so and show the diff"
```

# Unit 2 — «AI στο μάθημα»

### Task 2.1: `lesson_ai.py` — the planner (prompt, schema, validation)

**Files:**
- Create: `apps/api/app/curriculum/lesson_ai.py`
- Modify: `apps/api/app/prompts/registry.py` (entries `lesson.ai.plan`, `lesson.ai.plan.user`)
- Test: `apps/api/tests/test_lesson_ai.py` (new)

**Interfaces:**
- Consumes: `tutor_edit.tutor_edited_sections(db, lesson)`, `ground.ground_topic(db, query, source_ids=..., k=...)` (check its exact signature in `app/curriculum/ground.py`), `blueprint.blueprint_from_course_meta`, `enabled_sections`, `section_labels`, `i18n.language_directive/curriculum_style/answer_in`, `overrides.resolve`.
- Produces:
  - `LESSON_PLAN_SCHEMA: dict`
  - `LESSON_AI_PLAN_SYSTEM`, `LESSON_AI_PLAN_USER` + slice ids `"lesson.ai.plan"`, `"lesson.ai.plan.user"`
  - `build_plan_messages(*, course_title, course_brief, module_title, module_objective, neighbours: str, lesson_title, lesson_objective, blueprint_lines: str, sections: list[dict], edited: dict[str, dict], retrieved: str | None, instruction: str, note: str | None, language: str, source=None) -> list[dict]` (pure)
  - `plan_lesson_change(db, lesson_id: uuid.UUID, *, instruction: str, note: str | None) -> dict` → `{"summary", "sections": [{"section","action","reason","brief","title","tutor_edited"}], "note_to_tutor", "dropped": [...], "impact": {"rewrite_count", "est_words"}}`
  - `validate_plan(raw: dict, existing: list[dict]) -> dict` — covers every existing section exactly once (missing → keep), unknown keys → `dropped`.
  - `class LessonAiError(ValueError)`.
  - `SECTION_CHAR_LIMIT = 20_000`.

- [ ] **Step 1: Failing tests**

```python
# apps/api/tests/test_lesson_ai.py
"""The lesson panel's planner sees the WHOLE lesson — including the tutor's
before/after — and returns one verdict per section. The course-level revise
planner sees section titles only; that is why it could not do this."""
import uuid

import pytest
from sqlalchemy import text

from app.curriculum import blueprint as bp_mod
from app.curriculum import lesson_ai
from app.curriculum import tutor_edit
from app.db import Base, SessionLocal, engine
from app.models.block import Block

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


def setup_module(_):
    Base.metadata.create_all(engine)


@pytest.fixture
def lesson():
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", language="el", is_template=True,
                   meta={"brief": "Θέλω ένα course για guitar tone", "source_ids": None,
                         "blueprint": bp_mod.default_blueprint(),
                         "shape": {"minutes_per_lesson": 50}})
    db.add(course); db.flush()
    module = Block(kind="module", title="Η Κιθάρα ως Πηγή", parent_id=course.id, order=0,
                   language="el", meta={"objective": "το όργανο"})
    db.add(module); db.flush()
    prev = Block(kind="lesson", title="Τα ξύλα", parent_id=module.id, order=0, language="el",
                 meta={"objective": "ξύλα", "draft_status": "ready"})
    lesson = Block(kind="lesson", title="Τα εμβληματικά μοντέλα", parent_id=module.id, order=1,
                   language="el", meta={"objective": "Strat, Tele, LP, SG", "draft_status": "ready",
                                        "floor_words": 2200, "target_words": 2750})
    nxt = Block(kind="lesson", title="Μαγνήτες", parent_id=module.id, order=2, language="el",
                meta={"objective": "pickups", "draft_status": "ready"})
    db.add_all([prev, lesson, nxt]); db.flush()
    keys = ["warm_up", "theory", "demonstration", "exercises", "common_mistakes", "recap", "homework", "qa_prompts"]
    for i, k in enumerate(keys):
        db.add(Block(kind="segment", title=k, body=f"κείμενο {k}", order=i, parent_id=lesson.id,
                     language="el", meta={"section": k}))
    db.flush()
    theory = db.scalars(__import__("sqlalchemy").select(Block).where(
        Block.parent_id == lesson.id, Block.meta["section"].as_string() == "theory")).one()
    tutor_edit.mark_tutor_edit(theory, previous_body=theory.body)
    theory.body = "Η Strat έχει τρεις μαγνήτες single-coil και tremolo."
    db.commit()
    yield db, course, module, lesson
    db.close()


def test_plan_messages_carry_full_bodies_before_after_and_neighbours(lesson):
    db, course, module, les = lesson
    captured = {}

    class P:
        def guided_json(self, messages, schema, role):
            captured["messages"] = messages; captured["schema"] = schema; captured["role"] = role
            return {"summary": "ok", "note_to_tutor": "",
                    "sections": [{"section": k, "action": "rewrite" if k != "theory" else "keep",
                                  "reason": "r", "brief": "b"} for k in
                                 ["warm_up", "theory", "demonstration", "exercises",
                                  "common_mistakes", "recap", "homework", "qa_prompts"]]}
    import app.curriculum.lesson_ai as mod
    mod_get = mod.get_provider
    mod.get_provider = lambda: P()
    mod_ground = mod.ground_topic
    mod.ground_topic = lambda *a, **k: []
    try:
        plan = lesson_ai.plan_lesson_change(db, les.id, instruction="Ενημέρωσε τις υπόλοιπες", note=None)
    finally:
        mod.get_provider = mod_get; mod.ground_topic = mod_ground
    text_ = captured["messages"][-1]["content"]
    assert "Η Strat έχει τρεις μαγνήτες" in text_          # current theory, whole
    assert "κείμενο theory" in text_                       # the before
    assert "Τα ξύλα" in text_ and "Μαγνήτες" in text_       # neighbours
    assert "κείμενο exercises" in text_                    # every section's body
    assert captured["role"] == "plan"
    assert text_.rstrip().endswith(captured["messages"][-1]["content"].rstrip())  # instruction last: see below
    assert "Ενημέρωσε τις υπόλοιπες" in text_[-600:]
    assert plan["impact"]["rewrite_count"] == 7
    theory_row = next(s for s in plan["sections"] if s["section"] == "theory")
    assert theory_row["tutor_edited"] is True and theory_row["action"] == "keep"


def test_validate_plan_fills_gaps_and_drops_unknown_keys():
    existing = [{"section": "theory", "title": "Θεωρία"}, {"section": "recap", "title": "Ανακ."}]
    raw = {"summary": "s", "note_to_tutor": "",
           "sections": [{"section": "theory", "action": "rewrite", "reason": "r", "brief": "b"},
                        {"section": "ghost", "action": "rewrite", "reason": "r", "brief": "b"}]}
    out = lesson_ai.validate_plan(raw, existing)
    assert [s["section"] for s in out["sections"]] == ["theory", "recap"]
    assert out["sections"][1]["action"] == "keep"
    assert out["dropped"] and "ghost" in out["dropped"][0]["reason"]


def test_plan_on_a_non_lesson_raises(lesson):
    db, course, module, les = lesson
    with pytest.raises(lesson_ai.LessonAiError):
        lesson_ai.plan_lesson_change(db, module.id, instruction="x", note=None)
```

- [ ] **Step 2: Run** — `cd apps/api && pytest tests/test_lesson_ai.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# apps/api/app/curriculum/lesson_ai.py
"""The lesson panel («AI στο μάθημα»): plan a change to ONE lesson, section by
section, from the WHOLE lesson.

Why this exists next to `revise.py`: the course-level planner sees section
titles only (its docstring: "NO LESSON OR SEGMENT BODIES"), so "I changed the
theory — update the rest" is structurally impossible there. This planner is
scoped to one lesson and is handed everything: every section's full text, the
tutor's before/after on the sections he edited, the blueprint, the neighbours,
and library passages. It returns one verdict per section (rewrite/keep + why +
a one-line brief). Apply (`apply_lesson_change`, Task 2.2) then makes ONE draft
call with the schema restricted to the ticked sections and the rest shown as
fixed text (`draft.LESSON_FIXED_BLOCK`).
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select

from app.curriculum import blueprint as bp_mod
from app.curriculum.ground import ground_topic
from app.curriculum.tutor_edit import tutor_edited_sections
from app.i18n import answer_in, curriculum_style, language_directive
from app.llm.factory import get_provider
from app.models.block import Block
from app.prompts.overrides import resolve

log = logging.getLogger(__name__)

SECTION_CHAR_LIMIT = 20_000
PLAN_RETRIEVAL_K = 6


class LessonAiError(ValueError):
    """A request the lesson cannot accept (not a lesson, no course). 4xx / failed job."""


LESSON_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "2-3 sentences, to the tutor, in his language: what you will change and why."},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "the section key exactly as listed"},
                    "action": {"type": "string", "enum": ["rewrite", "keep"]},
                    "reason": {"type": "string", "description": "one sentence: why rewrite, or why it can stay"},
                    "brief": {"type": "string",
                              "description": "if rewrite: one line saying WHAT must change in this section; empty if keep"},
                },
                "required": ["section", "action", "reason", "brief"],
                "additionalProperties": False,
            },
        },
        "note_to_tutor": {"type": "string",
                          "description": "anything he should decide himself (a contradiction, a missing fact); empty if none"},
    },
    "required": ["summary", "sections", "note_to_tutor"],
    "additionalProperties": False,
}

LESSON_AI_PLAN_SYSTEM = (
    "You are the tutor's co-author on ONE lesson of his guitar course. You will "
    "be shown the whole lesson, section by section, and — where he edited a "
    "section by hand — both his version and the version the AI wrote before. His "
    "hand-edited text is the truth of this lesson now. Your job is to decide, for "
    "EVERY section, whether it must be rewritten to fit what he asks and what he "
    "changed, or can stay exactly as it is. Be surgical: a section stays unless "
    "it now contradicts, repeats, or fails to build on the changed material. "
    "Never propose rewriting a section he edited by hand unless he explicitly "
    "asks for it.\n\n{language_directive}\n\n{style_directive}"
)
LESSON_AI_PLAN_SLICE_ID = "lesson.ai.plan"

LESSON_AI_PLAN_USER = (
    "COURSE: {course_title}{course_brief_block}\n"
    "MODULE: {module_title} — {module_objective}\n"
    "{neighbours}\n"
    "LESSON: {lesson_title} — {lesson_objective}\n"
    "\nTHE LESSON'S SECTIONS (blueprint order; key — label — weight):\n{blueprint_lines}\n"
    "\nCURRENT TEXT OF EVERY SECTION:\n{sections}\n"
    "{edited_block}"
    "{retrieved_block}"
    "\nWHAT THE TUTOR ASKS:\n{instruction}{note_block}\n"
    "\nReturn one entry per section key listed above — no more, no fewer.\n"
    "\n{answer_in}"
)
LESSON_AI_PLAN_USER_SLICE_ID = "lesson.ai.plan.user"
LESSON_AI_EDITED_BLOCK = (
    "\nSECTIONS THE TUTOR EDITED BY HAND — the current text above is HIS; this is "
    "what the AI had written before, so you can see exactly what he changed:\n{edited}\n"
)
LESSON_AI_RETRIEVED_BLOCK = "\nFROM HIS LIBRARY (for grounding the rewrites):\n{retrieved}\n"
LESSON_AI_NOTE_BLOCK = "\n\nHIS EXTRA NOTE: {note}"
LESSON_AI_COURSE_BRIEF_BLOCK = "\nWHAT THE TUTOR WANTS FROM THIS COURSE: {course_brief}"


def _cap(text: str | None) -> str:
    t = (text or "").strip()
    if len(t) <= SECTION_CHAR_LIMIT:
        return t
    return t[:SECTION_CHAR_LIMIT] + "\n…[περικόπηκε]"


def build_plan_messages(
    *, course_title: str, course_brief: str | None, module_title: str, module_objective: str,
    neighbours: str, lesson_title: str, lesson_objective: str, blueprint_lines: str,
    sections: list[dict], edited: dict[str, dict], retrieved: str | None,
    instruction: str, note: str | None, language: str, source=None,
) -> list[dict]:
    """Pure. `sections` = [{section, title, body}], `edited` = {section: {prev_body}}."""
    system = resolve(source, LESSON_AI_PLAN_SLICE_ID, LESSON_AI_PLAN_SYSTEM).format(
        language_directive=language_directive(language, source),
        style_directive=curriculum_style(language, source),
    )
    sections_text = json.dumps(
        [{"section": s["section"], "title": s["title"], "body": _cap(s["body"])} for s in sections],
        ensure_ascii=False, indent=1,
    )
    edited_block = ""
    if edited:
        edited_block = LESSON_AI_EDITED_BLOCK.format(edited=json.dumps(
            {k: {"before": _cap(v.get("prev_body"))} for k, v in edited.items()},
            ensure_ascii=False, indent=1,
        ))
    user = resolve(source, LESSON_AI_PLAN_USER_SLICE_ID, LESSON_AI_PLAN_USER).format(
        course_title=course_title,
        course_brief_block=(LESSON_AI_COURSE_BRIEF_BLOCK.format(course_brief=course_brief) if course_brief else ""),
        module_title=module_title, module_objective=module_objective or "",
        neighbours=neighbours, lesson_title=lesson_title, lesson_objective=lesson_objective or "",
        blueprint_lines=blueprint_lines, sections=sections_text, edited_block=edited_block,
        retrieved_block=(LESSON_AI_RETRIEVED_BLOCK.format(retrieved=retrieved) if retrieved else ""),
        instruction=instruction.strip(),
        note_block=(LESSON_AI_NOTE_BLOCK.format(note=note.strip()) if note and note.strip() else ""),
        answer_in=answer_in(language, source),
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def neighbours_text(db, lesson: Block) -> str:
    """«ΠΡΟΗΓΟΥΜΕΝΟ / ΕΠΟΜΕΝΟ ΜΑΘΗΜΑ» + the module's other lesson titles. Shared
    with `jobs/curriculum_draft.py`'s neighbours block (Task 3.2)."""
    siblings = db.scalars(
        select(Block).where(Block.parent_id == lesson.parent_id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()
    idx = next((i for i, s in enumerate(siblings) if s.id == lesson.id), None)
    def line(b: Block | None) -> str:
        if b is None:
            return "—"
        return f"{b.title} — {(b.meta or {}).get('objective') or ''}".rstrip(" —")
    prev = siblings[idx - 1] if idx not in (None, 0) else None
    nxt = siblings[idx + 1] if idx is not None and idx + 1 < len(siblings) else None
    others = ", ".join(s.title for s in siblings if s.id != lesson.id) or "—"
    return (f"PREVIOUS LESSON: {line(prev)}\nNEXT LESSON: {line(nxt)}\n"
            f"OTHER LESSONS IN THIS MODULE: {others}")


def _lesson_sections(db, lesson: Block) -> list[dict]:
    segs = db.scalars(
        select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment").order_by(Block.order)
    ).all()
    return [{"section": (s.meta or {}).get("section") or s.title, "title": s.title,
             "body": s.body or "", "tutor_edited": bool((s.meta or {}).get("tutor_edited")),
             "id": str(s.id)} for s in segs]


def _blueprint_lines(bp: dict, language: str) -> str:
    labels = bp_mod.section_labels(bp, language if language in ("el", "en") else "el")
    return "\n".join(f"- {s['key']} — {labels.get(s['key'], s['key'])} — {s.get('weight', 0):.2f}"
                     for s in bp_mod.enabled_sections(bp))


def validate_plan(raw: dict, existing: list[dict]) -> dict:
    """Every existing section exactly once; unknown keys dropped with a reason;
    missing ones default to keep. Same honesty contract as `revise.validate_ops`."""
    known = {s["section"]: s for s in existing}
    seen: set[str] = set()
    out: list[dict] = []
    dropped: list[dict] = []
    for item in raw.get("sections") or []:
        key = str(item.get("section") or "")
        if key not in known:
            dropped.append({"section": key, "reason": f"unknown section key {key!r}"})
            continue
        if key in seen:
            dropped.append({"section": key, "reason": f"duplicate entry for {key!r}"})
            continue
        seen.add(key)
        action = "rewrite" if item.get("action") == "rewrite" else "keep"
        out.append({"section": key, "title": known[key]["title"], "action": action,
                    "reason": str(item.get("reason") or ""), "brief": str(item.get("brief") or ""),
                    "tutor_edited": bool(known[key].get("tutor_edited"))})
    for s in existing:
        if s["section"] not in seen:
            out.append({"section": s["section"], "title": s["title"], "action": "keep",
                        "reason": "", "brief": "", "tutor_edited": bool(s.get("tutor_edited"))})
    order = {s["section"]: i for i, s in enumerate(existing)}
    out.sort(key=lambda s: order[s["section"]])
    return {"summary": str(raw.get("summary") or ""), "sections": out,
            "note_to_tutor": str(raw.get("note_to_tutor") or ""), "dropped": dropped}


def _impact(plan: dict, sections: list[dict], bp: dict, target_words: int) -> dict:
    from app.curriculum.depth import count_words
    weights = bp_mod.section_weights(bp)
    rewrite = [s["section"] for s in plan["sections"] if s["action"] == "rewrite"]
    est = 0
    for key in rewrite:
        w = weights.get(key)
        est += int(w * target_words) if w else count_words(next((x["body"] for x in sections if x["section"] == key), ""))
    return {"rewrite_count": len(rewrite), "est_words": est}


def _lesson_or_raise(db, lesson_id: uuid.UUID) -> tuple[Block, Block, Block]:
    lesson = db.get(Block, lesson_id)
    if lesson is None or lesson.kind != "lesson":
        raise LessonAiError(f"block {lesson_id} is not a lesson")
    module = db.get(Block, lesson.parent_id)
    course = db.get(Block, module.parent_id) if module is not None else None
    if module is None or course is None or course.kind != "course":
        raise LessonAiError(f"lesson {lesson_id} is not inside a course")
    return lesson, module, course


def plan_lesson_change(db, lesson_id: uuid.UUID, *, instruction: str, note: str | None) -> dict:
    lesson, module, course = _lesson_or_raise(db, lesson_id)
    meta = course.meta or {}
    bp = bp_mod.blueprint_from_course_meta(meta)
    language = lesson.language or course.language or "el"
    sections = _lesson_sections(db, lesson)
    edited = tutor_edited_sections(db, lesson)
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    retrieved = None
    try:
        passages = ground_topic(db, f"{lesson.title} {instruction}", source_ids=source_ids, k=PLAN_RETRIEVAL_K)
        retrieved = "\n\n".join(f"[{p.source_title}, p.{p.page_no}] {p.text}" for p in passages) or None
    except Exception:
        log.warning("lesson_ai: retrieval failed — planning without passages", exc_info=True)
    messages = build_plan_messages(
        course_title=course.title, course_brief=meta.get("brief"),
        module_title=module.title, module_objective=(module.meta or {}).get("objective") or "",
        neighbours=neighbours_text(db, lesson), lesson_title=lesson.title,
        lesson_objective=(lesson.meta or {}).get("objective") or lesson.body or "",
        blueprint_lines=_blueprint_lines(bp, language), sections=sections, edited=edited,
        retrieved=retrieved, instruction=instruction, note=note, language=language, source=db,
    )
    raw = get_provider().guided_json(messages, LESSON_PLAN_SCHEMA, role="plan")
    plan = validate_plan(raw, sections)
    target = int((lesson.meta or {}).get("target_words") or
                 ((meta.get("shape") or {}).get("minutes_per_lesson", 50) * 55))
    plan["impact"] = _impact(plan, sections, bp, target)
    return plan
```

Check `ground_topic`'s signature and the passage attribute names (`source_title`, `page_no`, `text`) against `app/curriculum/ground.py` and `segment_generate.py:214-220`; adapt.

- [ ] **Step 4: Register prompts**

Two `PromptEntry`s in `registry.py` (`flow="lesson"`, `language_from_course=True`): `lesson.ai.plan` (`kind="prompt"`, `source_ref="app/curriculum/lesson_ai.py:<LESSON_AI_PLAN_SYSTEM line>"`, `call_sites=("curriculum/lesson_ai.py:<line of get_provider().guided_json>",)`, slices: system + user) and no separate entry for the user slice (it is a slice of the same prompt — follow how `segment.generate` registers `segment.generate` + `segment.generate.user` as two slices of ONE entry: `grep -n "segment.generate" apps/api/app/prompts/registry.py`). `build` renders `build_plan_messages` with a two-section sample lesson (one tutor-edited). Greek `title_el="Το πλάνο του AI για ένα μάθημα"`, `what_it_does_el`, `when_it_runs_el` («Όταν πατάς «Φτιάξε πλάνο» στο πλαίσιο AI ενός μαθήματος»).

- [ ] **Step 5: Run** — `pytest tests/test_lesson_ai.py tests/test_prompts_registry.py -q`; `python -m tests.prompt_baseline`; diff shows only the new entry; `pytest tests/test_prompts_byte_identity.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/curriculum/lesson_ai.py apps/api/app/prompts/registry.py apps/api/tests/test_lesson_ai.py apps/api/tests/fixtures/prompt_renders_baseline.json
git commit -m "feat(lesson-ai): a planner that sees the whole lesson and the tutor's before/after"
```

### Task 2.2: `apply_lesson_change` — snapshot, one partial draft, in-place persist

**Files:**
- Modify: `apps/api/app/curriculum/lesson_ai.py`, `apps/api/app/curriculum/draft.py` (`LESSON_SECTION_BRIEFS_BLOCK`, `build_lesson_messages(section_briefs=...)`, `draft_lesson(section_briefs=...)`), `apps/api/app/prompts/registry.py` (`lesson.section_briefs` fragment)
- Test: `apps/api/tests/test_lesson_ai.py` (extend)

**Interfaces:**
- Produces: `lesson_ai.apply_lesson_change(db, lesson_id, *, instruction: str, note: str | None, sections: list[dict]) -> dict` where `sections = [{"section": key, "brief": str}]` (the ticked ones). Returns `{"rewritten": [keys], "word_count": int}`. Mutates + commits. Raises `LessonAiError` on bad keys/empty list; lets `LLMError` propagate after restoring `draft_status="ready"`.
- `draft.LESSON_SECTION_BRIEFS_BLOCK`, slice `lesson.section_briefs`; `build_lesson_messages(..., section_briefs: dict[str, str] | None = None)` rendered right after the fixed block.

- [ ] **Step 1: Failing tests** (append to `test_lesson_ai.py`)

```python
def test_apply_rewrites_only_ticked_sections_keeps_ids_and_snapshots(lesson, monkeypatch):
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod
    captured = {}

    def fake_draft_lesson(db_, *, ctx, library, language, blueprint=None, student_brief=None,
                          course_brief=None, source_ids=None, prompts=None, revise_current=None,
                          fixed_sections=None, exclude_sections=frozenset(), section_briefs=None):
        captured.update(fixed=fixed_sections, exclude=set(exclude_sections), briefs=section_briefs,
                        objective=ctx.lesson_objective)
        from app.curriculum.depth import Measurement
        drafted = {"title": les.title, "summary": "νέα περίληψη",
                   "warm_up": {"body": "νέο ζέσταμα", "citations": []},
                   "exercises": {"body": "νέες ασκήσεις", "items": [], "citations": []}}
        return drafted, Measurement(total_words=50, target=2750, floor=2200, per_section={}, thin_sections=[])
    monkeypatch.setattr(mod, "draft_lesson", fake_draft_lesson)

    from sqlalchemy import select
    before_ids = {(s.meta or {}).get("section"): s.id for s in
                  db.scalars(select(Block).where(Block.parent_id == les.id)).all()}
    out = lesson_ai.apply_lesson_change(
        db, les.id, instruction="Ενημέρωσε", note="πιο απλά",
        sections=[{"section": "warm_up", "brief": "αναφορά στη Strat"},
                  {"section": "exercises", "brief": "ασκήσεις με tremolo"}])
    db.expire_all()
    assert captured["exclude"] == {"theory", "demonstration", "common_mistakes", "recap", "homework", "qa_prompts"}
    assert "Η Strat έχει τρεις μαγνήτες" in captured["fixed"]["theory"]
    assert captured["briefs"] == {"warm_up": "αναφορά στη Strat", "exercises": "ασκήσεις με tremolo"}
    assert "Ενημέρωσε" in captured["objective"] and "πιο απλά" in captured["objective"]
    rows = {(s.meta or {}).get("section"): s for s in
            db.scalars(select(Block).where(Block.parent_id == les.id)).all()}
    assert rows["warm_up"].body == "νέο ζέσταμα" and rows["warm_up"].id == before_ids["warm_up"]
    assert rows["theory"].body.startswith("Η Strat") and "tutor_edited" in rows["theory"].meta
    assert rows["recap"].body == "κείμενο recap"
    l = db.get(Block, les.id)
    assert l.meta["draft_status"] == "ready"
    assert [s["section"] for s in l.meta["prev_segments"]][1] == "theory"     # snapshot taken
    assert l.meta["revise_instruction"].startswith("Ενημέρωσε")
    assert out["rewritten"] == ["warm_up", "exercises"]


def test_apply_with_unknown_or_empty_sections_raises(lesson):
    db, course, module, les = lesson
    with pytest.raises(lesson_ai.LessonAiError):
        lesson_ai.apply_lesson_change(db, les.id, instruction="x", note=None, sections=[])
    with pytest.raises(lesson_ai.LessonAiError):
        lesson_ai.apply_lesson_change(db, les.id, instruction="x", note=None,
                                      sections=[{"section": "ghost", "brief": ""}])


def test_apply_restores_ready_when_the_model_fails(lesson, monkeypatch):
    from app.llm.errors import LLMError
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod
    def boom(*a, **k):
        raise LLMError("upstream", "no")
    monkeypatch.setattr(mod, "draft_lesson", boom)
    with pytest.raises(LLMError):
        lesson_ai.apply_lesson_change(db, les.id, instruction="x", note=None,
                                      sections=[{"section": "recap", "brief": ""}])
    db.expire_all()
    assert db.get(Block, les.id).meta["draft_status"] == "ready"
```

- [ ] **Step 2: Run** → FAIL (`apply_lesson_change` missing).

- [ ] **Step 3: Implement the briefs block in `draft.py`**

```python
LESSON_SECTION_BRIEFS_BLOCK = (
    "\n\nΓια κάθε ενότητα που ξαναγράφεις, αυτό ακριβώς πρέπει να αλλάξει:\n{briefs}"
)
LESSON_SECTION_BRIEFS_SLICE_ID = "lesson.section_briefs"
```
`build_lesson_messages(..., section_briefs: dict[str, str] | None = None)`:
```python
    briefs_block = ""
    if section_briefs:
        import json
        briefs_block = resolve(source, LESSON_SECTION_BRIEFS_SLICE_ID, LESSON_SECTION_BRIEFS_BLOCK).format(
            briefs=json.dumps({k: v for k, v in section_briefs.items() if v}, ensure_ascii=False))
```
appended to the same `revise_block=revise_block + fixed_block + briefs_block`. `draft_lesson(..., section_briefs=None)` threads it to every `build_lesson_messages` call. Register a `lesson.section_briefs` fragment entry like `lesson.fixed`.

- [ ] **Step 4: Implement `apply_lesson_change`** (in `lesson_ai.py`)

```python
from app.curriculum.corpus import build_retrieval_context
from app.curriculum.draft import LessonContext, draft_lesson, persist_lesson
from app.curriculum.restore import snapshot_of
from app.curriculum.tutor_edit import recompute_lesson_words
from app.jobs.curriculum_draft import _lesson_size  # sizing rule shared with the fan-out
from app.prompts import overrides
from app.curriculum.outline import TIER_GENERAL


def apply_lesson_change(db, lesson_id: uuid.UUID, *, instruction: str, note: str | None,
                        sections: list[dict]) -> dict:
    """ONE draft call with the schema restricted to `sections`; everything else
    is fixed text. Snapshot first (`prev_segments` — the same lesson-level undo
    the revise engine uses), write in place, recount. Commits. On a model error
    the lesson goes back to `ready` untouched and the error propagates."""
    lesson, module, course = _lesson_or_raise(db, lesson_id)
    existing = _lesson_sections(db, lesson)
    known = {s["section"] for s in existing}
    ticked = [str(s.get("section") or "") for s in sections]
    briefs = {str(s.get("section")): str(s.get("brief") or "") for s in sections}
    if not ticked:
        raise LessonAiError("no sections selected")
    unknown = [k for k in ticked if k not in known]
    if unknown:
        raise LessonAiError(f"unknown sections: {unknown}")
    keep = known - set(ticked)
    fixed = {s["section"]: s["body"] for s in existing if s["section"] in keep and s["body"].strip()}

    meta = course.meta or {}
    bp = bp_mod.blueprint_from_course_meta(meta)
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    shape = meta.get("shape") or {}
    plan = {"minutes_per_lesson": shape.get("minutes_per_lesson", 50),
            "teaching_minutes": shape.get("minutes_per_lesson", 50)}
    size = _lesson_size(lesson, {**plan,
                                 "target_words": (lesson.meta or {}).get("target_words") or shape.get("target_words_per_lesson", 2750),
                                 "floor_words": (lesson.meta or {}).get("floor_words") or 2200},
                        deepen=False)
    full_instruction = instruction.strip() + (f"\n{note.strip()}" if note and note.strip() else "")
    objective = ((lesson.meta or {}).get("objective") or lesson.body or "")
    objective = f"{objective}\n\nΑναθεώρηση από τον καθηγητή: {full_instruction}"
    ctx = LessonContext(
        lesson_title=lesson.title, lesson_objective=objective,
        module_title=module.title, module_objective=(module.meta or {}).get("objective") or "",
        course_title=course.title, tier=(module.meta or {}).get("tier") or TIER_GENERAL,
        position=neighbours_text(db, lesson),
        minutes=size["minutes"], teaching_minutes=size["teaching_minutes"],
        target_words=size["target_words"], floor_words=size["floor_words"],
    )
    # Snapshot + claim, one transaction.
    lesson.meta = {**(lesson.meta or {}), "prev_segments": snapshot_of(db, lesson),
                   "revise_instruction": full_instruction, "draft_status": "drafting", "error": None}
    db.commit()

    library = build_retrieval_context(db, source_ids)
    prompts = overrides.snapshot(db)
    language = lesson.language or course.language or "el"
    try:
        drafted, m = draft_lesson(
            db, ctx=ctx, library=library, language=language, blueprint=bp,
            student_brief=None, course_brief=meta.get("brief"), source_ids=source_ids,
            prompts=prompts, revise_current=None, fixed_sections=fixed or None,
            exclude_sections=keep, section_briefs=briefs,
        )
    except Exception:
        db.rollback()
        lesson = db.get(Block, lesson_id)
        lesson.meta = {**(lesson.meta or {}), "draft_status": "ready"}
        db.commit()
        raise
    lesson = db.get(Block, lesson_id)
    persist_lesson(db, lesson, drafted, m, library, bp, teaching_minutes=size["teaching_minutes"], keep=keep)
    words = recompute_lesson_words(db, lesson)
    db.commit()
    return {"rewritten": [k for k in ticked], "word_count": words}
```

`_lesson_size` reads `plan["target_words"]`/`plan["floor_words"]`/`plan["minutes_per_lesson"]` — read its body (`curriculum_draft.py:225-252`) and pass exactly the keys it uses. `position` is repurposed to carry the neighbours text on this path (the `POSITION:` line then reads «PREVIOUS LESSON: …»; acceptable and honest).

- [ ] **Step 5: Run** — `pytest tests/test_lesson_ai.py tests/test_lesson_draft.py tests/test_prompts_registry.py -q`; baseline regen for the new fragment; `pytest tests/test_prompts_byte_identity.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/curriculum/lesson_ai.py apps/api/app/curriculum/draft.py apps/api/app/prompts/registry.py apps/api/tests
git commit -m "feat(lesson-ai): apply = snapshot, one partial draft around the fixed sections, in-place persist"
```

### Task 2.3: `lesson_ai` job + the two routes

**Files:**
- Create: `apps/api/app/jobs/lesson_ai.py`
- Modify: `apps/api/app/routers/curriculum.py` (two routes; module-level import of the runner), `apps/api/app/schemas/curriculum.py` (`LessonAiPlanRequest`, `LessonAiApplyRequest`)
- Test: `apps/api/tests/test_lesson_ai_api.py` (new)

**Interfaces:**
- Produces: `POST /blocks/{lesson_id}/ai/plan` `{instruction, note?}` → 202 `JobAccepted` (kind `lesson_ai`, params `{lesson_id, mode:"plan", instruction, note}`); `POST /blocks/{lesson_id}/ai/apply` `{instruction, note?, sections:[{section, brief}]}` → 202 (params `{..., mode:"apply", sections}`); 404 non-lesson; 409 `lesson_busy` (`{"code":"lesson_busy"}`) when a `lesson_ai` job for this lesson is pending/running or `draft_status == "drafting"`; 422 empty/unknown sections (apply). Job progress: plan → `{"phase":"done","plan":{...}}`; apply → `{"phase":"generating"}` → `{"phase":"done","rewritten":[...],"word_count":n}`; `result_root_id` = course id. `run_lesson_ai_job(job_id)`.

- [ ] **Step 1: Failing tests**

```python
# apps/api/tests/test_lesson_ai_api.py
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.generation_job import GenerationJob
import app.routers.curriculum as cur_router
import app.jobs.lesson_ai as job_mod

client = TestClient(app)
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)

def setup_module(_):
    Base.metadata.create_all(engine)

@pytest.fixture
def lesson_id():
    db = SessionLocal()
    course = Block(kind="course", title="Ή", language="el", is_template=True, meta={"shape": {"minutes_per_lesson": 50}})
    db.add(course); db.flush()
    module = Block(kind="module", title="Μ", parent_id=course.id, order=0, language="el", meta={})
    db.add(module); db.flush()
    lesson = Block(kind="lesson", title="Λ", parent_id=module.id, order=0, language="el", meta={"draft_status": "ready"})
    db.add(lesson); db.flush()
    db.add(Block(kind="segment", title="Θ", body="κείμενο", order=0, parent_id=lesson.id, language="el", meta={"section": "theory"}))
    db.commit()
    lid, mid = lesson.id, module.id
    db.close()
    return lid, mid

def test_plan_route_enqueues_and_the_job_stores_the_plan(lesson_id, monkeypatch):
    lid, _ = lesson_id
    monkeypatch.setattr(job_mod, "plan_lesson_change",
                        lambda db, l, *, instruction, note: {"summary": "s", "sections": [], "note_to_tutor": "", "dropped": [], "impact": {"rewrite_count": 0, "est_words": 0}})
    r = client.post(f"/blocks/{lid}/ai/plan", json={"instruction": "κάνε το καλύτερο"})
    assert r.status_code == 202
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["kind"] == "lesson_ai" and job["status"] == "succeeded"
    assert job["progress"]["phase"] == "done" and job["progress"]["plan"]["summary"] == "s"

def test_apply_route_validates_sections_then_runs(lesson_id, monkeypatch):
    lid, _ = lesson_id
    assert client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": []}).status_code == 422
    assert client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": [{"section": "ghost", "brief": ""}]}).status_code == 422
    monkeypatch.setattr(job_mod, "apply_lesson_change",
                        lambda db, l, *, instruction, note, sections: {"rewritten": ["theory"], "word_count": 3})
    r = client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": [{"section": "theory", "brief": "b"}]})
    assert r.status_code == 202
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "succeeded" and job["progress"]["rewritten"] == ["theory"]

def test_plan_on_a_module_is_404_and_busy_is_409(lesson_id, monkeypatch):
    lid, mid = lesson_id
    assert client.post(f"/blocks/{mid}/ai/plan", json={"instruction": "x"}).status_code == 404
    monkeypatch.setattr(cur_router, "run_lesson_ai_job", lambda job_id: None)   # stays pending
    assert client.post(f"/blocks/{lid}/ai/plan", json={"instruction": "x"}).status_code == 202
    r = client.post(f"/blocks/{lid}/ai/plan", json={"instruction": "y"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "lesson_busy"

def test_llm_failure_in_apply_is_a_failed_job_with_kind(lesson_id, monkeypatch):
    from app.llm.errors import LLMError
    lid, _ = lesson_id
    def boom(*a, **k):
        raise LLMError("rate_limit", "429")
    monkeypatch.setattr(job_mod, "apply_lesson_change", boom)
    r = client.post(f"/blocks/{lid}/ai/apply", json={"instruction": "x", "sections": [{"section": "theory", "brief": ""}]})
    job = client.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "failed" and job["error_kind"] == "rate_limit"
```

- [ ] **Step 2: Run** → FAIL (404 on the routes).

- [ ] **Step 3: Implement the job**

```python
# apps/api/app/jobs/lesson_ai.py
"""The lesson panel's two operations as ONE job kind, `mode` on params —
the `jobs/curriculum_revise.py` plan-vs-apply shape."""
from __future__ import annotations
import logging, uuid
from app.curriculum.lesson_ai import LessonAiError, apply_lesson_change, plan_lesson_change
from app.db import SessionLocal
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.block import Block
from app.models.generation_job import GenerationJob

log = logging.getLogger(__name__)


def _root_of(db, lesson_id: uuid.UUID) -> uuid.UUID | None:
    lesson = db.get(Block, lesson_id)
    module = db.get(Block, lesson.parent_id) if lesson and lesson.parent_id else None
    return module.parent_id if module else None


def run_lesson_ai_job(job_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        mode = job.params.get("mode") or "plan"
        lesson_id = uuid.UUID(str(job.params["lesson_id"]))
        job.status = "running"
        job.progress = {"phase": "planning" if mode == "plan" else "generating"}
        job.result_root_id = _root_of(db, lesson_id)
        db.commit()

        if mode == "plan":
            plan = plan_lesson_change(db, lesson_id, instruction=str(job.params.get("instruction") or ""),
                                      note=job.params.get("note"))
            job = db.get(GenerationJob, job_id)
            job.status = "succeeded"
            job.progress = {"phase": "done", "plan": plan}
            db.commit()
            return

        out = apply_lesson_change(db, lesson_id, instruction=str(job.params.get("instruction") or ""),
                                  note=job.params.get("note"), sections=list(job.params.get("sections") or []))
        job = db.get(GenerationJob, job_id)
        job.status = "succeeded"
        job.progress = {"phase": "done", **out}
        db.commit()
    except LessonAiError as e:
        _fail(db, job_id, "internal", str(e))
    except LLMNotConfigured:
        _fail(db, job_id, "auth", "No API key is configured. Open Settings, add your key, then try again.")
    except LLMError as e:
        _fail(db, job_id, e.kind, str(e) or e.kind)
    except Exception:
        log.exception("run_lesson_ai_job: job %s failed", job_id)
        _fail(db, job_id, "internal", "The lesson could not be processed. Try again.")
    finally:
        db.close()


def _fail(db, job_id, kind, message):
    try:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.status = "failed"; job.error_kind = kind; job.error = message
        db.commit()
    except Exception:
        log.warning("could not record failure for job %s", job_id, exc_info=True)
```

- [ ] **Step 4: Schemas + routes**

`schemas/curriculum.py`:
```python
class LessonAiPlanRequest(BaseModel):
    instruction: str = Field(min_length=1)
    note: str | None = None

class LessonAiSectionPick(BaseModel):
    section: str = Field(min_length=1)
    brief: str = ""

class LessonAiApplyRequest(LessonAiPlanRequest):
    sections: list[LessonAiSectionPick] = Field(min_length=1)
```

`routers/curriculum.py` (import `run_lesson_ai_job` at module level next to the other runners):
```python
def _lesson_busy(db: Session, lesson_id: UUID) -> bool:
    lesson = db.get(Block, lesson_id)
    if (lesson.meta or {}).get("draft_status") == "drafting":
        return True
    return db.scalar(select(GenerationJob.id).where(
        GenerationJob.kind == "lesson_ai",
        GenerationJob.status.in_(("pending", "running")),
        GenerationJob.params["lesson_id"].as_string() == str(lesson_id),
    ).limit(1)) is not None


def _enqueue_lesson_ai(db, background_tasks, lesson_id: UUID, params: dict) -> JobAccepted:
    lesson = _get_block_or_404(db, lesson_id)
    if lesson.kind != "lesson":
        raise HTTPException(status_code=404, detail="not a lesson")
    if _lesson_busy(db, lesson_id):
        raise HTTPException(status_code=409, detail={"code": "lesson_busy",
                            "message": "this lesson is already being worked on"})
    job = GenerationJob(kind="lesson_ai", status="pending", params={"lesson_id": str(lesson_id), **params})
    db.add(job); db.commit(); db.refresh(job)
    background_tasks.add_task(run_lesson_ai_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)


@router.post("/blocks/{lesson_id}/ai/plan", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def lesson_ai_plan(lesson_id: UUID, payload: LessonAiPlanRequest, background_tasks: BackgroundTasks,
                   db: Session = Depends(get_db)) -> JobAccepted:
    """«AI στο μάθημα» → «Φτιάξε πλάνο». Read-only; the plan lands on `progress.plan`."""
    return _enqueue_lesson_ai(db, background_tasks, lesson_id,
                              {"mode": "plan", "instruction": payload.instruction, "note": payload.note})


@router.post("/blocks/{lesson_id}/ai/apply", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def lesson_ai_apply(lesson_id: UUID, payload: LessonAiApplyRequest, background_tasks: BackgroundTasks,
                    db: Session = Depends(get_db)) -> JobAccepted:
    """«Εφαρμογή» — the ticked sections are rewritten around the rest. 422 for a
    section key the lesson does not have, so a stale card fails before a job row exists."""
    lesson = _get_block_or_404(db, lesson_id)
    if lesson.kind != "lesson":
        raise HTTPException(status_code=404, detail="not a lesson")
    known = {(s.meta or {}).get("section") or s.title for s in db.scalars(
        select(Block).where(Block.parent_id == lesson_id, Block.kind == "segment")).all()}
    bad = [p.section for p in payload.sections if p.section not in known]
    if bad:
        raise HTTPException(status_code=422, detail=f"unknown sections: {bad}")
    return _enqueue_lesson_ai(db, background_tasks, lesson_id, {
        "mode": "apply", "instruction": payload.instruction, "note": payload.note,
        "sections": [p.model_dump() for p in payload.sections]})
```
(Pydantic `min_length=1` on the list makes `[]` a 422 automatically.)

- [ ] **Step 5: Run** — `pytest tests/test_lesson_ai_api.py tests/test_lesson_ai.py tests/test_curriculum_api.py -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/app/jobs/lesson_ai.py apps/api/app/routers/curriculum.py apps/api/app/schemas/curriculum.py apps/api/tests/test_lesson_ai_api.py
git commit -m "feat(lesson-ai): plan and apply as one job kind behind two routes"
```

### Task 2.4: Sweep — an interrupted apply leaves the lesson `ready`

**Files:**
- Modify: `apps/api/app/jobs/sweep.py:30-61`
- Test: `apps/api/tests/test_jobs_sweep.py` (extend)

- [ ] **Step 1: Failing test** (append; use that file's fixture style)

```python
def test_interrupted_lesson_with_segments_goes_back_to_ready_not_queued(db_session_fixture_name):
    # lesson meta draft_status="drafting" AND it has one segment with a body
    # → after sweep_interrupted_lessons: draft_status == "ready", error mentions the restart
    # a lesson with NO segments still goes to "queued" (the old rule)
```
Write it concretely against the fixtures in `test_jobs_sweep.py`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — in `sweep_interrupted_lessons`, per stuck lesson:

```python
        has_text = db.scalar(select(Block.id).where(
            Block.parent_id == lesson.id, Block.kind == "segment", Block.body.isnot(None), Block.body != "",
        ).limit(1)) is not None
        # A lesson that already has text was mid-REWRITE (lesson panel apply,
        # or a revise re-draft). Its text is fine; nothing to draft. Back to
        # `ready`, not `queued` — `queued` would make Resume rewrite it from
        # scratch, which is the one thing an interruption must not cause.
        status = "ready" if has_text else "queued"
        lesson.meta = {**(lesson.meta or {}), "draft_status": status,
                       "error": "interrupted by a restart" + ("" if has_text else " — press Resume")}
```

- [ ] **Step 4: Run** — `pytest tests/test_jobs_sweep.py tests/test_main_lifespan.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/app/jobs/sweep.py apps/api/tests/test_jobs_sweep.py
git commit -m "fix(sweep): a lesson interrupted mid-rewrite keeps its text and stays ready"
```

### Task 2.5: Web — API client, scope provider wiring, the side panel

**Files:**
- Modify: `apps/web/src/lib/api.ts`, `apps/web/src/app/[locale]/(cockpit)/curricula/[rootId]/page.tsx`, `apps/web/src/messages/{el,en}.json`
- Create: `apps/web/src/components/curriculum/lesson-ai-panel.tsx`
- Test: `apps/web/tests/lesson-ai-panel.spec.ts` (new, Task 2.6 extends it)

**Interfaces:**
- Consumes: `useLessonAiScope()` (Task 1.5), `getJob`, `jobErrorText`, `WhatChanged`, `LessonWhatChanged`.
- Produces (api.ts):
```ts
export interface LessonPlanSection { section: string; title: string; action: "rewrite" | "keep"; reason: string; brief: string; tutor_edited: boolean }
export interface LessonPlan { summary: string; sections: LessonPlanSection[]; note_to_tutor: string; dropped: { section: string; reason: string }[]; impact: { rewrite_count: number; est_words: number } }
export function planLessonAi(lessonId: string, input: { instruction: string; note?: string }): Promise<JobAccepted>
export function applyLessonAi(lessonId: string, input: { instruction: string; note?: string; sections: { section: string; brief: string }[] }): Promise<JobAccepted>
```
- Panel props: `{ tree: BlockNode; onApplied: () => void }`; it reads the lesson from the scope, finds its node in `tree` (recursive search by id) for the sections/tutor-edit strip. Testids: `lesson-ai-panel`, `lesson-ai-close`, `lesson-ai-instruction`, `lesson-ai-plan`, `lesson-ai-chip-propagate`, `lesson-ai-status`, `lesson-ai-error`, `lesson-ai-edited-chip` (one per edited section), `lesson-ai-done`.

- [ ] **Step 1: Failing Playwright test**

```ts
// apps/web/tests/lesson-ai-panel.spec.ts
// helpers + `tree` as in tutor-edited.spec.ts (a ready lesson with 8 segments, theory tutor_edited)
test("the panel opens scoped to the lesson, plans via a job and shows the card", async ({ page }) => {
  let planCalls = 0; let polls = 0;
  // in the route handler:
  //  POST /blocks/{LESSON}/ai/plan → 202 {job_id: JOB, status: "pending"}; planCalls++ and assert body.instruction contains "Ενημέρωσε"
  //  GET /jobs/{JOB} → first "running", then "succeeded" with progress.plan = {
  //     summary: "Θα ξαναγράψω 3 ενότητες.", note_to_tutor: "", dropped: [],
  //     impact: {rewrite_count: 3, est_words: 900},
  //     sections: [ {section:"warm_up",title:"Ζέσταμα",action:"rewrite",reason:"αναφέρει παλιά θεωρία",brief:"πες για τη Strat",tutor_edited:false},
  //                 {section:"theory",title:"Θεωρία",action:"keep",reason:"δική σου",brief:"",tutor_edited:true},
  //                 {section:"exercises",title:"Ασκήσεις",action:"rewrite",reason:"…",brief:"…",tutor_edited:false},
  //                 {section:"recap",title:"Ανακεφαλαίωση",action:"rewrite",reason:"…",brief:"…",tutor_edited:false},
  //                 ...keep for the rest ] }
  await page.goto(`/el/curricula/${ROOT}`);
  await page.getByTestId("lesson-ai").first().click();
  const panel = page.getByTestId("lesson-ai-panel");
  await expect(panel).toContainText("Μπράτσο");
  await expect(panel.getByTestId("lesson-ai-edited-chip")).toHaveCount(1);
  await panel.getByTestId("lesson-ai-chip-propagate").click();
  await expect(panel.getByTestId("lesson-ai-instruction")).toHaveValue(/Ενημέρωσε/);
  await panel.getByTestId("lesson-ai-plan").click();
  await expect(panel.getByTestId("lesson-ai-status")).toBeVisible();
  await expect(panel.getByTestId("lesson-plan-card")).toContainText("Θα ξαναγράψω 3 ενότητες.");
  expect(planCalls).toBe(1);
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement api.ts** — the two functions + types next to `restoreLessonSegments`, using `request<JobAccepted>` with `method: "POST"` and JSON bodies; JSDoc states the polling obligation (`getJob` until terminal; plan on `progress.plan`).

- [ ] **Step 4: Implement the panel**

Structure (copy `ReviseDrawer`'s aside/backdrop/full-screen/Escape code; strings under a new `curricula.lessonAi` namespace):

```tsx
"use client";
export function LessonAiPanel({ tree, onApplied }: { tree: BlockNode; onApplied: () => void }) {
  const t = useTranslations("curricula.lessonAi");
  const tJobErrors = useTranslations("jobErrors");
  const scope = useLessonAiScope();
  const [open, setOpen] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [note, setNote] = useState("");
  const [phase, setPhase] = useState<"idle" | "planning" | "card" | "applying" | "done">("idle");
  const [plan, setPlan] = useState<LessonPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lessonNode = useMemo(() => (scope?.lesson ? findNode(tree, scope.lesson.id) : null), [tree, scope?.lesson]);
  const edited = useMemo(() => (lessonNode?.children ?? []).filter((s) => s.meta?.tutor_edited), [lessonNode]);
  // open on request counter (same pattern as ReviseDrawer); reset state when the lesson id changes
  async function waitForJob(jobId: string) { /* 2s poll, 300 max, as chat-panel */ }
  async function handlePlan(extraNote?: string) {
    if (!scope?.lesson) return;
    setPhase("planning"); setError(null);
    try {
      const accepted = await planLessonAi(scope.lesson.id, { instruction, note: extraNote ?? note });
      const job = await waitForJob(accepted.job_id);
      if (job.status !== "succeeded") { setError(jobErrorText(job, tJobErrors)); setPhase(plan ? "card" : "idle"); return; }
      setPlan(job.progress?.plan as LessonPlan); setPhase("card");
    } catch (err) { setError(err instanceof ApiError ? err.detail : t("error")); setPhase("idle"); }
  }
  async function handleApply(picks: { section: string; brief: string }[], extraNote: string) {
    if (!scope?.lesson) return;
    setPhase("applying"); setError(null);
    try {
      const accepted = await applyLessonAi(scope.lesson.id, { instruction, note: extraNote, sections: picks });
      const job = await waitForJob(accepted.job_id);
      if (job.status !== "succeeded") { setError(jobErrorText(job, tJobErrors)); setPhase("card"); return; }
      onApplied(); setPhase("done");
    } catch (err) { setError(err instanceof ApiError ? err.detail : t("error")); setPhase("card"); }
  }
  // render: header (lesson title · module title), edited strip (chips → WhatChanged with prev_body/body),
  // quick chips (propagate only when edited.length > 0; two fixed suggestions),
  // Textarea data-testid="lesson-ai-instruction", Button data-testid="lesson-ai-plan" (disabled when instruction.trim().length < 3 or phase planning/applying),
  // phase === "planning" → <p role="status" data-testid="lesson-ai-status">{t("planning")}</p>
  // phase === "card" && plan → <LessonPlanCard plan={plan} onReplan={(n) => handlePlan(n)} onApply={handleApply} busy={false} />
  // phase === "applying" → status t("applying")
  // phase === "done" → <p data-testid="lesson-ai-done">{t("done")}</p> + <LessonWhatChanged lessonId prevSegments={lessonNode.meta.prev_segments} liveSegments={lessonNode.children} instruction={lessonNode.meta.revise_instruction} onRestored={() => onApplied()} /> + button t("again") → setPhase("idle")
  // error → <p role="alert" data-testid="lesson-ai-error">
}
function findNode(node: BlockNode, id: string): BlockNode | null { if (node.id === id) return node; for (const c of node.children ?? []) { const f = findNode(c, id); if (f) return f; } return null; }
```

Mount in `page.tsx` inside `<LessonAiScopeProvider>`: `<LessonAiPanel tree={tree} onApplied={refreshTree} />` as a sibling of `ReviseDrawer` (the `tree` prop is the page's current tree; after `refreshTree` the panel re-reads `lessonNode` for the done state).

Strings `curricula.lessonAi` (el / en): `heading` «AI στο μάθημα» / "AI on this lesson"; `description` «Πες τι θέλεις να αλλάξει σε αυτό το μάθημα. Θα δεις πρώτα ένα πλάνο ανά ενότητα — τίποτα δεν αλλάζει μέχρι να πατήσεις Εφαρμογή.»; `editedStrip` «Άλλαξες με το χέρι:»; `chipPropagate` «Ενημέρωσε τις υπόλοιπες ενότητες με βάση τις αλλαγές μου»; `chipHarder` «Κάνε τις ασκήσεις πιο δύσκολες»; `chipExamples` «Πρόσθεσε παραδείγματα στη θεωρία»; `placeholder` «π.χ. Άλλαξα τη θεωρία — φέρε τις υπόλοιπες ενότητες σε συμφωνία μαζί της»; `plan` «Φτιάξε πλάνο»; `planning` «Διαβάζω το μάθημα και φτιάχνω πλάνο…»; `applying` «Ξαναγράφω τις ενότητες…»; `done` «Έγινε — δες «Τι άλλαξε;» ή γύρνα πίσω αν δεν σου αρέσει.»; `again` «Νέα αλλαγή»; `error` «Κάτι πήγε στραβά — δοκίμασε ξανά.»; `close`, `expand`, `collapse` as in `revise`.

- [ ] **Step 5: Run** — `npx tsc --noEmit && npm run lint && npx playwright test tests/lesson-ai-panel.spec.ts` (the card assertion needs Task 2.6; keep it and expect that single assertion to fail until then — or mark it `test.fixme` for this commit and unmark in 2.6).

- [ ] **Step 6: Commit**

```bash
git add apps/web/src apps/web/tests/lesson-ai-panel.spec.ts
git commit -m "feat(web): «AI στο μάθημα» — a lesson-scoped panel that plans through a job"
```

### Task 2.6: Web — the plan card (tick, note, re-plan, apply) + done state

**Files:**
- Create: `apps/web/src/components/curriculum/lesson-plan-card.tsx`
- Modify: `apps/web/src/components/curriculum/lesson-ai-panel.tsx` (render), `apps/web/src/messages/{el,en}.json`
- Test: `apps/web/tests/lesson-ai-panel.spec.ts` (extend)

**Interfaces:**
- `LessonPlanCard({ plan, onReplan(note: string), onApply(picks: {section, brief}[], note: string), busy })`. Testids: `lesson-plan-card`, `lesson-plan-row-{section}`, `lesson-plan-check-{section}`, `lesson-plan-brief-{section}`, `lesson-plan-note`, `lesson-ai-replan`, `lesson-ai-apply`, `lesson-plan-impact`, `lesson-plan-dropped`.

- [ ] **Step 1: Extend the Playwright test**

```ts
  // continuing the previous test after the card is visible:
  const card = panel.getByTestId("lesson-plan-card");
  await expect(card.getByTestId("lesson-plan-check-warm_up")).toBeChecked();
  await expect(card.getByTestId("lesson-plan-check-theory")).not.toBeChecked();
  await expect(card.getByTestId("lesson-plan-impact")).toContainText("3");
  await card.getByTestId("lesson-plan-check-recap").uncheck();
  await expect(card.getByTestId("lesson-plan-impact")).toContainText("2");
  await card.getByTestId("lesson-plan-note").fill("και πιο σύντομα");
  // POST /blocks/{LESSON}/ai/apply → assert body.sections has exactly warm_up + exercises, body.note === "και πιο σύντομα"; 202 {job_id: JOB2}
  // GET /jobs/{JOB2} → succeeded {phase:"done", rewritten:["warm_up","exercises"], word_count: 2000}
  // GET /curricula/{ROOT} after apply → tree with meta.prev_segments on the lesson
  await card.getByTestId("lesson-ai-apply").click();
  await expect(panel.getByTestId("lesson-ai-done")).toBeVisible();
  await expect(panel.getByTestId("lesson-what-changed-trigger")).toBeVisible();
});

test("re-plan sends the note and replaces the card", async ({ page }) => {
  // plan → card → fill note "χωρίς ασκήσεις" → click lesson-ai-replan
  // assert second POST /ai/plan body.note === "χωρίς ασκήσεις"; job returns a plan with rewrite_count 1 → impact shows 1
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement the card**

```tsx
export function LessonPlanCard({ plan, onReplan, onApply, busy }: Props) {
  const t = useTranslations("curricula.lessonAi.card");
  const [checked, setChecked] = useState<Record<string, boolean>>(
    () => Object.fromEntries(plan.sections.map((s) => [s.section, s.action === "rewrite"])));
  const [briefs, setBriefs] = useState<Record<string, string>>(
    () => Object.fromEntries(plan.sections.map((s) => [s.section, s.brief])));
  const [note, setNote] = useState("");
  useEffect(() => { /* when `plan` changes (re-plan), reset checked/briefs from it */ }, [plan]);
  const picks = plan.sections.filter((s) => checked[s.section]).map((s) => ({ section: s.section, brief: briefs[s.section] ?? "" }));
  return (
    <div data-testid="lesson-plan-card" className="rounded-lg border border-border p-3 flex flex-col gap-3">
      <p className="text-sm">{plan.summary}</p>
      {plan.note_to_tutor && <p className="text-sm text-amber-700 dark:text-amber-400">{plan.note_to_tutor}</p>}
      {plan.dropped.length > 0 && <p data-testid="lesson-plan-dropped" className="text-xs text-amber-700">{t("dropped", { count: plan.dropped.length })}</p>}
      <ul className="flex flex-col gap-2">
        {plan.sections.map((s) => (
          <li key={s.section} data-testid={`lesson-plan-row-${s.section}`} className="flex flex-col gap-1">
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" data-testid={`lesson-plan-check-${s.section}`} checked={!!checked[s.section]}
                     onChange={(e) => setChecked((c) => ({ ...c, [s.section]: e.target.checked }))} className="mt-1" />
              <span className="flex flex-col">
                <span className="font-medium">{s.title}{s.tutor_edited && <Badge variant="outline" className="ml-2">{t("yours")}</Badge>}</span>
                {s.reason && <span className="text-xs text-muted-foreground">{s.reason}</span>}
              </span>
            </label>
            {checked[s.section] && (
              <Input data-testid={`lesson-plan-brief-${s.section}`} value={briefs[s.section] ?? ""} placeholder={t("briefPlaceholder")}
                     onChange={(e) => setBriefs((b) => ({ ...b, [s.section]: e.target.value }))} className="ml-6 text-xs" />
            )}
          </li>
        ))}
      </ul>
      <p data-testid="lesson-plan-impact" className="text-xs text-muted-foreground">{t("impact", { count: picks.length })}</p>
      <Textarea data-testid="lesson-plan-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("notePlaceholder")} />
      <div className="flex gap-2">
        <Button type="button" variant="outline" data-testid="lesson-ai-replan" disabled={busy} onClick={() => onReplan(note)}>{t("replan")}</Button>
        <Button type="button" data-testid="lesson-ai-apply" disabled={busy || picks.length === 0} onClick={() => onApply(picks, note)}>{t("apply")}</Button>
      </div>
      {picks.length === 0 && <p className="text-xs text-muted-foreground">{t("nothingTicked")}</p>}
    </div>
  );
}
```

Strings `curricula.lessonAi.card`: el `yours` «δική σου», `briefPlaceholder` «Τι ακριβώς να αλλάξει εδώ;», `impact` «{count, plural, =0 {Καμία ενότητα δεν θα ξαναγραφεί} one {Θα ξαναγραφεί # ενότητα} other {Θα ξαναγραφούν # ενότητες}} — οι υπόλοιπες μένουν όπως είναι.», `notePlaceholder` «Κάτι ακόμα για το AI; (προαιρετικό)», `replan` «Ξαναφτιάξε το πλάνο», `apply` «Εφαρμογή», `nothingTicked` «Δεν επέλεξες ενότητες.», `dropped` «{count} πρόταση αγνοήθηκε από τον έλεγχο ασφαλείας.»; English equivalents.

Panel: on `onReplan(note)` call `handlePlan(note)` and keep the previous card visible under the status until the new plan lands.

- [ ] **Step 4: Run** — `npx tsc --noEmit && npm run lint && npx playwright test tests/lesson-ai-panel.spec.ts tests/tutor-edited.spec.ts` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/web/src apps/web/tests/lesson-ai-panel.spec.ts
git commit -m "feat(web): the per-section plan card — tick, brief, note, re-plan, apply, what-changed"
```

# Unit 3 — Guided «Προσθήκη μαθήματος»

### Task 3.1: `curriculum_draft` accepts `lesson_ids`

**Files:**
- Modify: `apps/api/app/jobs/curriculum_draft.py:484` (after `lesson_ids = _queued_lesson_ids(db, root_id)`)
- Test: `apps/api/tests/test_curriculum_draft_job.py` (extend)

- [ ] **Step 1: Failing test** — a root with two `queued` lessons; enqueue `curriculum_draft` with `params={"root_id":..., "lesson_ids":[str(l2.id)]}`; stub `_draft_one` to record ids; assert only `l2` was drafted and the job `succeeded` (progress counts still come from `draft_progress`, so `queued: 1` remains — assert that too).

- [ ] **Step 2: Run** → FAIL (both drafted).

- [ ] **Step 3: Implement**

```python
        lesson_ids = _queued_lesson_ids(db, root_id)
        # `lesson_ids` on params (Unit 3): draft ONLY these, when they are
        # queued. `None`/absent = every queued lesson (today's Resume). Used by
        # the guided add-lesson chain so one new lesson does not drag every
        # other straggler into the same fan-out.
        only = job.params.get("lesson_ids")
        if only:
            wanted = {uuid.UUID(str(x)) for x in only}
            lesson_ids = [lid for lid in lesson_ids if lid in wanted]
```
Also in the finalize block: when `only` was given, judge success on those ids alone — `report["failed"]` counts the whole root; compute `mine = {lid: status}` via one query over `Block.meta["draft_status"]` for `wanted` and set `job.status = "failed"` only if every wanted lesson is `failed`.

- [ ] **Step 4: Run** — `pytest tests/test_curriculum_draft_job.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(draft): a draft job can be pointed at specific lessons"`.

### Task 3.2: Neighbours + tutor brief blocks in every draft

**Files:**
- Modify: `apps/api/app/curriculum/draft.py` (`LESSON_NEIGHBOURS_BLOCK`, `LESSON_TUTOR_BRIEF_BLOCK`, `LESSON_TAIL` gets `{neighbours_block}` after the POSITION line and `{tutor_brief_block}` after `{course_brief_block}`; `build_lesson_messages(neighbours: str | None, tutor_brief: str | None)`), `apps/api/app/jobs/curriculum_draft.py` (`plan["neighbours"]` map built in Phase A; `_draft_one` passes `neighbours=plan["neighbours"][str(lesson_id)]`, `tutor_brief=(lesson.meta or {}).get("brief")`), `apps/api/app/prompts/registry.py` (two fragments + `lesson.draft` `build` renders a sample neighbours block), `apps/api/tests/fixtures/prompt_renders_baseline.json`
- Test: `apps/api/tests/test_lesson_draft.py` (extend), `tests/test_curriculum_draft_job.py` (extend)

**Interfaces:**
- `draft.LESSON_NEIGHBOURS_BLOCK = "\nΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ: {prev}\nΕΠΟΜΕΝΟ ΜΑΘΗΜΑ: {next}\nΣΤΗΝ ΙΔΙΑ ΕΝΟΤΗΤΑ: {siblings}\n"` (slice `lesson.draft.neighbours`); `draft.LESSON_TUTOR_BRIEF_BLOCK = "\n\nΟ ΚΑΘΗΓΗΤΗΣ ΖΗΤΗΣΕ ΡΗΤΑ αυτό το μάθημα να καλύπτει:\n{brief}\nΚάλυψε κάθε σημείο του με το βάθος που του αξίζει και συμπλήρωσε ό,τι λείπει."` (slice `lesson.draft.brief`).
- `curriculum_draft._neighbours(db, root_id) -> dict[str, dict]` → `{lesson_id: {"prev": "title — objective" | "—", "next": ..., "siblings": "t1, t2"}}` built beside `_positions`.
- `LESSON_TAIL` changes bytes for EVERY draft (the neighbours block is always rendered, «—» at the edges): the byte-identity baseline is regenerated in this task on purpose; pinned tests that compare full prompt text (`test_surgical_revise.py` same-kwargs pins are relative comparisons and stay green).

- [ ] **Step 1: Failing tests** — in `test_lesson_draft.py`: `build_lesson_messages(..., neighbours={"prev": "Τα ξύλα — ξύλα", "next": "—", "siblings": "Μαγνήτες"})` renders «ΠΡΟΗΓΟΥΜΕΝΟ ΜΑΘΗΜΑ: Τα ξύλα — ξύλα» and «ΕΠΟΜΕΝΟ ΜΑΘΗΜΑ: —»; `tutor_brief="Ξύλο μπράτσου: Maple…"` renders the brief block; `tutor_brief=None` renders nothing for it. In `test_curriculum_draft_job.py`: `_neighbours` for M1[L1,L2,L3] gives L2 prev=L1, next=L3, siblings "L1, L3"; `_draft_one` passes `tutor_brief` from `meta.brief`.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — constants; `LESSON_TAIL` edit:
```
    "POSITION: {position}. Do not re-teach what earlier lessons covered; "
    "build on it.\n"
    "{neighbours_block}"
    ...
    "{course_brief_block}"
    "{tutor_brief_block}"
```
`build_lesson_messages`: `neighbours: dict | None = None, tutor_brief: str | None = None`; render `neighbours_block = resolve(source, LESSON_NEIGHBOURS_SLICE_ID, LESSON_NEIGHBOURS_BLOCK).format(prev=..., next=..., siblings=...) if neighbours else ""` and `tutor_brief_block = resolve(source, LESSON_TUTOR_BRIEF_SLICE_ID, LESSON_TUTOR_BRIEF_BLOCK).format(brief=tutor_brief.strip()) if tutor_brief and tutor_brief.strip() else ""`. `draft_lesson` threads both. In `curriculum_draft.py`, `_neighbours` mirrors `_positions`'s loops; `plan["neighbours"] = _neighbours(db, root_id)`; `_draft_one` reads `neighbours=plan.get("neighbours", {}).get(str(lesson_id))` and `tutor_brief=(lesson.meta or {}).get("brief")` before `db.close()`. `lesson_ai.apply_lesson_change` (Task 2.2) switches from stuffing neighbours into `position` to passing `neighbours=` — build the dict from `neighbours_text`'s siblings (refactor `neighbours_text` into `neighbours_dict(db, lesson) -> dict` + a text renderer).

- [ ] **Step 4: Registry + baseline** — register `lesson.draft.neighbours` and `lesson.draft.brief` fragments; make `_build_lesson_draft` render with a sample `neighbours` so the viewer shows the real prompt. `pytest tests/test_lesson_draft.py tests/test_curriculum_draft_job.py tests/test_prompts_registry.py -q`; `python -m tests.prompt_baseline`; `git diff tests/fixtures/prompt_renders_baseline.json` must show the `lesson.draft` render gaining the neighbours lines and the two new fragments — nothing else; `pytest tests/test_prompts_byte_identity.py tests/test_surgical_revise.py -q`.

- [ ] **Step 5: Commit** — `git commit -m "feat(draft): every lesson sees its neighbours; a tutor brief is honoured whole"`.

### Task 3.3: `plan_lesson_json` + `lesson_generate` job + route

**Files:**
- Modify: `apps/api/app/curriculum/extend.py` (`LESSON_PLAN_SCHEMA_ONE`, `LESSON_PLAN_TAIL`, `build_lesson_plan_messages`, `plan_lesson_json`, `generate_lesson`), `apps/api/app/routers/curriculum.py` (route + runner import), `apps/api/app/schemas/curriculum.py` (`LessonGenerateRequest`), `apps/api/app/prompts/registry.py` (`curriculum.extend.lesson`)
- Create: `apps/api/app/jobs/lesson_generate.py`
- Test: `apps/api/tests/test_lesson_generate.py` (new)

**Interfaces:**
- `POST /blocks/{module_id}/lessons/generate` `{brief: str (min 10), title?: str, after?: uuid}` → 202 (kind `lesson_generate`, params `{module_id, brief, title, after}`); 404 non-module; 422 `after` not a sibling.
- `extend.plan_lesson_json(db, *, course, module, library, brief, title) -> {"title","objective","est_minutes"}` — one `guided_json(role="plan")`; a tutor `title` overrides.
- `extend.generate_lesson(db, module_id, *, brief, title, after) -> Block` — plan → `edit._add_lesson(...)` → `meta = {**meta, "brief": brief}` → commit.
- Job progress: `{"phase":"planning"}` → `{"phase":"drafting","lesson_id":..., "draft_job_id":...}` → chained `curriculum_draft` with `lesson_ids=[id]` run in the same thread (the `module_generate.py:79-100` pattern); `result_root_id` = course id.

- [ ] **Step 1: Failing tests**

```python
# apps/api/tests/test_lesson_generate.py
# fixture: course (shape minutes 50, blueprint default) → module M with lessons L1 "Τα ξύλα", L2 "Μαγνήτες"
def test_plan_messages_show_siblings_course_map_and_the_brief(...):
    # capture messages via a fake provider; assert "Τα ξύλα" and "Μαγνήτες" in the tail,
    # the other module titles appear, the brief text appears under "ΤΟ ΜΑΘΗΜΑ ΠΟΥ ΖΗΤΗΣΕ", role == "plan"
def test_generate_lesson_adds_queued_lesson_with_brief_and_position(...):
    # fake provider returns {"title": "Το μπράτσο", "objective": "…", "est_minutes": 50}
    # generate_lesson(db, M.id, brief="…", title=None, after=L1.id) → order 1, meta.brief == brief, draft_status queued, added_by_tutor True
def test_tutor_title_wins(...):
def test_route_enqueues_and_job_chains_a_single_lesson_draft(monkeypatch, ...):
    # monkeypatch job_mod.generate_lesson → returns the Block; monkeypatch job_mod.run_curriculum_draft_job → records params
    # POST → 202; job succeeded; progress.lesson_id set; recorded draft params == {"root_id": course, "lesson_ids": [lesson]}
def test_route_422_when_after_is_not_a_sibling(...):
```
Write the bodies concretely (copy the fixture shape from `test_lesson_ai.py`).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement in `extend.py`**

```python
LESSON_PLAN_SCHEMA_ONE: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "the lesson's title, in the course language"},
        "objective": {"type": "string", "description": "one or two sentences: what the student can do after it"},
        "est_minutes": {"type": "integer"},
    },
    "required": ["title", "objective", "est_minutes"],
    "additionalProperties": False,
}

LESSON_PLAN_TAIL = (
    "YOUR TASK: design ONE new lesson for an existing module — a title and a "
    "one-to-two-sentence objective (it is drafted in full later, from the "
    "tutor's brief below). It must fit the module's arc and must NOT repeat any "
    "lesson the module already has.\n"
    "\nCOURSE: {course_title}{course_brief_block}\n"
    "\nTHE COURSE'S MODULES:\n{course_map}\n"
    "\nTHE MODULE THIS LESSON JOINS: {module_title} — {module_objective}\n"
    "ITS LESSONS, IN ORDER:\n{siblings}\n"
    "\nΤΟ ΜΑΘΗΜΑ ΠΟΥ ΖΗΤΗΣΕ Ο ΚΑΘΗΓΗΤΗΣ, με τα δικά του λόγια:\n{brief}\n"
    "{title_block}"
    "\nSHAPE: {minutes_per_lesson} minutes, later drafted to ~{target_words} words.\n"
    "\n{language_directive}\n"
    "\n{style_directive}\n"
    "\n{answer_in}"
)
LESSON_PLAN_SLICE_ID = "curriculum.extend.lesson"
LESSON_PLAN_TITLE_BLOCK = "\nTHE TUTOR ALREADY CHOSE THE TITLE — keep it exactly: {title}\n"
```
`build_lesson_plan_messages(*, course_title, brief_course, language, course_map, module_title, module_objective, siblings, brief, title, minutes_per_lesson, target_words, library, source=None)` = `prefix_messages(library, source)` + one user message. `course_map` = `_module_lines(db, modules)` (reuse). `siblings` = `"\n".join(f"{i}. {l.title} — {(l.meta or {}).get('objective') or ''}")` or `"(no lessons yet)"`. `plan_lesson_json` mirrors `generate_module_json`; `generate_lesson` mirrors `generate_module` and ends:
```python
    lesson = _add_lesson(db, module.id, title=(title or planned["title"]).strip(),
                         objective=planned.get("objective") or "", after=after)
    lesson.est_minutes = _est_minutes(planned.get("est_minutes"), shape.get("minutes_per_lesson", 50))
    lesson.meta = {**(lesson.meta or {}), "brief": brief.strip()}
    db.commit(); db.refresh(lesson)
    return lesson
```
(`_add_lesson` from `app.curriculum.edit`; `EditError` → `ExtendError`.)

- [ ] **Step 4: Job + route + schema** — `jobs/lesson_generate.py` copies `module_generate.py` with `generate_lesson`, progress `{"phase":"planning"}` → success `{"phase":"drafting","lesson_id":str(lesson.id)}`, `result_root_id = course id`, then chains `GenerationJob(kind="curriculum_draft", params={"root_id": str(root_id), "lesson_ids": [str(lesson.id)]})` and calls `run_curriculum_draft_job(draft_job_id)` in the same thread; after enqueueing, write `progress["draft_job_id"]` on the lesson_generate row. Schema `LessonGenerateRequest(brief: str = Field(min_length=10), title: str | None = None, after: UUID | None = None)`. Route (module-level import of `run_lesson_generate_job`):

```python
@router.post("/blocks/{module_id}/lessons/generate", response_model=JobAccepted, status_code=202,
             dependencies=[Depends(require_llm_configured)])
def generate_module_lesson(module_id: UUID, payload: LessonGenerateRequest,
                           background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> JobAccepted:
    """«Προσθήκη μαθήματος» with a brief: plan title+objective from the module's
    siblings and the tutor's words, store the brief on the lesson, draft it."""
    module = _get_block_or_404(db, module_id)
    if module.kind != "module":
        raise HTTPException(status_code=404, detail="not a module")
    if payload.after is not None:
        sib = db.get(Block, payload.after)
        if sib is None or sib.parent_id != module.id:
            raise HTTPException(status_code=422, detail="`after` is not a lesson of this module")
    job = GenerationJob(kind="lesson_generate", status="pending", params={
        "module_id": str(module_id), "brief": payload.brief, "title": payload.title,
        "after": str(payload.after) if payload.after else None})
    db.add(job); db.commit(); db.refresh(job)
    background_tasks.add_task(run_lesson_generate_job, job.id)
    return JobAccepted(job_id=job.id, status=job.status)
```
Register `curriculum.extend.lesson` in the registry (`kind="prompt"`, `call_sites` at the `guided_json` line in `extend.py`), regenerate the baseline (new entry only).

- [ ] **Step 5: Run** — `pytest tests/test_lesson_generate.py tests/test_curriculum_extend.py tests/test_prompts_registry.py tests/test_prompts_byte_identity.py -q` → PASS.
- [ ] **Step 6: Commit** — `git commit -m "feat(curriculum): a lesson from a brief — planned against its siblings, drafted alone"`.

### Task 3.4: Web — the add-lesson dialog

**Files:**
- Create: `apps/web/src/components/curriculum/add-lesson-dialog.tsx`
- Modify: `apps/web/src/components/curriculum/block-card.tsx:538-545` (menu item opens the dialog instead of calling `addLesson` directly), `apps/web/src/lib/api.ts` (`generateLesson`), `apps/web/src/messages/{el,en}.json`
- Test: `apps/web/tests/add-lesson-dialog.spec.ts` (new)

**Interfaces:**
- `generateLesson(moduleId, {brief, title?, after?}): Promise<JobAccepted>`.
- `AddLessonDialog({ moduleId, moduleTitle, siblings: {id,title}[], open, onOpenChange, onAdded(): void })`. Testids: `add-lesson-dialog`, `add-lesson-brief`, `add-lesson-title`, `add-lesson-after`, `add-lesson-generate`, `add-lesson-empty`, `add-lesson-status`, `add-lesson-error`.

- [ ] **Step 1: Failing Playwright test** — open module ⋯ → «Προσθήκη μαθήματος» → dialog visible; generate button disabled until 10 chars; fill the brief with Chris's neck outline (first 3 lines suffice); select `after` = first sibling; click generate → assert `POST /blocks/{m1}/lessons/generate` body `{brief, title: null, after: <id>}` → 202; `GET /jobs/{id}` running → succeeded with `progress {phase:"drafting", lesson_id, draft_job_id}`; the dialog shows `add-lesson-status` text changing («Σχεδιάζω τον τίτλο…» → «Γράφεται…») then closes; `GET /curricula/{ROOT}` is re-fetched (count the calls ≥ 2). Second test: «Κενό μάθημα» still POSTs `/blocks/{m1}/lessons` with `{title: "Νέο μάθημα"}` (today's behaviour).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — Base UI `Dialog` (controlled `open`), `Textarea rows={8}` for the brief, `Input` for the title, native `<select>` for position («Στο τέλος» + one option per sibling «Μετά από: {title}»), two buttons. On generate: `setPhase("planning")` → `generateLesson(...)` → poll `getJob` every 2s (deadline 10 min; when `progress.phase === "drafting"` switch the status text) → on `succeeded`: `onAdded()` (page `refreshTree`, which re-arms the draft progress bar) and close; on `failed`: `jobErrorText`. «Κενό μάθημα» calls the existing `addLesson(moduleId, {title: t("newLessonTitle")})` then `onAdded()`. In `block-card.tsx`, the menu item sets `addLessonOpen=true` (state local to the module card) and renders `<AddLessonDialog moduleId={node.id} moduleTitle={node.title} siblings={node.children.filter(c => c.kind==="lesson").map(...)} open onOpenChange onAdded={() => onRefresh?.()} />`.

Strings `curricula.tree.addLessonDialog` (el): `title` «Νέο μάθημα στην ενότητα «{module}»», `briefLabel` «Τι θέλεις να διδάσκει αυτό το μάθημα;», `briefPlaceholder` «Γράψε ελεύθερα — θέματα, σημεία που θέλεις οπωσδήποτε, ακόμα και ολόκληρο διάγραμμα. Το AI θα τα καλύψει όλα και θα συμπληρώσει ό,τι λείπει.», `titleLabel` «Τίτλος (προαιρετικά — αλλιώς τον διαλέγει το AI)», `afterLabel` «Θέση», `afterEnd` «Στο τέλος», `afterItem` «Μετά από: {title}», `generate` «Δημιουργία με AI», `empty` «Κενό μάθημα», `planning` «Σχεδιάζω τον τίτλο και τον στόχο από τη βιβλιοθήκη σου…», `drafting` «Γράφεται… θα εμφανιστεί στον πίνακα μόλις ετοιμαστεί.», `error` «Δεν ήταν δυνατή η δημιουργία του μαθήματος.», `tooShort` «Γράψε τουλάχιστον μια πρόταση.»; English equivalents.

- [ ] **Step 4: Run** — `npx tsc --noEmit && npm run lint && npx playwright test tests/add-lesson-dialog.spec.ts tests/curricula-resume.spec.ts` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): «Προσθήκη μαθήματος» asks what the lesson should teach"`.

# Unit 4 — Ship

### Task 4.1: Full suites, deploy to the webapp, live verification

- [ ] **Step 1:** `cd apps/api && pytest -q` (whole suite, serialized) → green. `cd apps/web && npx tsc --noEmit && npm run lint && npx playwright test` → green.
- [ ] **Step 2:** Rebuild + redeploy: `cd /mnt/nvme2TB/guitar_tutor && docker compose build api web && docker compose up -d api web` (the claude-bridge container is unchanged unless `bridge.py` changed — Task 0.4 did: `docker compose build claude-bridge && docker compose up -d claude-bridge`). Confirm `docker exec guitar_tutor-api-1 grep -c ensure_ascii=False app/agent/loop.py` = 1.
- [ ] **Step 3 (live, on «edited test» `91cb4c9f-…`, provider `claude_cli`):**
  1. Revise drawer: type the tutor's exact sentence («Στο μάθημα: «Τα εμβληματικά μοντέλα…» άλλαξα τη θεωρία. Ξαναγραψε όλες τις υπόλοιπες ενότητες…»). Expect a 202, the panel's planning state, and within ~10 min an approval card (or a plain answer that points him to «AI στο μάθημα»). Record the bridge log line sizes (`in=` must be < 300K chars).
  2. Lesson panel on lesson `79cc4019-…`: first PATCH the theory body once through the UI so `tutor_edited` exists on this copy (it was copied before Unit 1 existed) — or run `UPDATE block SET meta = meta || '{"tutor_edited": {...}}'` is NOT allowed; use the UI. Then «Ενημέρωσε τις υπόλοιπες ενότητες…» → plan lists theory as «δική σου»/keep and ≥ 4 sections rewrite → apply → «Τι άλλαξε;» shows the diff → restore toggles back → toggle again.
  3. Module «Η Κιθάρα ως Πηγή του Ήχου» → «Προσθήκη μαθήματος» → paste Chris's neck brief (the four numbered parts) → generate → the new lesson appears queued then ready; its theory covers Maple/Mahogany, profile/mass/dead spots, frets/nut materials, truss rod/laminate/headstock. Delete nothing.
  4. `docker logs guitar_tutor-api-1 --since 1h | grep -iE "traceback|error"` → nothing new.
- [ ] **Step 4:** Commit any fix; update the memory file `guitar-tutor-data-port-2026-09-12.md` status line.

### Task 4.2: Tag `desktop-v0.5.0`

- [ ] **Step 1:** `git push origin desktop`; `git tag desktop-v0.5.0 -m "lesson AI panel, hand-edit propagation, guided add-lesson, revise chat repairs"`; `git push origin desktop-v0.5.0`. Watch `gh run list --workflow desktop-macos.yml --limit 2` and `desktop-linux.yml` to success.
- [ ] **Step 2:** `gh release view desktop-v0.5.0` shows the dmg + deb. Verify the deb locally with the recipe in memory `guitar-tutor-deb-verify-recipe` (short data dir, `XDG_*` redirect, `dbus-run-session`, `setsid --fork`), open the lesson panel once.
- [ ] **Step 3:** Hand to Chris: the release URL and `backups/2026-09-12-port/guitar-backup-webapp-2026-09-12.tar.gz` for the tutor's Settings → Επαναφορά (then re-paste his key).
