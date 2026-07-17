"""His edit reaches THE MODEL — proven at the live entry points, not at the preview.

WHY THIS FILE IS SEPARATE FROM `test_prompts_editable.py`. That file renders each
prompt through the registry and asserts the override comes back. That proves the
BUILDER reads `resolve()` — a real property, and the one P1's architecture buys for
free, because the registry's builders ARE the live builders.

It cannot prove the other half. A builder that honours an override is useless if the
live CALL SITE never hands it a resolution source: `build_lesson_messages(...)` with
no `source=` would render code defaults forever while the Settings page — which does
pass one — showed him his own text. Both halves green, the tutor lied to. So these
tests drive the REAL entry points (`run_agent_turn`, `generate_outline`, `answer`,
`draft_lesson`, `generate_artifact`, `_vision_task_for`) with a provider that records
what it was sent, and assert his sentence is ON THE WIRE.

THE ONE TO READ IS `test_the_draft_worker_gets_the_override_without_touching_the_database`.
It pins the design decision this whole task turned on — see `overrides.snapshot`.
"""
from __future__ import annotations

import pytest

from app.prompts import overrides

SENTINEL = "ΤΟ ΔΙΚΟ ΜΟΥ ΚΕΙΜΕΝΟ ΦΤΑΝΕΙ ΣΤΟ ΜΟΝΤΕΛΟ"


class _Recorder:
    """Records what it was asked to send. Returns whatever the caller needs to not
    crash — the assertion is about the REQUEST, never the response."""

    def __init__(self, payload=None):
        self.calls: list[list[dict]] = []
        self._payload = payload if payload is not None else {}

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return "ok"

    def chat_tools(self, messages, tools, **kw):
        self.calls.append(messages)
        from app.llm.tools_types import AssistantTurn

        return AssistantTurn(content="ok", tool_calls=[])

    def guided_json(self, messages, schema, **kw):
        self.calls.append(messages)
        return self._payload

    @property
    def sent(self) -> str:
        return "\n".join(
            m.get("content") or "" for call in self.calls for m in call
        )


def _override(db, slice_id: str) -> None:
    """Save his edit: the sentinel, plus the default so every `{placeholder}` the
    builder needs survives `validate`."""
    from app.prompts import registry

    _entry, sl = registry.SLICES[slice_id]
    overrides.save(db, slice_id, f"{SENTINEL}\n{sl.default}")


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


def test_the_chat_system_prompt_override_is_on_the_wire(db, monkeypatch):
    from app.agent import loop as loop_mod

    _override(db, "chat.system")
    rec = _Recorder()
    monkeypatch.setattr(loop_mod, "get_provider", lambda: rec)
    monkeypatch.setattr(loop_mod, "search", lambda *a, **k: [])

    loop_mod.run_agent_turn(db, [{"role": "user", "content": "γεια"}], locale="el")

    assert SENTINEL in rec.sent, (
        "he rewrote the chat assistant's instructions and `run_agent_turn` sent the "
        "code default anyway"
    )


# ---------------------------------------------------------------------------
# curriculum — the flow Chris named
# ---------------------------------------------------------------------------


def test_the_outline_override_is_on_the_wire(db, monkeypatch):
    """*"the tutor might have core teaching ideas which claude cannot even imagine ...
    right now he cannot inject those ideas in his creating curriculum prompts"*.
    This is that sentence, as a test."""
    from app.curriculum import corpus as corpus_mod
    from app.curriculum import outline as outline_mod
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.outline import generate_outline
    from app.curriculum.shape import plan_shape

    _override(db, "curriculum.outline")
    rec = _Recorder({"title": "T", "modules": [
        {"title": "M", "objective": "o", "tier": "library", "coverage_note": "",
         "lessons": [{"title": "L", "objective": "o", "est_minutes": 50}]},
    ]})
    monkeypatch.setattr(outline_mod, "get_provider", lambda: rec)

    generate_outline(
        db, title="Τόνος", brief=None, language="el", shape=plan_shape(4, 1, 50),
        library=LibraryContext(text="<source id='S1'>[p.1] x</source>",
                               token_count=10, fits=True, page_index={"S1": {1}}),
        student_brief=None, gap_policy="general_knowledge",
    )
    assert SENTINEL in rec.sent


def test_the_curriculum_system_prompt_override_is_on_the_wire(db, monkeypatch):
    """`curriculum.system` sits INSIDE the cached prefix. An override changes the
    prefix and re-mints the cache once — which is a thing to WARN about before a
    save, not a reason to send him a prompt he did not write."""
    from app.curriculum import outline as outline_mod
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.outline import generate_outline
    from app.curriculum.shape import plan_shape

    _override(db, "curriculum.system")
    rec = _Recorder({"title": "T", "modules": [
        {"title": "M", "objective": "o", "tier": "library", "coverage_note": "",
         "lessons": [{"title": "L", "objective": "o", "est_minutes": 50}]},
    ]})
    monkeypatch.setattr(outline_mod, "get_provider", lambda: rec)

    generate_outline(
        db, title="Τόνος", brief=None, language="el", shape=plan_shape(4, 1, 50),
        library=LibraryContext(text="<source id='S1'>[p.1] x</source>",
                               token_count=10, fits=True, page_index={"S1": {1}}),
        student_brief=None, gap_policy="general_knowledge",
    )
    assert SENTINEL in rec.calls[0][0]["content"], "the system turn is his to write"


