"""SEGMENT GENERATION — the other half of Task 1's surgical ops.

NOT `curriculum/segment.py` — that file already exists and does something
unrelated (deterministic delivery-plane session partitioning, `segment_block`/
`partition_by_minutes`), is actively imported by `agent/tools.py`, `lessons/
edit.py`, `routers/curriculum.py`, `routers/lessons.py`, and has its own two
test modules. This module's name collided with it only in English; the two have
nothing to do with each other (that one PACES an already-written course into
teaching sessions, this one WRITES one lesson SECTION), so it lives here instead
rather than clobbering working, tested code.

`revise.apply_revision`'s `add_segment` / `edit_segment` only ever CREATE or MARK
a segment `meta.segment_status == "queued"` with a `segment_instruction`; they
make zero provider calls (apply's four properties — one transaction, one commit,
rollback-on-exception, zero LLM calls — cover these ops too, same as every other
queued op in that module). `generate_segment` here is what actually WRITES the
section: one grounded, schema-constrained call per queued segment, run by the
revise job (`jobs/curriculum_revise.py`) right after apply commits.

MIRRORS `refine.py`'s SHAPE ON PURPOSE. Both are "one block, one instruction, one
`guided_json` call, whole-dict meta reassignment"; `refine_block` rewrites a
block that already has a body, this fills one that does not, and the schema is
the SAME two fields (`title`, `body`) — the tutor's instruction decides the
content, not a richer contract this call has no use for.

GROUNDED BY `ground_topic`, SCOPED TO THE COURSE — the same read `revise.
plan_revision` does (`revise.py:364-366`): the tutor's own `course.meta.
source_ids`, not the whole library. The retrieved passages are what this module
calls "citations": `SEGMENT_SCHEMA` deliberately has NO `citations` field, so the
model is never asked to name a page it read — it is simply not given the chance
to invent one (`corpus.py`'s citation-honesty argument, taken one step further).
What actually grounded the call IS the citation, stored on `meta.citations` after
the call succeeds; nothing about it needs validating against anything else,
because it was never the model's claim to begin with.

FAILURE IS THE CALLER'S TO CATCH. Unlike `refine_block`'s dead-retriever
try/except (retrieval there is an improvement over an already-written block, not
a precondition), a provider failure HERE means the section never got written —
there is nothing to degrade to. `generate_segment` therefore raises
(`LLMError` or anything else `guided_json` raises) and mutates nothing on
failure; the JOB LOOP is what turns one segment's exception into a `failed`
status and moves on to the next, mirroring `jobs/curriculum_draft.py`'s
per-lesson isolation.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.curriculum.ground import ground_topic
from app.curriculum.revise import _recompute_lesson_word_count
from app.i18n import answer_in, language_directive
from app.llm.factory import get_provider
from app.models.block import Block
from app.prompts.overrides import resolve

SEGMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Keep the given section title unless the instruction asks you to change it.",
        },
        "body": {
            "type": "string",
            "description": "The FULL text of this section — not an outline, not a note about what you would write.",
        },
    },
    "required": ["title", "body"],
    "additionalProperties": False,
}

# THE PROMPT, lifted into constants so the tutor can rewrite it (Prompt
# transparency, same discipline as REFINE_SYSTEM/REFINE_USER). Two slices,
# because the model reads two messages: SYSTEM is the writing philosophy (write
# ONE section, consistent with the rest, follow the instruction exactly), USER
# is the lesson context, the retrieved passages, and — last, for recency — the
# tutor's instruction.
SEGMENT_SYSTEM = (
    "You are writing ONE new section of an existing lesson in a Greek guitar "
    "teacher's course, in the course language. Output ONLY the JSON object.\n\n"
    "Write the FULL section, in full — not an outline, not a summary of what "
    "you would say. Make it read as part of the SAME lesson as the sections "
    "around it below: same level, same voice, and do not repeat what they "
    "already cover. Follow the tutor's instruction for this section exactly — "
    "it is what he asked for, not a suggestion.\n\n"
    "NEVER invent a page citation. The passages below are what this section may "
    "be grounded in; where they do not cover something you still need to say, "
    "write it from your own knowledge rather than attaching a citation to it.\n\n"
    "{language_directive}"
)
SEGMENT_SYSTEM_SLICE_ID = "segment.generate"

SEGMENT_TAIL = (
    "LESSON: {lesson_title}\n"
    "OBJECTIVE: {lesson_objective}"
    "{siblings_block}"
    "{context_block}"
    "\n\nNEW SECTION TITLE: {title}\n"
    "\nWHAT THIS SECTION SHOULD SAY:\n{instruction}\n"
    "\n{answer_in}"
)
SEGMENT_TAIL_SLICE_ID = "segment.generate.user"

SEGMENT_SIBLINGS_BLOCK = "\n\nTHE LESSON'S OTHER SECTIONS, FOR CONSISTENCY:\n{siblings}"
SEGMENT_CONTEXT_BLOCK = "\n\nFROM HIS LIBRARY:\n{context}"

# Per-sibling truncation — the sibling sections are here for VOICE/LEVEL
# consistency, not as source material to re-read in full; 2000 chars is plenty
# to establish tone without re-spending the whole lesson's token budget on every
# one-section generation call.
SEGMENT_SIBLING_CHARS = 2000
SEGMENT_RETRIEVAL_K = 4


def build_segment_messages(
    *,
    instruction: str,
    lesson_title: str,
    lesson_objective: str,
    title: str,
    language: str,
    siblings: str | None = None,
    context: str | None = None,
    source=None,
) -> list[dict]:
    """Pure. The tutor's instruction is the LAST thing the model reads, after the
    lesson context and the retrieved passages — same recency argument as
    `build_refine_messages`."""
    system = resolve(source, SEGMENT_SYSTEM_SLICE_ID, SEGMENT_SYSTEM).format(
        language_directive=language_directive(language, source),
    )
    user = resolve(source, SEGMENT_TAIL_SLICE_ID, SEGMENT_TAIL).format(
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        siblings_block=(SEGMENT_SIBLINGS_BLOCK.format(siblings=siblings) if siblings else ""),
        context_block=(SEGMENT_CONTEXT_BLOCK.format(context=context) if context else ""),
        title=title,
        instruction=instruction,
        answer_in=answer_in(language, source),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _sibling_text(db, lesson_id: uuid.UUID, exclude_id: uuid.UUID) -> str | None:
    siblings = db.scalars(
        select(Block)
        .where(Block.parent_id == lesson_id, Block.kind == "segment", Block.id != exclude_id)
        .order_by(Block.order)
    ).all()
    if not siblings:
        return None
    return "\n\n".join(
        f"{s.title}:\n{(s.body or '')[:SEGMENT_SIBLING_CHARS]}" for s in siblings
    )


def _course_source_ids(db, lesson: Block) -> list[uuid.UUID] | None:
    """Walk lesson -> module -> course for the tutor-chosen `source_ids` scope —
    the SAME read `plan_revision` does (`revise.py:364-366`), so a segment
    grounds from the same shelf the rest of the course's revise chat does.
    `None` (course missing, or `source_ids` unset) means "the whole corpus",
    exactly like `plan_revision`'s own None-vs-list rule."""
    module = db.get(Block, lesson.parent_id) if lesson.parent_id else None
    course = db.get(Block, module.parent_id) if module is not None and module.parent_id else None
    if course is None:
        return None
    raw = (course.meta or {}).get("source_ids")
    return None if raw is None else [uuid.UUID(s) for s in raw]


