"""Tool results reach the model as the tutor wrote them, and never unbounded.

2026-09-11: `get_curriculum` on a 25-lesson Greek course returned 488,708 chars
of prose; `json.dumps(..., ensure_ascii=True)` made that 2,310,878 chars of
`\\uXXXX`, and the next model call was "~1,826,053 tokens (limit 1,000,000)".
"""
from app.agent import loop
from app.routers import chat


def test_chat_router_shares_loops_stringify():
    # 2026-09-12 review: chat.py had its own `_stringify` copy — "mirrors
    # loop.py's own `_stringify` exactly" — that never got the ensure_ascii
    # fix or the cap, so the mutation-approval path still built unbounded,
    # ASCII-escaped tool messages. One source of truth now: chat.py imports
    # loop's `_stringify` rather than defining its own.
    assert chat._stringify is loop._stringify


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
