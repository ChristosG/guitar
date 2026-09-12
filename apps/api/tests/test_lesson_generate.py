"""«Προσθήκη μαθήματος» WITH A BRIEF: one planning call against the module's own
siblings, then the ordinary draft — for ONE lesson.

What has to be true:

  * The planner sees what the module already teaches (its lessons, in order) and
    where the module sits in the course, so the new lesson neither repeats a
    sibling nor floats free of the arc. The tutor's words go in whole.
  * A title the tutor typed WINS. He is not asking the model to name it.
  * The lesson lands `queued` with `meta.brief` — Task 3.2's drafter renders that
    brief verbatim, so the brief must survive the plan, not be spent by it.
  * The chained draft is narrowed to THIS lesson (`params.lesson_ids`), not the
    whole course: the tutor added one lesson, not a redraft.
"""
import uuid

import pytest
from sqlalchemy import select, text

import app.curriculum.corpus as corpus_mod
import app.curriculum.extend as extend_mod
import app.jobs.lesson_generate as job_mod
from app.curriculum import blueprint as bp_mod
from app.curriculum.corpus import LibraryContext
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.block import Block
from app.models.generation_job import GenerationJob

from fastapi.testclient import TestClient

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip("database not reachable", allow_module_level=True)


client = TestClient(app)

BRIEF = "Θέλω ένα μάθημα μόνο για το μπράτσο και πώς αλλάζει τον ήχο."
EMPTY_LIBRARY = LibraryContext(text="", token_count=0, fits=True)


def setup_module(_):
    Base.metadata.create_all(engine)


@pytest.fixture
def tree():
    """A course of two modules; the first has two lessons, the second one — so a
    sibling list, a course map and a foreign lesson all exist."""
    db = SessionLocal()
    course = Block(kind="course", title="Ήχος", language="el", is_template=True,
                   meta={"brief": "Θέλω ένα course για guitar tone", "source_ids": None,
                         "blueprint": bp_mod.default_blueprint(),
                         "shape": {"minutes_per_lesson": 50,
                                   "target_words_per_lesson": 2750}})
    db.add(course); db.flush()
    module = Block(kind="module", title="Η Κιθάρα ως Πηγή", parent_id=course.id, order=0,
                   language="el", meta={"objective": "το όργανο", "tier": "library"})
    other = Block(kind="module", title="Ενισχυτές και λυχνίες", parent_id=course.id, order=1,
                  language="el", meta={"objective": "ο ενισχυτής", "tier": "library"})
    db.add_all([module, other]); db.flush()
    l1 = Block(kind="lesson", title="Τα ξύλα", parent_id=module.id, order=0, language="el",
               meta={"objective": "ξύλα", "draft_status": "ready"})
    l2 = Block(kind="lesson", title="Μαγνήτες", parent_id=module.id, order=1, language="el",
               meta={"objective": "pickups", "draft_status": "ready"})
    foreign = Block(kind="lesson", title="Προενισχυτής", parent_id=other.id, order=0,
                    language="el", meta={"objective": "preamp", "draft_status": "ready"})
    db.add_all([l1, l2, foreign])
    db.commit()
    yield db, course, module, other, l1, l2, foreign
    db.close()


class _Provider:
    """Captures the planning call and answers it with one lesson."""

    def __init__(self, planned=None):
        self.planned = planned or {"title": "Το μπράτσο", "objective": "Τι κάνει το μπράτσο.",
                                   "est_minutes": 50}
        self.messages = None
        self.schema = None
        self.role = None

    def guided_json(self, messages, schema, *, temperature=0.2, role="spec", max_tokens=None):
        self.messages = messages
        self.schema = schema
        self.role = role
        return dict(self.planned)

    def count_tokens(self, text_: str) -> int:
        return len(text_) // 3 + 1


@pytest.fixture
def provider(monkeypatch):
    p = _Provider()
    monkeypatch.setattr(extend_mod, "get_provider", lambda: p)
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: p)
    return p