def generate_segment(db, segment: Block) -> None:
    """Fill in ONE queued segment's title/body per `meta.segment_instruction`.

    Mutates `segment` (and its parent lesson's word count); the caller commits —
    same contract as `refine.refine_block`. Raises whatever `guided_json` raises
    on a provider failure and leaves `segment` untouched: there is no degraded
    fallback for a section that was never written, unlike `refine_block`'s
    already-has-a-body case. The job loop decides what a raised exception means
    for the segment's status.
    """
    meta = segment.meta or {}
    instruction = meta.get("segment_instruction") or ""
    lesson = db.get(Block, segment.parent_id) if segment.parent_id else None

    siblings = _sibling_text(db, lesson.id, segment.id) if lesson is not None else None
    source_ids = _course_source_ids(db, lesson) if lesson is not None else None
    lesson_title = lesson.title if lesson is not None else ""
    lesson_objective = (lesson.meta or {}).get("objective") or "" if lesson is not None else ""

    passages = ground_topic(
        db, f"{lesson_title} {instruction}".strip(),
        source_ids=source_ids, k=SEGMENT_RETRIEVAL_K,
    )
    context = "\n\n".join(
        f"[{p.source_title}, p.{p.page_no}] {p.text}" for p in passages
    ) or None

    result = get_provider().guided_json(
        build_segment_messages(
            instruction=instruction,
            lesson_title=lesson_title,
            lesson_objective=lesson_objective,
            title=segment.title,
            language=segment.language,
            siblings=siblings,
            context=context,
            source=db,
        ),
        SEGMENT_SCHEMA,
        role="draft",
    )

    segment.title = (result.get("title") or segment.title).strip() or segment.title
    segment.body = result.get("body") or segment.body
    citations = [
        {"source_id": str(p.source_id), "source_title": p.source_title, "page": p.page_no}
        for p in passages
    ]
    # WHOLE-DICT reassignment — `Block.meta` is plain sa.JSON, no MutableDict
    # (Global Constraint #4). Scrubs `segment_instruction` (its job is done) and
    # flips the segment to `done`. `citations` REPLACES whatever was there
    # before: what actually grounded THIS generation, never a stale citation
    # left over from an earlier edit.
    segment.meta = {
        **{k: v for k, v in meta.items() if k != "segment_instruction"},
        "segment_status": "done",
        "citations": citations,
    }
    if lesson is not None:
        _recompute_lesson_word_count(db, lesson)
