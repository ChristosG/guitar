"""The lesson panel's planner sees the WHOLE lesson — including the tutor's
before/after — and returns one verdict per section. The course-level revise
planner sees section titles only; that is why it could not do this."""
import pytest
from sqlalchemy import select, text

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
    keys = ["warm_up", "theory", "demonstration", "exercises", "common_mistakes", "recap",
            "homework", "qa_prompts"]
    for i, k in enumerate(keys):
        db.add(Block(kind="segment", title=k, body=f"κείμενο {k}", order=i, parent_id=lesson.id,
                     language="el", meta={"section": k}))
    db.flush()
    theory = db.scalars(select(Block).where(
        Block.parent_id == lesson.id, Block.meta["section"].as_string() == "theory")).one()
    tutor_edit.mark_tutor_edit(theory, previous_body=theory.body)
    theory.body = "Η Strat έχει τρεις μαγνήτες single-coil και tremolo."
    db.commit()
    yield db, course, module, lesson
    db.close()


SECTION_KEYS = ["warm_up", "theory", "demonstration", "exercises", "common_mistakes", "recap",
                "homework", "qa_prompts"]


def test_plan_messages_carry_full_bodies_before_after_and_neighbours(lesson, monkeypatch):
    db, course, module, les = lesson
    captured = {}

    class P:
        def guided_json(self, messages, schema, role):
            captured["messages"] = messages; captured["schema"] = schema; captured["role"] = role
            return {"summary": "ok", "note_to_tutor": "",
                    "sections": [{"section": k, "action": "rewrite" if k != "theory" else "keep",
                                  "reason": "r", "brief": "b"} for k in SECTION_KEYS]}

    import app.curriculum.lesson_ai as mod
    monkeypatch.setattr(mod, "get_provider", lambda: P())
    monkeypatch.setattr(mod, "ground_topic", lambda *a, **k: [])

    plan = lesson_ai.plan_lesson_change(db, les.id, instruction="Ενημέρωσε τις υπόλοιπες", note=None)

    text_ = captured["messages"][-1]["content"]
    assert "Η Strat έχει τρεις μαγνήτες" in text_          # current theory, whole
    assert "κείμενο theory" in text_                       # the before
    assert "Τα ξύλα" in text_ and "Μαγνήτες" in text_       # neighbours
    assert "κείμενο exercises" in text_                    # every section's body
    assert captured["role"] == "plan"
    assert "Ενημέρωσε τις υπόλοιπες" in text_[-600:]       # the instruction, last
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


def test_the_planner_prompt_carries_the_register_rules(lesson, monkeypatch):
    """Every content-writing prompt renders `curriculum_style`, `language_directive`
    and `answer_in` — the curriculum register rule. The planner's `brief` lines are
    read back to the tutor and become the instruction the drafter writes from, so a
    planner that drifts into English drags the lesson with it."""
    from app.i18n import answer_in, curriculum_style, language_directive

    db, course, module, les = lesson
    captured = {}

    class P:
        def guided_json(self, messages, schema, role):
            captured["messages"] = messages
            return {"summary": "ok", "note_to_tutor": "",
                    "sections": [{"section": k, "action": "keep", "reason": "", "brief": ""}
                                 for k in SECTION_KEYS]}

    import app.curriculum.lesson_ai as mod
    monkeypatch.setattr(mod, "get_provider", lambda: P())
    monkeypatch.setattr(mod, "ground_topic", lambda *a, **k: [])
    lesson_ai.plan_lesson_change(db, les.id, instruction="κάνε κάτι", note="και αυτό")

    system = captured["messages"][0]["content"]
    user = captured["messages"][-1]["content"]
    assert language_directive("el", db) in system
    assert curriculum_style("el", db) in system
    assert answer_in("el", db) in user
    assert "και αυτό" in user                                   # his extra note travels


def test_build_plan_messages_is_pure_and_caps_a_runaway_section():
    huge = "α" * (lesson_ai.SECTION_CHAR_LIMIT + 500)
    msgs = lesson_ai.build_plan_messages(
        course_title="Ήχος", course_brief=None, module_title="Μ", module_objective="",
        neighbours="PREVIOUS LESSON: —", lesson_title="Λ", lesson_objective="",
        blueprint_lines="- theory — Θεωρία — 0.20", sections=[{"section": "theory",
                                                               "title": "Θεωρία", "body": huge}],
        edited={}, retrieved=None, instruction="γράψε", note=None, language="el",
    )
    body = msgs[-1]["content"]
    assert "…[περικόπηκε]" in body
    assert "α" * (lesson_ai.SECTION_CHAR_LIMIT + 1) not in body
    # No edited block and no library block when there is nothing to show.
    assert "FROM HIS LIBRARY" not in body
    assert "EDITED BY HAND" not in body


