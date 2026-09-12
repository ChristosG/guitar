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


def test_refine_clears_the_tutor_marker(lesson_with_two_segments, monkeypatch):
    from app.curriculum import refine as refine_mod
    db, lesson, theory, warm = lesson_with_two_segments
    tutor_edit.mark_tutor_edit(theory, previous_body=theory.body); db.commit()

    class P:
        def guided_json(self, messages, schema, role="draft"):
            return {"title": "Θεωρία", "body": "Ξαναγραμμένο από το AI."}
    monkeypatch.setattr(refine_mod, "get_provider", lambda: P())
    # `search` is imported LOCALLY inside `refine_block` (`from app.brain.retrieve
    # import search`), so this patch never actually intercepts the call — it is
    # here (with `raising=False`, matching `test_curriculum_editing.py`'s same
    # patch of a name `refine_mod` doesn't hold) only so a future refactor that
    # promotes the import to module scope doesn't silently start hitting the real
    # embedder. The real `search()` runs against this test's empty corpus, returns
    # no hits (or raises into `refine_block`'s own try/except), and either way
    # `context` ends up `None` — irrelevant to what this test checks.
    monkeypatch.setattr(refine_mod, "search", lambda *a, **k: [], raising=False)
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
    # Real signature is `generate_segment(db, segment: Block) -> None`, mutates
    # and does NOT commit (same contract as `refine_block`) — the caller commits.
    sg.generate_segment(db, warm)
    db.commit()
    db.expire_all()
    assert "tutor_edited" not in db.get(Block, warm.id).meta
