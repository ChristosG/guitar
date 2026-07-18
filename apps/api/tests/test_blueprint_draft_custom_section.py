"""EXTRA GUARD TEST (flagged in the T3-4 review, closed alongside Task 8).

Every one of `draft_lesson`'s three `blueprint=` parameters — in
`jobs/curriculum_draft.py:_draft_one`, in the redraft route's rescheduled job,
and in `persist_lesson` — silently defaults to `blueprint.default_blueprint()`
if the kwarg is ever dropped. That is exactly the failure invariant #2 exists to
make SAFE for a blueprintless course and exactly the failure this test exists to
catch for a course that DOES carry a custom one: without this, a future edit
that drops a `blueprint=` kwarg anywhere on the draft path would silently draft
every course under the stock 8-section skeleton, no error, no red test — the
tutor's custom section would simply never appear, forever.

So this runs the REAL `run_curriculum_draft_job` — the same fan-out the redraft
route (Task 8) reschedules — against a course whose `meta["blueprint"]` carries
a section the code default does not have (an extra prose section, "fx_loops"),
and proves BOTH halves:

  (a) the schema the provider is actually asked to fill (`build_lesson_schema`,
      called inside `draft_lesson`) includes the custom section — proving the
      MODEL was told about it, not just that the database has an opinion, and
  (b) `persist_lesson` writes a segment LABELLED from the custom blueprint (its
      own el/en label, not a code constant) — proving the persisted lesson
      actually reflects the course's blueprint end to end.
"""
import uuid

from sqlalchemy import select

from app.curriculum.blueprint import default_blueprint
from app.curriculum.corpus import build_library_context
from app.curriculum.depth import SECTION_WEIGHTS, SECTIONS
from app.curriculum.outline import materialize_outline
from app.curriculum.shape import plan_shape
from app.jobs.curriculum_draft import run_curriculum_draft_job
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.models.knowledge import KnowledgeSource, Page

SHAPE = plan_shape(2, 1, 50)  # 2 lessons / 1 module x 2

CUSTOM_SECTION_KEY = "fx_loops"
CUSTOM_SECTION_LABEL = {"el": "Βρόχοι εφέ", "en": "FX Loops"}
CUSTOM_SECTION_DESCRIPTION = "How the pedals sit in the amp's effects loop, and why it matters."


def _custom_blueprint() -> dict:
    """The code default PLUS one prose section the code has never heard of —
    the whole point being that nothing in `depth.py`/`draft.py` could produce
    this shape from its own constants."""
    bp = default_blueprint()
    bp["sections"].append({
        "key": CUSTOM_SECTION_KEY,
        "label": dict(CUSTOM_SECTION_LABEL),
        "description": CUSTOM_SECTION_DESCRIPTION,
        "weight": 0.05,
        "kind": "prose",
        "audience": "teacher",
        "enabled": True,
    })
    return bp


def _full_lesson_with_custom_section(title="Drafted") -> dict:
    """A fully-populated lesson dict covering every DEFAULT section (so `measure`
    is happy) PLUS the custom `fx_loops` section — what a well-behaved model
    would return against the schema this blueprint builds."""
    lesson = {"title": title, "summary": "Two sentences."}
    for name in SECTIONS:
        n = int(SECTION_WEIGHTS[name] * 2200)
        section = {"body": " ".join(["word"] * n), "citations": []}
        if name == "exercises":
            section["items"] = []
        if name == "qa_prompts":
            section["items"] = [{"question": "q", "answer_key": "a"}]
        lesson[name] = section
    lesson[CUSTOM_SECTION_KEY] = {
        "body": "Run the delay and reverb in the loop, drive up front.",
        "citations": [],
    }
    return lesson


class _SchemaCapturingProvider:
    """Records every schema it was asked to fill, and always answers with the
    custom-section-carrying lesson — proving the draft path both ASKED FOR and
    RECEIVED the custom section."""

    def __init__(self):
        self.schemas: list[dict] = []
        self.calls = 0

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.schemas.append(schema)
        self.calls += 1
        return _full_lesson_with_custom_section()

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


def _book(db) -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title="Book", language="en", status="ready")
    db.add(source)
    db.flush()
    db.add(Page(source_id=source.id, page_no=19,
                text="A humbucker cancels hum by pairing opposed coils. " * 4,
                status="ready"))
    db.commit()
    return source


def _course_with_custom_blueprint(db) -> uuid.UUID:
    source = _book(db)
    outline = {
        "title": "Tone Fundamentals",
        "modules": [{
            "title": "Module 0", "objective": "o", "tier": "library",
            "coverage_note": "p.19",
            "lessons": [
                {"title": f"L0.{j}", "objective": "o", "est_minutes": 50} for j in range(2)
            ],
        }],
    }
    return materialize_outline(
        db, outline, title="Tone Fundamentals", language="en", shape=SHAPE,
        library=build_library_context(db, [source.id]), source_ids=[source.id],
        blueprint=_custom_blueprint(),
    )


def _job(db, root_id) -> uuid.UUID:
    job = GenerationJob(
        kind="curriculum_draft", status="pending", params={"root_id": str(root_id)},
    )
    db.add(job)
    db.commit()
    return job.id


def test_the_course_s_own_blueprint_is_what_gets_drafted_from_not_the_code_default(db, monkeypatch):
    import app.curriculum.corpus as corpus_mod
    import app.curriculum.draft as draft_mod

    provider = _SchemaCapturingProvider()
    monkeypatch.setattr(draft_mod, "get_provider", lambda: provider)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: provider)

    root_id = _course_with_custom_blueprint(db)

    # Sanity: the course really did freeze the custom blueprint onto its own
    # meta at materialize time (invariant #3's per-course copy).
    db.expire_all()
    course = db.get(Block, root_id)
    frozen_keys = {s["key"] for s in course.meta["blueprint"]["sections"]}
    assert CUSTOM_SECTION_KEY in frozen_keys

    run_curriculum_draft_job(_job(db, root_id))

    # (a) THE MODEL WAS ASKED FOR IT: the schema `draft_lesson` built — not the
    # code default's 8 keys — includes the custom section.
    assert provider.calls == 2, "both lessons should have been drafted"
    for schema in provider.schemas:
        assert CUSTOM_SECTION_KEY in schema["properties"], (
            "the draft schema did not include the course's custom blueprint "
            "section — some caller on the draft path dropped `blueprint=` and "
            "fell back to the code default"
        )

    # (b) THE PERSISTED LESSON REFLECTS IT: `persist_lesson` wrote a segment
    # labelled from the CUSTOM blueprint's own el/en label, not a code constant
    # depth.py/draft.py have never heard of.
    db.expire_all()
    module = db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
    ).one()
    lessons = db.scalars(
        select(Block).where(Block.parent_id == module.id, Block.kind == "lesson")
    ).all()
    assert lessons and all(l.meta["draft_status"] == "ready" for l in lessons)

    custom_segment = None
    for lesson in lessons:
        segments = db.scalars(select(Block).where(Block.parent_id == lesson.id)).all()
        custom_segment = next(
            (s for s in segments if (s.meta or {}).get("section") == CUSTOM_SECTION_KEY), None,
        )
        if custom_segment:
            break

    assert custom_segment is not None, (
        "no persisted segment carries the custom section's key — persist_lesson "
        "is not reading this course's own blueprint"
    )
    assert custom_segment.title == CUSTOM_SECTION_LABEL["en"], (
        "the segment's title did not come from the custom blueprint's own label"
    )
    assert "delay and reverb" in custom_segment.body
