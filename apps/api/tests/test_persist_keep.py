"""`persist_lesson` writes AROUND the tutor's sections and keeps segment ids.

Two things a redraft used to do that made hand edits impossible to keep: delete
every non-custom segment (so a `tutor_edited` theory vanished) and recreate rows
(so an artifact pinned to a segment lost its parent)."""
import pytest
from sqlalchemy import text

from app.curriculum import blueprint as bp_mod
from app.curriculum.corpus import LibraryContext
from app.curriculum.depth import Measurement
from app.curriculum.draft import (
    LESSON_FIXED_SLICE_ID,  # noqa: F401  (the slice id the registry registers)
    LessonContext,
    build_lesson_messages,
    persist_lesson,
)
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
    # The constructor every other test in this suite uses for "no sources at all"
    # (`tests/test_student_brief.py:29`, `tests/test_blueprint_storage.py:30`).
    # Nothing DB-backed: `prefix_messages` renders the no-library branch.
    return LibraryContext(text="", token_count=0, fits=True)


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


def test_a_custom_survives_and_a_stray_section_less_row_does_not(drafted_lesson):
    """The two rows `persist_lesson` cannot address by blueprint key.

    A `meta.custom` segment is the tutor's own surgical addition and must outlive
    every redraft, ordered after the blueprint sections. A non-custom row with no
    `meta.section` (what `restore.restore_version` writes when the snapshot had
    none) has no key to be rewritten under — the delete-all this replaced took it,
    and leaving it behind would leave a row that is never rewritten, never removed
    and never re-ordered, colliding with a real section's slot.
    """
    db, lesson, segs = drafted_lesson
    custom = Block(kind="segment", title="Δικό μου", body="δικό μου", order=90,
                   parent_id=lesson.id, language="el",
                   meta={"section": "custom:diko-mou", "custom": True})
    stray = Block(kind="segment", title="Ορφανό", body="ορφανό", order=91,
                  parent_id=lesson.id, language="el", meta={})
    db.add_all([custom, stray]); db.commit()
    custom_id, stray_id = custom.id, stray.id

    drafted = {"title": "Μπράτσο", "summary": "νέα περίληψη"}
    for key in bp_mod.section_keys(bp_mod.default_blueprint()):
        drafted[key] = {"body": f"νέο {key}", "citations": []} if key not in ("exercises", "qa_prompts") \
            else {"body": f"νέο {key}", "items": [], "citations": []}
    m = Measurement(total_words=100, target=2750, floor=2200, per_section={}, thin_sections=[])
    persist_lesson(db, lesson, drafted, m, _empty_library(), bp_mod.default_blueprint(),
                   teaching_minutes=50)
    db.commit(); db.expire_all()

    rows = db.query(Block).filter_by(parent_id=lesson.id).all()
    assert db.get(Block, stray_id) is None
    kept_custom = db.get(Block, custom_id)
    assert kept_custom is not None and kept_custom.body == "δικό μου"
    assert kept_custom.order == max(r.order for r in rows)      # after the blueprint
    assert sorted(r.order for r in rows) == list(range(len(rows)))
