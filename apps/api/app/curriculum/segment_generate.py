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

import logging
import uuid
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.curriculum.ground import ground_topic
from app.curriculum.revise import _recompute_lesson_word_count
from app.curriculum.sanitize import strip_inline_citations
from app.i18n import answer_in, curriculum_style, language_directive
from app.llm.factory import get_provider
from app.models.block import Block
from app.prompts.overrides import resolve

log = logging.getLogger(__name__)

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
    "{language_directive}\n\n"
    "{style_directive}"
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
        style_directive=curriculum_style(language, source),
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

    THE CONNECTION DISCIPLINE (`jobs/curriculum_draft.py`'s, applied here):
    every DB read happens BEFORE the model call, the read transaction is
    released, and the rows are re-fetched for the write afterwards. This is a
    `role="draft"` call — 32K output, minutes each, run once per queued segment
    in a drain that can cover a whole course ("add a homework section to every
    lesson" = 24 of these back-to-back). Holding the read transaction across
    the call pinned a pooled connection for the entire drain, which is exactly
    the starvation the draft fan-out's docstring spends 18 lines defending
    against while the board polls progress every 2 seconds.
    """
    segment_id = segment.id
    lesson = db.get(Block, segment.parent_id) if segment.parent_id else None
    lesson_id = lesson.id if lesson is not None else None
    instruction = (segment.meta or {}).get("segment_instruction") or ""

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
    messages = build_segment_messages(
        instruction=instruction,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        title=segment.title,
        language=segment.language,
        siblings=siblings,
        context=context,
        source=db,
    )

    # Everything is read; hand the connection back before dialling the model.
    # A read-only transaction has nothing to keep — rollback is the honest way
    # to end it (commit would imply there was something to save).
    db.rollback()

    result = get_provider().guided_json(messages, SEGMENT_SCHEMA, role="draft")

    # Fresh transaction for the write. Re-fetch: the segment may have been
    # deleted while the model wrote (a revise op, a course delete) — writing
    # onto a vanished row would resurrect it.
    segment = db.get(Block, segment_id)
    if segment is None:
        log.info("generate_segment: segment %s deleted mid-generation — discarding", segment_id)
        return
    meta = segment.meta or {}

    segment.title = (result.get("title") or segment.title).strip() or segment.title
    # The style rule tells the model to keep page markers out of the prose;
    # `strip_inline_citations` is what holds when it writes one anyway — the
    # REAL provenance is `meta.citations` below, never the body text.
    segment.body = strip_inline_citations(result.get("body")) or segment.body
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
        **{k: v for k, v in meta.items() if k not in ("segment_instruction", "tutor_edited")},
        "segment_status": "done",
        "citations": citations,
    }
    lesson = db.get(Block, lesson_id) if lesson_id is not None else None
    if lesson is not None:
        _recompute_lesson_word_count(db, lesson)


# ---------------------------------------------------------------------------
# Draining every queued segment under a course — shared by the revise job's
# apply-mode chain (jobs/curriculum_revise.py) and the resume endpoint
# (routers/curriculum.py). Extracted (whole-branch review, finding #3) so a
# segment stranded `queued` with no job of its own watching it — the revise
# job's chain never ran or was interrupted, or `add_segment`/`edit_segment`
# queued it outside that chain entirely — has a SECOND way back to `done`:
# pressing Resume, exactly like a stranded lesson already does.
# ---------------------------------------------------------------------------

def _queued_segment_ids(db, root_id: uuid.UUID) -> list[uuid.UUID]:
    """Every SEGMENT with `meta.segment_status == "queued"` under this course, in
    teaching order (module, then lesson, then segment order) — the ones
    `apply_revision`'s `add_segment`/`edit_segment` created or marked, and the
    only ones `drain_queued_segments` below has any business touching.
    Mirrors `curriculum_draft._queued_lesson_ids`'s join shape, one level
    deeper (module -> lesson -> segment instead of module -> lesson)."""
    module = aliased(Block)
    lesson = aliased(Block)
    rows = db.execute(
        select(Block.id, Block.meta)
        .join(lesson, Block.parent_id == lesson.id)
        .join(module, lesson.parent_id == module.id)
        .where(
            module.parent_id == root_id,
            module.kind == "module",
            lesson.kind == "lesson",
            Block.kind == "segment",
        )
        .order_by(module.order, lesson.order, Block.order)
    ).all()
    return [seg_id for seg_id, meta in rows if (meta or {}).get("segment_status") == "queued"]


def drain_queued_segments(
    db, root_id: uuid.UUID,
    *, on_progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    """Generate every SEGMENT still `segment_status == "queued"` under this
    course — one grounded provider call each, via `generate_segment`.

    FAILURE IS PER-SEGMENT, never raised out of here: an exception marks that
    ONE segment `failed` (with `segment_error`) and the loop moves on to the
    next — the same isolation `run_curriculum_revise_job`'s apply-mode chain
    already had inline before this was lifted out, and the same principle
    `jobs/curriculum_draft.py` uses per-lesson.

    `on_progress(total, done, failed)`, called once up front and again after
    every segment, is the caller's hook for a live progress row — the revise
    job passes one so `job.progress["segments_*"]` keeps updating as it always
    did; a plain resume-triggered drain has no job row for these segments and
    passes none. Returns `{"total", "done", "failed"}` either way — purely
    informational, the tree itself is the source of truth for what happened."""
    segment_ids = _queued_segment_ids(db, root_id)
    done = failed = 0
    if on_progress is not None:
        on_progress(len(segment_ids), done, failed)
    for segment_id in segment_ids:
        segment = db.get(Block, segment_id)
        if segment is None:
            continue
        try:
            generate_segment(db, segment)
            db.commit()
            done += 1
        except Exception as e:
            log.warning("drain_queued_segments: segment %s failed to generate",
                        segment_id, exc_info=True)
            db.rollback()
            segment = db.get(Block, segment_id)
            if segment is not None:
                # WHOLE-DICT reassignment (Constraint #4).
                segment.meta = {**(segment.meta or {}),
                                "segment_status": "failed", "segment_error": str(e)}
                db.commit()
            failed += 1
        if on_progress is not None:
            on_progress(len(segment_ids), done, failed)
    return {"total": len(segment_ids), "done": done, "failed": failed}
