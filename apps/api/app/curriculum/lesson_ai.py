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

THE HONESTY CONTRACT IS `revise.validate_ops`', deliberately: a model verdict
for a section this lesson does not have is DROPPED with a reason the tutor can
read, never silently ignored, and a section the model forgot to mention defaults
to `keep` rather than vanishing from the plan. A plan card that quietly shrinks
is the same failure as a fabricated citation — it is a screen he trusts that is
wrong.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select

from app.curriculum import blueprint as bp_mod
from app.curriculum.corpus import build_retrieval_context
from app.curriculum.depth import count_words, floor_words, target_words
from app.curriculum.draft import LessonContext, draft_lesson, persist_lesson
from app.curriculum.ground import ground_topic
from app.curriculum.neighbours import neighbours_of
from app.curriculum.outline import TIER_GENERAL
from app.curriculum.restore import snapshot_of
from app.curriculum.tutor_edit import recompute_lesson_words, tutor_edited_sections
from app.i18n import answer_in, curriculum_style, language_directive
from app.jobs.curriculum_draft import _lesson_size  # the ONE sizing rule, shared with the fan-out
from app.llm.factory import get_provider
from app.models.block import Block
from app.prompts import overrides
from app.prompts.overrides import resolve

log = logging.getLogger(__name__)

# Per-section truncation. A section is shown WHOLE — that is the entire point of
# this planner — but a runaway body (a paste, a drafting loop that never stopped)
# must not be able to blow the planning call's context on its own.
SECTION_CHAR_LIMIT = 20_000
PLAN_RETRIEVAL_K = 6

# The minutes assumed when a course carries no `shape` — only ever reached by a
# lesson that ALSO has no `target_words` of its own. `depth.target_words` turns
# it into words; this module does not own a second words-per-minute constant,
# because two of those disagreeing is how an estimate starts lying quietly.
DEFAULT_MINUTES_PER_LESSON = 50


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


def sections_json(sections: list[dict]) -> str:
    """The lesson's sections as the model sees them. Public because the Settings
    preview interpolates the SAME text as a span — a second `json.dumps` over
    there would drift from this one's indent the day either changes, and the
    chip would stop landing on the text it claims to label."""
    return json.dumps(
        [{"section": s["section"], "title": s["title"], "body": _cap(s["body"])} for s in sections],
        ensure_ascii=False, indent=1,
    )


def edited_json(edited: dict[str, dict]) -> str:
    """The tutor's before/after, as the model sees it. Only `prev_body` — the
    CURRENT text is already in `sections_json`, and sending it twice would spend
    the lesson's tokens saying the same thing."""
    return json.dumps(
        {k: {"before": _cap(v.get("prev_body"))} for k, v in edited.items()},
        ensure_ascii=False, indent=1,
    )


def build_plan_messages(
    *, course_title: str, course_brief: str | None, module_title: str, module_objective: str,
    neighbours: str, lesson_title: str, lesson_objective: str, blueprint_lines: str,
    sections: list[dict], edited: dict[str, dict], retrieved: str | None,
    instruction: str, note: str | None, language: str, source=None,
) -> list[dict]:
    """Pure. `sections` = [{section, title, body}], `edited` = {section: {prev_body}}.

    The tutor's instruction is the LAST thing the model reads before the "one
    entry per section" rule — the same recency argument `build_segment_messages`
    and `build_refine_messages` make. `ensure_ascii=False` on both dumps: his
    lesson is Greek, and `\\u03b8...` in a prompt is a lesson the model has to
    decode before it can read it.
    """
    system = resolve(source, LESSON_AI_PLAN_SLICE_ID, LESSON_AI_PLAN_SYSTEM).format(
        language_directive=language_directive(language, source),
        style_directive=curriculum_style(language, source),
    )
    sections_text = sections_json(sections)
    edited_block = LESSON_AI_EDITED_BLOCK.format(edited=edited_json(edited)) if edited else ""
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


