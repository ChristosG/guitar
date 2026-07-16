"""The chat→curriculum bridge (`curriculum/from_chat.py` + its endpoint).

What has to be true: the answer lands VERBATIM (no LLM call anywhere on the
path), under the module the tutor picked, as a `ready` lesson with one segment;
the chat turn's citations map into the board's provenance shape; and the
failure modes are 4xxs, not 500s.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.curriculum.corpus import build_library_context
from app.curriculum.outline import materialize_outline
from app.curriculum.shape import plan_shape
from app.main import app
from app.models.block import Block

from test_curriculum_draft_job import _book, _outline

client = TestClient(app)
SHAPE = plan_shape(8, 1, 50)

GREEK_ANSWER = (
    "Το overdrive είναι ο ήχος μιας λυχνίας που «σπάει» — ξεκινά καθαρός και "
    "παραμορφώνεται όσο ανεβαίνει η ένταση."
)


def _course(db) -> uuid.UUID:
    source = _book(db)
    return materialize_outline(
        db, _outline(), title="Tone", language="el", shape=SHAPE,
        library=build_library_context(db, [source.id]), source_ids=[source.id],
    )


def _first_module(db, root_id) -> Block:
    return db.scalars(
        select(Block).where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).first()


def test_the_answer_lands_verbatim_as_a_ready_lesson_with_provenance(db):
    root_id = _course(db)
    module = _first_module(db, root_id)
    before = len(module.children)

    r = client.post(f"/blocks/{module.id}/lessons/from-chat", json={
        "title": "Τι είναι το overdrive",
        "content": GREEK_ANSWER,
        "citations": [
            {"source_id": "abc", "source_title": "Book", "page_no": 12, "snippet": "…"},
            {"source_id": "abc", "source_title": "Book", "page_no": None},  # not linkable -> dropped
        ],
    })
    assert r.status_code == 201, r.text
    lesson_id = uuid.UUID(r.json()["id"])

    db.expire_all()
    lesson = db.get(Block, lesson_id)
    assert lesson.parent_id == module.id
    assert lesson.order == before  # appended
    meta = lesson.meta or {}
    assert meta["draft_status"] == "ready"   # the next Resume must NOT overwrite it
    assert meta["origin"] == "chat"
    assert meta["word_count"] == len(GREEK_ANSWER.split())

    segments = db.scalars(select(Block).where(Block.parent_id == lesson.id)).all()
    assert len(segments) == 1
    assert segments[0].body == GREEK_ANSWER          # verbatim — no model call
    assert segments[0].title == "Από τη συνομιλία"   # course language is el
    cites = (segments[0].meta or {})["citations"]
    assert cites == [{"source_id": "abc", "source_title": "Book", "page": 12}]


def test_the_bridge_rejects_bad_targets_and_empty_content(db):
    root_id = _course(db)
    module = _first_module(db, root_id)

    # A lesson is not a module.
    lesson = db.scalars(select(Block).where(Block.parent_id == module.id)).first()
    r = client.post(f"/blocks/{lesson.id}/lessons/from-chat",
                    json={"title": "x", "content": "y"})
    assert r.status_code == 422

    # Unknown module -> 404.
    r = client.post(f"/blocks/{uuid.uuid4()}/lessons/from-chat",
                    json={"title": "x", "content": "y"})
    assert r.status_code == 404

    # Whitespace-only content -> 422, nothing persisted.
    r = client.post(f"/blocks/{module.id}/lessons/from-chat",
                    json={"title": "x", "content": "   "})
    assert r.status_code == 422