def _lessons(db, module_id) -> list[Block]:
    return db.scalars(
        select(Block)
        .where(Block.parent_id == module_id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_plan_messages_show_siblings_course_map_and_the_brief(tree, provider):
    db, course, module, other, l1, l2, foreign = tree

    planned = extend_mod.plan_lesson_json(
        db, course=course, module=module, library=EMPTY_LIBRARY, brief=BRIEF, title=None,
    )

    assert planned["title"] == "Το μπράτσο"
    tail = provider.messages[-1]["content"]
    # The siblings, by title — the anti-duplication input.
    assert "Τα ξύλα" in tail and "Μαγνήτες" in tail
    # The course map: the OTHER modules are there too, so the lesson can build on
    # what comes before it instead of only on its own module.
    assert "Ενισχυτές και λυχνίες" in tail and "Η Κιθάρα ως Πηγή" in tail
    # The tutor's words, whole, under the heading that says they are his.
    assert BRIEF in tail.split("ΤΟ ΜΑΘΗΜΑ ΠΟΥ ΖΗΤΗΣΕ")[1]
    assert provider.role == "plan"
    assert provider.schema is extend_mod.LESSON_PLAN_SCHEMA_ONE


def test_the_lesson_planner_carries_the_register_rules(tree, provider):
    """Every content-writing prompt renders `curriculum_style`, `language_directive`
    and `answer_in` — the title and objective it returns land on the board verbatim."""
    from app.i18n import answer_in, curriculum_style, language_directive

    db, course, module, other, l1, l2, foreign = tree
    extend_mod.plan_lesson_json(db, course=course, module=module, library=EMPTY_LIBRARY,
                                brief=BRIEF, title=None)
    tail = provider.messages[-1]["content"]
    assert language_directive("el", db) in tail
    assert curriculum_style("el", db) in tail
    assert answer_in("el", db) in tail


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------

def test_generate_lesson_adds_queued_lesson_with_brief_and_position(tree, provider):
    db, course, module, other, l1, l2, foreign = tree

    lesson = extend_mod.generate_lesson(db, module.id, brief=BRIEF, title=None, after=l1.id)

    assert lesson.title == "Το μπράτσο"
    assert lesson.order == 1
    assert [l.title for l in _lessons(db, module.id)] == ["Τα ξύλα", "Το μπράτσο", "Μαγνήτες"]
    meta = lesson.meta or {}
    assert meta["brief"] == BRIEF
    assert meta["objective"] == "Τι κάνει το μπράτσο."
    assert meta["draft_status"] == "queued"
    assert meta["added_by_tutor"] is True
    assert lesson.est_minutes == 50


def test_tutor_title_wins(tree, provider):
    db, course, module, other, l1, l2, foreign = tree

    lesson = extend_mod.generate_lesson(db, module.id, brief=BRIEF,
                                        title="Το μπράτσο και ο ήχος", after=None)

    assert lesson.title == "Το μπράτσο και ο ήχος"
    assert lesson.order == 2          # appended
    # And the model was TOLD to keep it, rather than being silently overruled.
    assert "Το μπράτσο και ο ήχος" in provider.messages[-1]["content"]


def test_generate_lesson_rejects_a_non_module(tree, provider):
    db, course, module, other, l1, l2, foreign = tree
    with pytest.raises(extend_mod.ExtendError):
        extend_mod.generate_lesson(db, course.id, brief=BRIEF, title=None, after=None)


def test_generate_lesson_wraps_a_foreign_after_as_an_extend_error(tree, provider):
    db, course, module, other, l1, l2, foreign = tree
    with pytest.raises(extend_mod.ExtendError):
        extend_mod.generate_lesson(db, module.id, brief=BRIEF, title=None, after=foreign.id)


# ---------------------------------------------------------------------------
# The route and the chain
# ---------------------------------------------------------------------------

def test_route_enqueues_and_job_chains_a_single_lesson_draft(tree, monkeypatch):
    db, course, module, other, l1, l2, foreign = tree
    from app.curriculum.edit import _add_lesson

    def fake_generate_lesson(db_, module_id, *, brief, title, after):
        lesson = _add_lesson(db_, module_id, title=title or "Το μπράτσο",
                             objective="Τι κάνει το μπράτσο.", after=after)
        lesson.meta = {**(lesson.meta or {}), "brief": brief}
        db_.commit()
        db_.refresh(lesson)
        return lesson

    recorded: dict = {}

    def fake_run_draft(draft_job_id):
        s = SessionLocal()
        try:
            recorded.update(s.get(GenerationJob, draft_job_id).params)
        finally:
            s.close()

    monkeypatch.setattr(job_mod, "generate_lesson", fake_generate_lesson)
    monkeypatch.setattr(job_mod, "run_curriculum_draft_job", fake_run_draft)

    r = client.post(f"/blocks/{module.id}/lessons/generate", json={"brief": BRIEF})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    body = client.get(f"/jobs/{job_id}").json()
    assert body["status"] == "succeeded"
    lesson_id = body["progress"]["lesson_id"]
    assert lesson_id
    assert body["progress"]["draft_job_id"]

    db.expire_all()
    lesson = db.get(Block, uuid.UUID(lesson_id))
    assert (lesson.meta or {})["brief"] == BRIEF
    # THE CHAIN IS NARROWED TO THIS LESSON — he added one lesson, not a redraft.
    assert recorded == {"root_id": str(course.id), "lesson_ids": [lesson_id]}


def test_route_422_when_after_is_not_a_sibling(tree):
    db, course, module, other, l1, l2, foreign = tree

    r = client.post(f"/blocks/{module.id}/lessons/generate",
                    json={"brief": BRIEF, "after": str(foreign.id)})
    assert r.status_code == 422

    # A brief too short to steer anything is refused by the schema, not the model.
    r = client.post(f"/blocks/{module.id}/lessons/generate", json={"brief": "λίγα"})
    assert r.status_code == 422

    # And the route is about MODULES.
    r = client.post(f"/blocks/{l1.id}/lessons/generate", json={"brief": BRIEF})
    assert r.status_code == 404


def test_the_lesson_planner_prompt_is_registered(tree):
    from app.prompts import registry

    entry = registry.REGISTRY["curriculum.extend.lesson"]
    assert entry.kind == "prompt"
    assert entry.flow == registry.REGISTRY["curriculum.extend"].flow
    assert {s.id for s in entry.slices} == {extend_mod.LESSON_PLAN_SLICE_ID}
    rendered = registry.render("curriculum.extend.lesson", "el")
    assert "design ONE new lesson" in rendered.text