# ---------------------------------------------------------------------------
# THE ONE THAT PINS THE DESIGN
# ---------------------------------------------------------------------------


def test_the_draft_worker_gets_the_override_without_touching_the_database(db, monkeypatch):
    """The lesson drafts are the reason `overrides.snapshot` exists.

    `jobs/curriculum_draft._draft_one` hands its connection back (`db.close()`) BEFORE
    the model call, on purpose — that one line is what keeps the progress poll
    answering while N workers each spend minutes inside `guided_json`. So the workers
    resolve NOTHING: Phase A reads the overrides once and passes a plain dict down,
    exactly as it already does for `student_brief`.

    This test drives `draft_lesson` with `db=None` — a session it CANNOT use even by
    accident — and asserts his sentence still reaches the model. If someone ever
    "simplifies" this by putting a `resolve(db, ...)` back inside the builder, this
    test is the one that says no: it would raise `AttributeError` on None rather than
    quietly re-introducing a pool stall that only shows up under a 20-lesson run.
    """
    from app.curriculum import draft as draft_mod
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.draft import LessonContext, draft_lesson

    _override(db, "lesson.draft")
    snapshot = overrides.snapshot(db)   # Phase A does this, with a real session
    assert snapshot, "Phase A must actually read his overrides"

    rec = _Recorder({
        "title": "L", "objective": "o",
        **{s: {"body": "x " * 900, "citations": []} for s in draft_mod.SECTIONS},
    })
    monkeypatch.setattr(draft_mod, "get_provider", lambda: rec)

    draft_lesson(
        None,                      # <- no session at all, like the worker's hot path
        ctx=LessonContext(
            lesson_title="Λ", lesson_objective="o", module_title="M",
            module_objective="o", course_title="C", tier="library",
            position="lesson 1 of 1, module 1 of 1", minutes=50,
            teaching_minutes=40, target_words=2200, floor_words=1600,
        ),
        library=LibraryContext(text="<source id='S1'>[p.1] x</source>",
                               token_count=10, fits=True, page_index={"S1": {1}}),
        language="el", prompts=snapshot,
    )
    assert SENTINEL in rec.sent


# ---------------------------------------------------------------------------
# retrieval / artifacts / ocr
# ---------------------------------------------------------------------------


def test_the_grounded_answer_override_is_on_the_wire(db, monkeypatch):
    from app.brain import retrieve as retrieve_mod

    _override(db, "retrieval.no_hits")
    rec = _Recorder()
    monkeypatch.setattr(retrieve_mod, "get_provider", lambda: rec)
    monkeypatch.setattr(retrieve_mod, "search", lambda *a, **k: [])

    retrieve_mod.answer(db, "πώς παίζω CAGED;", locale="el")
    assert SENTINEL in rec.sent


def test_the_artifact_override_is_on_the_wire(db, monkeypatch):
    from app.artifacts import generate as gen_mod

    _override(db, "artifacts.generate")
    rec = _Recorder({"alphaTex": "3.6.4", "title": "t"})
    monkeypatch.setattr(gen_mod, "get_provider", lambda: rec)
    monkeypatch.setattr(gen_mod, "search", lambda *a, **k: [])
    monkeypatch.setattr(gen_mod, "validate_spec", lambda kind, raw: raw)

    gen_mod.generate_artifact(db, kind="tab", prompt="σκάλα Σολ", locale="el")
    assert SENTINEL in rec.sent


def test_the_ocr_prompt_override_reaches_the_vision_call(db):
    """OCR resolves ONCE per run, not per page — 888 pages is 888 queries, each one
    sitting between a commit and a vision call. So the snapshot is taken in
    `ocr_source` and threaded; this asserts the threading, at the only branch point."""
    from app.brain.ocr import _vision_task_for
    from app.models.knowledge import Page

    _override(db, "ocr.transcribe")
    page = Page(page_no=1, status="pending", ocr_reason=None)
    task = _vision_task_for(page, overrides.snapshot(db))
    assert task.mode == "transcribe"
    assert SENTINEL in task.prompt


def test_the_language_directive_override_reaches_every_prompt_it_is_injected_into(db):
    """`language_directive` is injected into 8 prompts. One override, all 8 — which
    is the property that makes it a shared fragment rather than eight copies."""
    from app.prompts import registry

    _override(db, "shared.language_directive")
    carriers = [
        p for p in registry.REGISTRY
        if any(s.name == "language_directive" for s in registry.render(p, "el", db).spans)
    ]
    assert len(carriers) >= 8, f"expected the directive in >=8 prompts, found {carriers}"
    for prompt_id in carriers:
        assert SENTINEL in registry.render(prompt_id, "el", db).text, prompt_id