def test_impact_uses_blueprint_weights_not_a_guess(lesson, monkeypatch):
    db, course, module, les = lesson
    bp = bp_mod.default_blueprint()
    weights = bp_mod.section_weights(bp)

    class P:
        def guided_json(self, messages, schema, role):
            return {"summary": "", "note_to_tutor": "",
                    "sections": [{"section": k,
                                  "action": "rewrite" if k in ("theory", "recap") else "keep",
                                  "reason": "", "brief": ""} for k in SECTION_KEYS]}

    import app.curriculum.lesson_ai as mod
    monkeypatch.setattr(mod, "get_provider", lambda: P())
    monkeypatch.setattr(mod, "ground_topic", lambda *a, **k: [])
    plan = lesson_ai.plan_lesson_change(db, les.id, instruction="x", note=None)
    expected = int(weights["theory"] * 2750) + int(weights["recap"] * 2750)
    assert plan["impact"] == {"rewrite_count": 2, "est_words": expected}


def test_retrieval_failure_does_not_sink_the_plan(lesson, monkeypatch):
    """Retrieval is the weak link; a planner that dies with it would make the
    panel unusable on a course whose library is not indexed."""
    db, course, module, les = lesson

    class P:
        def guided_json(self, messages, schema, role):
            return {"summary": "", "note_to_tutor": "", "sections": []}

    def boom(*a, **k):
        raise RuntimeError("no index")

    import app.curriculum.lesson_ai as mod
    monkeypatch.setattr(mod, "get_provider", lambda: P())
    monkeypatch.setattr(mod, "ground_topic", boom)
    plan = lesson_ai.plan_lesson_change(db, les.id, instruction="x", note=None)
    assert [s["action"] for s in plan["sections"]] == ["keep"] * len(SECTION_KEYS)
    assert plan["dropped"] == []


def test_the_planner_prompt_is_registered_and_renders(lesson):
    from app.prompts import registry

    entry = registry.REGISTRY["lesson.ai.plan"]
    assert entry.flow == "lesson" and entry.kind == "prompt"
    assert entry.language_from_course is True
    assert {s.id for s in entry.slices} == {"lesson.ai.plan", "lesson.ai.plan.user"}
    assert entry.source_of_truth() is lesson_ai.build_plan_messages
    rendered = registry.render("lesson.ai.plan", "el")
    assert "EDITED BY HAND" in rendered.text          # the sample lesson has one


def test_apply_rewrites_only_ticked_sections_keeps_ids_and_snapshots(lesson, monkeypatch):
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod
    captured = {}

    def fake_draft_lesson(db_, *, ctx, library, language, blueprint=None, student_brief=None,
                          course_brief=None, neighbours=None, tutor_brief=None, source_ids=None,
                          prompts=None, revise_current=None, fixed_sections=None,
                          exclude_sections=frozenset(), section_briefs=None,
                          repair_citations=True):
        captured.update(fixed=fixed_sections, exclude=set(exclude_sections), briefs=section_briefs,
                        objective=ctx.lesson_objective, neighbours=neighbours,
                        position=ctx.position)
        from app.curriculum.depth import Measurement
        drafted = {"title": les.title, "summary": "νέα περίληψη",
                   "warm_up": {"body": "νέο ζέσταμα", "citations": []},
                   "exercises": {"body": "νέες ασκήσεις", "items": [], "citations": []}}
        return drafted, Measurement(total_words=50, target=2750, floor=2200, per_section={}, thin_sections=[])
    monkeypatch.setattr(mod, "draft_lesson", fake_draft_lesson)

    before_ids = {(s.meta or {}).get("section"): s.id for s in
                  db.scalars(select(Block).where(Block.parent_id == les.id)).all()}
    out = lesson_ai.apply_lesson_change(
        db, les.id, instruction="Ενημέρωσε", note="πιο απλά",
        sections=[{"section": "warm_up", "brief": "αναφορά στη Strat"},
                  {"section": "exercises", "brief": "ασκήσεις με tremolo"}])
    db.expire_all()
    assert captured["exclude"] == {"theory", "demonstration", "common_mistakes", "recap",
                                   "homework", "qa_prompts"}
    assert "Η Strat έχει τρεις μαγνήτες" in captured["fixed"]["theory"]
    assert captured["briefs"] == {"warm_up": "αναφορά στη Strat", "exercises": "ασκήσεις με tremolo"}
    assert "Ενημέρωσε" in captured["objective"] and "πιο απλά" in captured["objective"]
    # Task 3.2: the neighbours are a parameter of their own now, and POSITION is a
    # position line again rather than the three neighbour lines stuffed into it.
    assert captured["neighbours"] == {"prev": "Τα ξύλα — ξύλα", "next": "Μαγνήτες — pickups",
                                      "siblings": "Τα ξύλα, Μαγνήτες"}
    assert captured["position"] == "lesson 2 of 3 in module"
    assert "ΠΡΟΗΓΟΥΜΕΝΟ" not in captured["position"] and "PREVIOUS" not in captured["position"]
    rows = {(s.meta or {}).get("section"): s for s in
            db.scalars(select(Block).where(Block.parent_id == les.id)).all()}
    assert rows["warm_up"].body == "νέο ζέσταμα" and rows["warm_up"].id == before_ids["warm_up"]
    assert rows["theory"].body.startswith("Η Strat") and "tutor_edited" in rows["theory"].meta
    assert rows["recap"].body == "κείμενο recap"
    l = db.get(Block, les.id)
    assert l.meta["draft_status"] == "ready"
    assert [s["section"] for s in l.meta["prev_segments"]][1] == "theory"     # snapshot taken
    assert l.meta["ai_instruction"].startswith("Ενημέρωσε")
    # NOT `revise_instruction`: the draft worker's `_claim` consumes that key as a
    # one-shot redraft instruction, so leaving it here would make the tutor's next
    # Deepen silently re-apply this panel's instruction to the whole lesson.
    assert "revise_instruction" not in l.meta
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