def _siblings(db, lesson: Block) -> tuple[list[Block], int | None]:
    """The module's lessons in teaching order, and where this one sits in them."""
    siblings = db.scalars(
        select(Block).where(Block.parent_id == lesson.parent_id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()
    return list(siblings), next((i for i, s in enumerate(siblings) if s.id == lesson.id), None)


def neighbours_dict(db, lesson: Block) -> dict[str, str]:
    """`{"prev", "next", "siblings"}` for ONE lesson — what the lesson before it
    already taught and what the one after it is going to.

    The dict IS the interface now (Task 3.2): `draft.build_lesson_messages` takes
    it and renders `LESSON_NEIGHBOURS_BLOCK` from it, so the apply path and the
    drafting fan-out show the model the same three lines. `neighbours_text` below
    is the same data rendered for the PLAN prompt, which interpolates one string.
    """
    siblings, idx = _siblings(db, lesson)
    return neighbours_of(siblings, idx)


def neighbours_text(db, lesson: Block) -> str:
    """The plan prompt's `{neighbours}` — `neighbours_dict` as one block of text.

    Byte-identical to what this function returned when it did the walking itself:
    `LESSON_AI_PLAN_USER` interpolates a single string, and the planner's prompt is
    not what this task set out to change.
    """
    n = neighbours_dict(db, lesson)
    return (f"PREVIOUS LESSON: {n['prev']}\nNEXT LESSON: {n['next']}\n"
            f"OTHER LESSONS IN THIS MODULE: {n['siblings']}")


def position_text(db, lesson: Block) -> str:
    """«lesson 2 of 4 in module» — the POSITION line for a single-lesson call.

    No module index: this path knows the lesson's module, not how many modules the
    course has, and counting them would be a query to say something the neighbours
    block says better.
    """
    siblings, idx = _siblings(db, lesson)
    return f"lesson {(idx or 0) + 1} of {len(siblings) or 1} in module"


def _lesson_sections(db, lesson: Block) -> list[dict]:
    segs = db.scalars(
        select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment").order_by(Block.order)
    ).all()
    return [{"section": (s.meta or {}).get("section") or s.title, "title": s.title,
             "body": s.body or "", "tutor_edited": bool((s.meta or {}).get("tutor_edited")),
             "id": str(s.id)} for s in segs]


def _blueprint_lines(bp: dict, language: str, keys: set[str] | frozenset[str]) -> str:
    """The blueprint's enabled sections, FILTERED to the keys this lesson actually
    has rows for.

    `keys` is not optional and the filter is not cosmetic. The prompt ends with
    "return one entry per section key listed above — no more, no fewer", and the
    block right below these lines is the lesson's ACTUAL sections. A course whose
    blueprint gained a section after this lesson was drafted (or a lesson drafted
    before a section was enabled) would otherwise list a key with no text under
    it and then demand a verdict on it — the model either invents one, which
    `validate_plan` drops as unknown, or obeys the sections block and is "wrong"
    about the count. Two instructions that cannot both be satisfied is a prompt
    that teaches the model to pick one, which is not a thing to leave lying in a
    planner the tutor reads the output of.
    """
    labels = bp_mod.section_labels(bp, language if language in ("el", "en") else "el")
    return "\n".join(f"- {s['key']} — {labels.get(s['key'], s['key'])} — {s.get('weight', 0):.2f}"
                     for s in bp_mod.enabled_sections(bp) if s["key"] in keys)


def validate_plan(raw: dict, existing: list[dict]) -> dict:
    """Every existing section exactly once; unknown keys dropped with a reason;
    missing ones default to keep. Same honesty contract as `revise.validate_ops`.

    Defaulting to KEEP rather than to rewrite is the safe direction: a section the
    model forgot about is a section nobody asked to change, and a plan that
    silently ticks it would rewrite the tutor's text on a model omission.
    """
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
    """What approving this plan costs, in the only two numbers the tutor can act
    on: how many sections get rewritten, and roughly how many words that is. The
    estimate comes from the BLUEPRINT weights × the lesson's target — the same
    arithmetic the drafter writes to — and falls back to the section's current
    length for a key the blueprint no longer carries."""
    weights = bp_mod.section_weights(bp)
    rewrite = [s["section"] for s in plan["sections"] if s["action"] == "rewrite"]
    est = 0
    for key in rewrite:
        w = weights.get(key)
        est += (int(w * target_words) if w
                else count_words(next((x["body"] for x in sections if x["section"] == key), "")))
    return {"rewrite_count": len(rewrite), "est_words": est}


def _lesson_or_raise(db, lesson_id: uuid.UUID) -> tuple[Block, Block, Block]:
    lesson = db.get(Block, lesson_id)
    if lesson is None or lesson.kind != "lesson":
        raise LessonAiError(f"block {lesson_id} is not a lesson")
    module = db.get(Block, lesson.parent_id) if lesson.parent_id else None
    course = db.get(Block, module.parent_id) if module is not None and module.parent_id else None
    if module is None or course is None or course.kind != "course":
        raise LessonAiError(f"lesson {lesson_id} is not inside a course")
    return lesson, module, course


def plan_lesson_change(db, lesson_id: uuid.UUID, *, instruction: str, note: str | None) -> dict:
    """One `role="plan"` call over ONE lesson, returning a verdict per section.

    Retrieval is best-effort on purpose: a course whose library is not indexed
    (or a `search` that raises) must still be plannable — the passages ground the
    rewrites, they do not authorise the plan.

    THIS FUNCTION HOLDS A READ TRANSACTION ACROSS THE MODEL CALL, deliberately,
    exactly as `revise.plan_revision` does. Every read it needs — the lesson, its
    module and course, the sections, the tutor-edited flags, the retrieved
    passages — happens while it builds the prompt, and the implicit SQLAlchemy
    transaction those reads opened is still open when `guided_json` blocks for
    the next 20-60 seconds. That is the Global Constraint's ONE exception
    ("never hold a connection across a model call"), and it is allowed here for
    the same reason it is allowed there: this planner WRITES NOTHING. There is
    no lock to sit on, no row anyone else could be waiting for, and no work to
    lose — the whole transaction is discardable by construction.

    The exception is bounded by the caller, not left open-ended: the `lesson_ai`
    job wrapper (`app/jobs/lesson_ai.py`) `db.rollback()`s the INSTANT this
    returns and re-`db.get`s the job row before writing `progress` to it, so the
    read transaction dies with the model call rather than living on into the
    wrapper's own writes. `apply_lesson_change` below is the other half of that
    discipline and takes the opposite route — it closes the connection before
    its model call, because it does write.
    """
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
        passages = ground_topic(db, f"{lesson.title} {instruction}".strip(),
                                source_ids=source_ids, k=PLAN_RETRIEVAL_K)
        retrieved = "\n\n".join(f"[{p.source_title}, p.{p.page_no}] {p.text}" for p in passages) or None
    except Exception:
        log.warning("lesson_ai: retrieval failed — planning without passages", exc_info=True)
    messages = build_plan_messages(
        course_title=course.title, course_brief=meta.get("brief"),
        module_title=module.title, module_objective=(module.meta or {}).get("objective") or "",
        neighbours=neighbours_text(db, lesson), lesson_title=lesson.title,
        lesson_objective=(lesson.meta or {}).get("objective") or lesson.body or "",
        blueprint_lines=_blueprint_lines(bp, language, {s["section"] for s in sections}),
        sections=sections, edited=edited,
        retrieved=retrieved, instruction=instruction, note=note, language=language, source=db,
    )
    raw = get_provider().guided_json(messages, LESSON_PLAN_SCHEMA, role="plan")
    plan = validate_plan(raw, sections)
    minutes = (meta.get("shape") or {}).get("minutes_per_lesson") or DEFAULT_MINUTES_PER_LESSON
    target = int((lesson.meta or {}).get("target_words") or target_words(int(minutes)))
    plan["impact"] = _impact(plan, sections, bp, target)
    return plan


def apply_lesson_change(db, lesson_id: uuid.UUID, *, instruction: str, note: str | None,
                        sections: list[dict]) -> dict:
    """ONE draft call with the schema restricted to `sections`; everything else
    is fixed text. Snapshot first (`prev_segments` — the same lesson-level undo
    the revise engine uses), write in place, recount. Commits. On a model error
    the lesson goes back to `ready` untouched and the error propagates.

    ONE CALL, NOT ONE PER SECTION, and that is the design rather than a saving:
    the sections the tutor ticked have to agree with EACH OTHER as well as with
    the ones he left alone. Three separate calls would each see the old text of
    the other two and write three rewrites that do not meet in the middle.

    THE INSTRUCTION IS RECORDED AS `meta.ai_instruction`, NOT `revise_instruction`,
    and the difference is not cosmetic: `jobs/curriculum_draft._claim` treats
    `revise_instruction` as a ONE-SHOT flag — it scrubs it off the row and folds it
    into that draft's objective. Written here it would sit on a `ready` lesson until
    the tutor's next Deepen or Redraft, which would then silently re-apply this
    panel's instruction (and rebuild `revise_current` around it) to a full redraft
    nobody asked to revise. `ai_instruction` is history: display-only, for
    «Τι άλλαξε;» to say what was asked, read by nothing that drafts.

    NO CONNECTION IS HELD ACROSS THE MODEL CALL — `jobs/curriculum_draft.py:
    _draft_one`'s discipline, for the same reason: this runs on the request path
    and the board is polling. Everything the call needs is read first, the
    snapshot-and-claim is committed, `db.close()` hands the connection back
    (`expire_on_commit=False` keeps the attributes read above valid), and the
    lesson is re-fetched afterwards for the write.
    """
    lesson, module, course = _lesson_or_raise(db, lesson_id)
    if (lesson.meta or {}).get("draft_status") == "drafting":
        raise LessonAiError("lesson is being drafted")
    existing = _lesson_sections(db, lesson)
    known = {s["section"] for s in existing}
    # Deduplicated, in the order he ticked them: `keep` and the schema are sets
    # either way, so a repeated key would change nothing except the `rewritten`
    # list this returns — and that list is read back to the tutor.
    ticked = list(dict.fromkeys(str(s.get("section") or "") for s in sections))
    briefs = {str(s.get("section") or ""): str(s.get("brief") or "") for s in sections}
    if not ticked:
        raise LessonAiError("no sections selected")
    unknown = [k for k in ticked if k not in known]
    if unknown:
        raise LessonAiError(f"unknown sections: {unknown}")
    keep = known - set(ticked)
    # A kept section with no text is NOT shown as fixed — «αυτό δεν αλλάζει» over
    # an empty string is an instruction to write around nothing. Its key still
    # leaves the schema (`excluded` below) and `persist_lesson(keep=...)` still
    # leaves the row alone: unticked means untouched either way.
    fixed = {s["section"]: s["body"] for s in existing
             if s["section"] in keep and s["body"].strip()}

    meta = course.meta or {}
    bp = bp_mod.blueprint_from_course_meta(meta)
    # EVERYTHING HE DID NOT TICK LEAVES THE SCHEMA — not just the sections that
    # have a row today. A blueprint key enabled on the course but missing from this
    # lesson (drafted before the section was enabled, or its row deleted) is in
    # neither `known` nor `keep`: left in the schema, the model would write a whole
    # section nobody ticked, `persist_lesson` would create the row, and the
    # `rewritten` list handed back to the tutor would not mention it. Custom keys
    # carried in by `keep` are harmless — `build_lesson_schema` has no property to
    # drop for them.
    excluded = ({s["key"] for s in bp_mod.enabled_sections(bp)} - set(ticked)) | keep
    raw_sources = meta.get("source_ids")
    source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
    shape = meta.get("shape") or {}
    minutes_per_lesson = shape.get("minutes_per_lesson") or DEFAULT_MINUTES_PER_LESSON
    lesson_meta = lesson.meta or {}
    # A lesson born from «Προσθήκη μαθήματος» carries the tutor's own brief for
    # it (`meta.brief`, Task 3.3). It is what the lesson is FOR, so every draft
    # of it — including this panel's — has to see it; dropped here, an apply
    # would quietly rewrite the lesson without the sentence that asked for it.
    # Read now, with everything else, because there is no session after the claim.
    tutor_brief = lesson_meta.get("brief")
    # Exactly the four keys `_lesson_size` reads — the ONE sizing rule, shared with
    # the drafting fan-out, so an apply cannot write a lesson to a different length
    # than a redraft of the same lesson would. 50 means 50: the whole booked slot is
    # teaching time, so `teaching_minutes` is `minutes_per_lesson`, not a share of it.
    size = _lesson_size(
        lesson,
        {"minutes_per_lesson": minutes_per_lesson,
         "teaching_minutes": minutes_per_lesson,
         "target_words": lesson_meta.get("target_words") or target_words(int(minutes_per_lesson)),
         "floor_words": lesson_meta.get("floor_words") or floor_words(int(minutes_per_lesson))},
        deepen=False,
    )
    full_instruction = instruction.strip() + (f"\n{note.strip()}" if note and note.strip() else "")
    objective = lesson_meta.get("objective") or lesson.body or ""
    # The same fold `_draft_one` performs for a `modify_lesson` revision, word for
    # word: the instruction rides the VOLATILE objective, after the cached library
    # prefix, so it costs no cache write.
    objective = f"{objective}\n\nΑναθεώρηση από τον καθηγητή: {full_instruction}"
    ctx = LessonContext(
        lesson_title=lesson.title, lesson_objective=objective,
        module_title=module.title, module_objective=(module.meta or {}).get("objective") or "",
        course_title=course.title, tier=(module.meta or {}).get("tier") or TIER_GENERAL,
        # A plain position line again (Task 3.2): the neighbours no longer ride
        # `position` as a stopgap — they go to `draft_lesson(neighbours=...)`
        # below, which renders them in their own block under this line.
        position=position_text(db, lesson),
        minutes=size["minutes"], teaching_minutes=size["teaching_minutes"],
        target_words=size["target_words"], floor_words=size["floor_words"],
    )
    language = lesson.language or course.language or "el"

    # THE LIBRARY AND THE PROMPT SNAPSHOT ARE READ BEFORE THE CLAIM, so that the
    # claim is the LAST thing that happens on the request's transaction. Read after
    # it, a failure in either (an unindexed source, a database hiccup) would leave
    # the lesson stranded at `drafting` with `error: None` — a spinner the board
    # never takes down and nothing ever clears, for a call that was never made.
    # Neither read needs a transaction of its own. The neighbours are read here for
    # the same reason — they are the last thing on this path that touches the
    # lesson's siblings, and after `db.close()` there is no session to read them on.
    library = build_retrieval_context(db, source_ids)
    prompts = overrides.snapshot(db)
    neighbours = neighbours_dict(db, lesson)

    # SNAPSHOT AND CLAIM, ONE TRANSACTION, COMMITTED BEFORE THE MODEL CALL. The
    # snapshot is what «Τι άλλαξε;» and the restore toggle read, so it has to be
    # the segments as they stand THIS INSTANT — taken after the call, it would
    # snapshot the rewrite. `drafting` is the claim, and the `draft_status` check
    # at the top of this function is what reads it: an apply on a lesson that is
    # already drafting is refused, not queued behind it.
    lesson.meta = {**lesson_meta, "prev_segments": snapshot_of(db, lesson),
                   "ai_instruction": full_instruction, "draft_status": "drafting",
                   "error": None}
    db.commit()
    db.close()  # hand the connection back — see the docstring
    try:
        drafted, m = draft_lesson(
            db, ctx=ctx, library=library, language=language, blueprint=bp,
            student_brief=None, course_brief=meta.get("brief"),
            neighbours=neighbours, tutor_brief=tutor_brief, source_ids=source_ids,
            prompts=prompts, revise_current=None, fixed_sections=fixed or None,
            exclude_sections=excluded, section_briefs=briefs,
            # NO CITATION-REPAIR RE-DRAFT ON THIS PATH (Task 4.3). A repair is a
            # SECOND full draft call, and here that is 6-12 minutes for a page
            # number: 2026-09-12, live, an apply took 390s to draft, cited one
            # page it had never been shown, and spent 726s re-drafting the whole
            # lesson to fix it — 19 minutes, past the panel's poll cap, so the
            # tutor was told «Το AI δουλεύει ακόμα» and saw nothing change. A bad
            # cite is dropped instead (`strip_invalid_citations`, where the repair
            # already falls back to): the tutor's own text is the authority on an
            # apply and the grounding is per-lesson retrieval, so an uncited
            # paragraph costs him nothing he had. The fan-out keeps the repair.
            repair_citations=False,
        )
    except Exception:
        # Back to `ready` with the content UNCHANGED — nothing was written yet.
        # `prev_segments` may stay: it still describes the lesson exactly as it
        # is, so the restore toggle is a no-op rather than a lie. The row may also
        # be GONE (he deleted the lesson while it drafted): releasing a row that
        # is not there must not raise over the error we are propagating.
        db.rollback()
        lesson = db.get(Block, lesson_id)
        if lesson is not None:
            lesson.meta = {**(lesson.meta or {}), "draft_status": "ready"}
            db.commit()
        raise
    lesson = db.get(Block, lesson_id)
    persist_lesson(db, lesson, drafted, m, library, bp,
                   teaching_minutes=size["teaching_minutes"], keep=keep)
    words = recompute_lesson_words(db, lesson)
    db.commit()
    return {"rewritten": list(ticked), "word_count": words}