def test_the_section_briefs_block_renders_only_when_given(lesson):
    """The apply call's per-section briefs. Absent/empty must be byte-identical
    to before the parameter existed — the same pin `lesson.fixed` carries."""
    from app.curriculum.corpus import LibraryContext
    from app.curriculum.draft import LessonContext, build_lesson_messages

    lib = LibraryContext(text="", token_count=0, fits=True)
    ctx = LessonContext(lesson_title="Μ", lesson_objective="ο", module_title="Ξ", module_objective="",
                        course_title="Ή", tier="general_knowledge",
                        position="lesson 1 of 1 in module 1 of 1", minutes=50, teaching_minutes=50,
                        target_words=2750, floor_words=2200)
    base = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None,
                                 course_brief=None)
    same = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None,
                                 course_brief=None, section_briefs={})
    empty_values = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None,
                                         course_brief=None, section_briefs={"recap": ""})
    assert base[-1]["content"] == same[-1]["content"]
    # A plan whose every brief came back blank says nothing — and renders nothing.
    assert base[-1]["content"] == empty_values[-1]["content"]
    with_briefs = build_lesson_messages(ctx=ctx, library=lib, language="el", student_brief=None,
                                        course_brief=None,
                                        fixed_sections={"theory": "Ο σφένδαμος."},
                                        section_briefs={"recap": "πιο σύντομο", "homework": ""})
    text_ = with_briefs[-1]["content"]
    assert "πιο σύντομο" in text_
    assert '"homework"' not in text_                      # an empty brief says nothing
    assert text_.index("Ο σφένδαμος.") < text_.index("πιο σύντομο")   # after the fixed block
    assert "πιο σύντομο" not in empty_values[-1]["content"]


def test_the_section_briefs_fragment_is_registered_and_renders(lesson):
    from app.prompts import registry

    entry = registry.REGISTRY["lesson.section_briefs"]
    assert entry.flow == "lesson" and entry.kind == "fragment"
    assert {s.id for s in entry.slices} == {"lesson.section_briefs"}
    rendered = registry.render("lesson.section_briefs", "el")
    assert "ξαναγράφεις" in rendered.text


def _fake_draft_lesson(captured, les):
    """The real `draft_lesson`'s signature, minus the model. Kept in one place so
    a parameter added to the live function fails these tests loudly rather than
    being swallowed by a `**kwargs` stand-in."""
    def fake(db_, *, ctx, library, language, blueprint=None, student_brief=None,
             course_brief=None, neighbours=None, tutor_brief=None, source_ids=None,
             prompts=None, revise_current=None, fixed_sections=None,
             exclude_sections=frozenset(), section_briefs=None,
             repair_citations=True):
        captured.update(fixed=fixed_sections, exclude=set(exclude_sections),
                        briefs=section_briefs, objective=ctx.lesson_objective,
                        neighbours=neighbours, position=ctx.position,
                        tutor_brief=tutor_brief, repair_citations=repair_citations)
        from app.curriculum.depth import Measurement
        return ({"title": les.title, "summary": "νέα περίληψη",
                 "warm_up": {"body": "νέο ζέσταμα", "citations": []}},
                Measurement(total_words=50, target=2750, floor=2200, per_section={},
                            thin_sections=[]))
    return fake


def test_apply_excludes_every_unticked_blueprint_section_even_with_no_row(lesson, monkeypatch):
    """A blueprint key the course has enabled but THIS lesson has no row for is in
    neither `known` nor `keep`. Left in the schema, the model writes a whole section
    nobody ticked and the `rewritten` list read back to the tutor does not mention
    it — so the exclusion is computed from the blueprint, not from the rows."""
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod
    for seg in db.scalars(select(Block).where(Block.parent_id == les.id)).all():
        if (seg.meta or {}).get("section") not in ("warm_up", "theory", "recap"):
            db.delete(seg)
    db.commit()
    captured = {}
    monkeypatch.setattr(mod, "draft_lesson", _fake_draft_lesson(captured, les))

    out = lesson_ai.apply_lesson_change(db, les.id, instruction="Ενημέρωσε", note=None,
                                        sections=[{"section": "warm_up", "brief": "πιο σύντομο"}])

    enabled = {s["key"] for s in bp_mod.enabled_sections(bp_mod.default_blueprint())}
    assert captured["exclude"] == enabled - {"warm_up"}
    assert "demonstration" in captured["exclude"]      # enabled, no row, not ticked
    assert out["rewritten"] == ["warm_up"]


def test_apply_carries_a_brief_born_lesson_s_own_brief_into_the_draft(lesson, monkeypatch):
    """A lesson created by «Προσθήκη μαθήματος» carries the tutor's brief for it on
    `meta.brief`, and `draft.LESSON_TUTOR_BRIEF_BLOCK` renders it whole. The panel's
    apply is a DRAFT of that same lesson — dropping the brief here would rewrite it
    without the sentence that asked for it in the first place."""
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod

    brief = "Μόνο για το μπράτσο και πώς αλλάζει τον ήχο."
    les.meta = {**(les.meta or {}), "brief": brief}
    db.commit()
    captured = {}
    monkeypatch.setattr(mod, "draft_lesson", _fake_draft_lesson(captured, les))

    lesson_ai.apply_lesson_change(db, les.id, instruction="Ενημέρωσε", note=None,
                                  sections=[{"section": "recap", "brief": ""}])

    assert captured["tutor_brief"] == brief


def test_apply_passes_no_tutor_brief_for_an_ordinary_lesson(lesson, monkeypatch):
    """And a lesson with no brief passes None — not "", which would render an empty
    block telling the model the tutor asked for nothing in particular."""
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod

    captured = {}
    monkeypatch.setattr(mod, "draft_lesson", _fake_draft_lesson(captured, les))
    lesson_ai.apply_lesson_change(db, les.id, instruction="Ενημέρωσε", note=None,
                                  sections=[{"section": "recap", "brief": ""}])
    assert captured["tutor_brief"] is None


def test_apply_leaves_the_lesson_ready_when_the_library_read_fails(lesson, monkeypatch):
    """The claim is the LAST thing before the model call. A library read that blew
    up after it would leave the lesson stranded at `drafting` with no error — a
    spinner for a call that was never made."""
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod

    def boom(*a, **k):
        raise RuntimeError("no index")
    monkeypatch.setattr(mod, "build_retrieval_context", boom)
    monkeypatch.setattr(mod, "draft_lesson", _fake_draft_lesson({}, les))
    with pytest.raises(RuntimeError):
        lesson_ai.apply_lesson_change(db, les.id, instruction="x", note=None,
                                      sections=[{"section": "recap", "brief": ""}])
    db.rollback(); db.expire_all()
    meta = db.get(Block, les.id).meta
    assert meta["draft_status"] == "ready"
    assert "prev_segments" not in meta                 # nothing was claimed at all


def test_apply_refuses_a_lesson_that_is_already_drafting(lesson):
    db, course, module, les = lesson
    les.meta = {**(les.meta or {}), "draft_status": "drafting"}
    db.commit()
    with pytest.raises(lesson_ai.LessonAiError):
        lesson_ai.apply_lesson_change(db, les.id, instruction="x", note=None,
                                      sections=[{"section": "recap", "brief": ""}])


def test_apply_never_buys_a_citation_repair_re_draft(lesson, monkeypatch):
    """Task 4.3 Fix A. Live, 2026-09-12: an apply drafted for 390s, the model
    cited one page it had never been shown, and the repair retry drafted the whole
    lesson AGAIN — 726s, 19 minutes in total, past the panel's poll cap, so the
    tutor watched «Το AI δουλεύει ακόμα» and saw nothing change.

    On THIS path the second call buys nothing: the grounding is per-lesson
    retrieval, the tutor's own text is the authority, and `strip_invalid_citations`
    — where the repair already falls back to — keeps the prose and drops the false
    chip. Every other caller (the fan-out, which drafts from the whole library and
    where a clickable page number is the point) keeps the default."""
    db, course, module, les = lesson
    import app.curriculum.lesson_ai as mod

    captured = {}
    monkeypatch.setattr(mod, "draft_lesson", _fake_draft_lesson(captured, les))

    lesson_ai.apply_lesson_change(db, les.id, instruction="Ενημέρωσε", note=None,
                                  sections=[{"section": "recap", "brief": ""}])

    assert captured["repair_citations"] is False
