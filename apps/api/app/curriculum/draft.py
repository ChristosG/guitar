"""Drafting ONE lesson, from the whole library, with citations that are checked.

THE LESSON IS THE FAN-OUT UNIT, AND THAT IS AN ARITHMETIC RESULT, NOT A TASTE.

One call per MODULE looks tidier: 4-5 lessons in a single structured response, a
quarter of the calls. It does not survive contact with the numbers. A 40-minute
lesson targets ~2,200 words (`app.curriculum.depth`); in GREEK that is ~6,000
tokens of prose inside a JSON envelope, so four or five of them is 30-40k output
tokens — straight through `max_tokens` on every single module. And a response cut
off at `max_tokens` is a TRUNCATED JSON STRING, so the failure arrives as a
`JSONDecodeError`. Whoever picks it up goes looking for a bad prompt. The whole
curriculum fails, in a way that points at the wrong file.

So: one lesson, one call, `max_tokens=32000`, streamed, each reading the SAME
cached library prefix the outline call warmed.

---------------------------------------------------------------------------
EVERY CITATION IS VALIDATED, AND A BAD ONE IS WORSE THAN NONE.

The model is shown `[p.19] ... [p.20] ...` markers inside `<source id="S1">` and
asked to cite `{source_id: "S1", page: 19}`. Nothing stops it citing p.412 of a
77-page book. That citation renders as a chip the tutor CLICKS — and lands him on
a page that does not say what the lesson claims it says. He would be right to stop
trusting every other chip on the screen after that, including the true ones.

So every cite is checked against `LibraryContext.page_index` — the exact set of
(source, page) pairs the model was actually shown. A lesson with a bad cite gets
ONE repair retry naming the offending pages. If it comes back bad a second time,
the lesson is KEPT and the unresolvable citations are DROPPED: the prose is
almost certainly fine, and an uncited paragraph is an honest thing while a false
citation is not.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.curriculum.corpus import LibraryContext, prefix_messages
from app.curriculum.depth import (
    DEEPEN_MAX_PASSES,
    LESSON_DRAFT_SCHEMA,
    SECTIONS,
    Measurement,
    measure,
)
from app.curriculum.ground import ground_topic
from app.curriculum.outline import TIER_GENERAL, TIER_LIBRARY, TIER_WEB
from app.i18n import answer_in, language_directive
from app.llm.factory import get_provider

log = logging.getLogger(__name__)


@dataclass
class LessonContext:
    """Everything about ONE lesson that varies between calls. Every field here is
    volatile by definition — which is precisely why it is a separate object from
    `LibraryContext`, and why it is rendered AFTER the cached library block.
    """

    lesson_title: str
    lesson_objective: str
    module_title: str
    module_objective: str
    course_title: str
    tier: str
    position: str          # "lesson 3 of 4, module 2 of 5" — the model needs to know
    minutes: int
    teaching_minutes: int
    target_words: int
    floor_words: int


def _tier_directive(tier: str) -> str:
    if tier == TIER_LIBRARY:
        return (
            "THIS MODULE IS GROUNDED IN HIS LIBRARY. Teach it from the pages above. "
            "Quote and paraphrase HIS material, and cite the page you used on every "
            "section — {source_id, page} against the <source> ids and [p.N] markers "
            "you were given. Cite ONLY pages you actually read. Do not invent a page "
            "number; an empty citations array is always better than a wrong one."
        )
    if tier == TIER_WEB:
        return (
            "His library does not cover this module, and it needs current "
            "information. Write it from your general knowledge, and say plainly "
            "inside the prose where a fact would need checking against a current "
            "source. Cite nothing to his library — you did not read it there. "
            "Leave every citations array empty."
        )
    return (
        "HIS LIBRARY DOES NOT COVER THIS MODULE — he has agreed to it being written "
        "from your general knowledge instead, and it will be LABELLED as such for "
        "him. So write it well, but cite NOTHING to his library: you did not read "
        "this there. Leave every citations array empty. A fabricated page number is "
        "the one thing that would make this dishonest."
    )


def build_lesson_messages(
    *,
    ctx: LessonContext,
    library: LibraryContext,
    language: str,
    student_brief: str | None,
    course_brief: str | None,
    retrieved: str | None = None,
    deepen: Measurement | None = None,
    previous: dict | None = None,
) -> list[dict]:
    """The messages for one lesson draft. Pure.

    THE PREFIX IS `corpus.prefix_messages` — byte-identical to the outline call's
    and the add-module call's, and that is not a coincidence to be tidied away
    later: it is what makes the prompt cache hit. EVERYTHING about this lesson —
    including its word target and its module's tier directive — goes after it.
    (They used to live in a per-lesson system message, which silently gave every
    distinct tier/length combination its own 90K-token cache write at 1.25x.)

    `deepen`/`previous` turn this into the deepen prompt: same prefix (still a
    cache hit), plus the previous draft and the sections that came back thin.
    """
    messages = prefix_messages(library)

    # ---- volatile, and strictly after the cache breakpoint ----
    tail = [
        "YOUR TASK: write ONE complete lesson — the actual pages the tutor will "
        "teach from, not a plan for them.",
        f"\nCOURSE: {ctx.course_title}",
        f"MODULE: {ctx.module_title} — {ctx.module_objective}",
        f"LESSON: {ctx.lesson_title} — {ctx.lesson_objective}",
        f"POSITION: {ctx.position}. Do not re-teach what earlier lessons covered; "
        f"build on it.",
        f"\nLENGTH IS NOT OPTIONAL. This lesson is {ctx.teaching_minutes} minutes "
        f"of teaching plus a Q&A block, and it must run to about "
        f"{ctx.target_words:,} words in total across its sections — roughly four to "
        f"five pages. A lesson under {ctx.floor_words:,} words is a rejected "
        "lesson: it will be sent back to you to be written properly. Write the "
        "theory out in full, in real paragraphs. Do not write bullet points and "
        "call them a lesson.",
        f"\n{_tier_directive(ctx.tier)}",
        f"\n{language_directive(language)}",
    ]
    if course_brief:
        tail.append(f"\nWHAT THE TUTOR WANTS FROM THIS COURSE:\n{course_brief}")
    if student_brief:
        tail.append(f"\n{student_brief}")
    if retrieved:
        # The oversized-library fallback: the whole book did not fit, so this
        # lesson gets the passages retrieval found for it instead. The tutor is
        # told this on the board (`meta.library.full_context = false`).
        tail.append(
            "\nHis library was too large to read in full for this course, so here "
            "are the passages retrieved for THIS lesson. Ground it in these, and "
            f"cite them:\n\n{retrieved}"
        )
    if deepen is not None and previous is not None:
        import json

        thin = ", ".join(deepen.thin_sections) or "all of them"
        tail.append(
            f"\nYOUR PREVIOUS DRAFT CAME BACK AT {deepen.total_words:,} WORDS — "
            f"under the {deepen.floor:,}-word floor. Rewrite it in full, keeping "
            f"what is good, and EXPAND these sections, which are the thin ones: "
            f"{thin}. Add real teaching substance — worked explanations, more "
            f"exercises, the things students actually ask — not padding, and not a "
            f"longer introduction.\n\nYOUR PREVIOUS DRAFT:\n"
            f"{json.dumps(previous, ensure_ascii=False)}"
        )
    tail.append(f"\n{answer_in(language)}")

    messages.append({"role": "user", "content": "\n".join(tail)})
    return messages


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

def _iter_citation_lists(lesson: dict):
    for name in SECTIONS:
        section = lesson.get(name)
        if isinstance(section, dict) and isinstance(section.get("citations"), list):
            yield name, section


def invalid_citations(lesson: dict, library: LibraryContext) -> list[tuple[str, str, int]]:
    """Every `(section, source_ref, page)` the model cited that it was never shown.

    Checked against `LibraryContext.page_index` — the pages ACTUALLY in the prompt
    — and not against the database. Those are different sets: a page whose OCR
    produced 12 characters is in the DB and is NOT in the prompt (`corpus.
    MIN_PAGE_CHARS` drops it), so the model cannot have read it there, so a cite to
    it is a fabrication even though the row exists.
    """
    bad: list[tuple[str, str, int]] = []
    for name, section in _iter_citation_lists(lesson):
        for cite in section["citations"]:
            if not isinstance(cite, dict):
                continue
            ref, page = cite.get("source_id"), cite.get("page")
            pages = library.page_index.get(str(ref))
            if pages is None or not isinstance(page, int) or page not in pages:
                bad.append((name, str(ref), page if isinstance(page, int) else -1))
    return bad


def strip_invalid_citations(lesson: dict, library: LibraryContext) -> dict:
    """Drop the citations that do not resolve, keep the lesson. See the module
    docstring: after one repair attempt, a false citation is the only part worth
    destroying."""
    for _name, section in _iter_citation_lists(lesson):
        section["citations"] = [
            c for c in section["citations"]
            if isinstance(c, dict)
            and isinstance(c.get("page"), int)
            and c.get("page") in library.page_index.get(str(c.get("source_id")), ())
        ]
    return lesson


def _repair_message(bad: list[tuple[str, str, int]], library: LibraryContext) -> dict:
    detail = "; ".join(f"{name} cites {ref} p.{page}" for name, ref, page in bad)
    available = ", ".join(
        f"{ref} has pages {min(pages)}-{max(pages)}"
        for ref, pages in sorted(library.page_index.items())
    ) or "no sources at all"
    return {
        "role": "user",
        "content": (
            f"STOP. You cited pages that do not exist in what I gave you: {detail}. "
            f"The library you were shown has: {available}. The tutor CLICKS these "
            f"citations and lands on the page — a wrong page number is worse than no "
            f"citation at all. Produce the whole lesson again, identical in "
            f"substance, citing ONLY [p.N] markers you actually read. Where you are "
            f"not certain of the page, use an empty citations array."
        ),
    }


# ---------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------

def draft_lesson(
    db,
    *,
    ctx: LessonContext,
    library: LibraryContext,
    language: str,
    student_brief: str | None = None,
    course_brief: str | None = None,
    source_ids: list[uuid.UUID] | None = None,
) -> tuple[dict, Measurement]:
    """One lesson: draft -> validate citations (one repair) -> measure -> at most
    one deepen pass. Returns `(lesson, measurement)`.

    `db` IS ONLY TOUCHED FOR THE RETRIEVAL FALLBACK, and only when the library did
    not fit whole. In the normal (full-context) path this function performs NO
    database access at all — which is what lets `jobs/curriculum_draft.py` run it
    on a worker thread holding no connection while the board polls progress on the
    request path.
    """
    provider = get_provider()

    retrieved = None
    if not library.fits and not library.is_empty:
        passages = ground_topic(
            db, f"{ctx.module_title} {ctx.lesson_title} {ctx.lesson_objective}",
            source_ids=source_ids, k=6,
        )
        retrieved = "\n\n".join(
            f"[{p.source_title}, p.{p.page_no}] {p.text}" for p in passages
        )

    messages = build_lesson_messages(
        ctx=ctx, library=library, language=language,
        student_brief=student_brief, course_brief=course_brief, retrieved=retrieved,
    )
    lesson = provider.guided_json(messages, LESSON_DRAFT_SCHEMA, role="draft")

    bad = invalid_citations(lesson, library)
    if bad:
        log.warning("lesson %r cited %d page(s) it was never shown — repairing",
                    ctx.lesson_title, len(bad))
        repaired = provider.guided_json(
            [*messages, _repair_message(bad, library)], LESSON_DRAFT_SCHEMA, role="draft",
        )
        lesson = repaired
        if invalid_citations(lesson, library):
            log.warning("lesson %r still cites unknown pages after one repair — "
                        "dropping the bad citations, keeping the prose", ctx.lesson_title)
            lesson = strip_invalid_citations(lesson, library)

    m = measure(lesson, teaching_minutes=ctx.teaching_minutes)
    passes = 0
    while m.needs_deepening and passes < DEEPEN_MAX_PASSES:
        passes += 1
        log.info("lesson %r came back at %d words (floor %d) — deepen pass %d",
                 ctx.lesson_title, m.total_words, m.floor, passes)
        deeper = provider.guided_json(
            build_lesson_messages(
                ctx=ctx, library=library, language=language,
                student_brief=student_brief, course_brief=course_brief,
                retrieved=retrieved, deepen=m, previous=lesson,
            ),
            LESSON_DRAFT_SCHEMA, role="draft",
        )
        if invalid_citations(deeper, library):
            deeper = strip_invalid_citations(deeper, library)
        deeper_m = measure(deeper, teaching_minutes=ctx.teaching_minutes)
        # Keep the LONGER draft. A deepen pass that came back shorter has not
        # deepened anything, and silently accepting it would make the tutor's
        # "Deepen" button able to shrink his lesson.
        if deeper_m.total_words > m.total_words:
            lesson, m = deeper, deeper_m

    return lesson, m


# ---------------------------------------------------------------------------
# Persisting a drafted lesson
# ---------------------------------------------------------------------------

# The section headings that land on `Block.title` (a NOT NULL column). The FRONTEND
# renders from `meta.section` — the stable machine key — and is free to ignore
# these; they exist so a segment row is legible in the database, in a CSV export,
# and in the printed handout, without every consumer needing a translation table.
SECTION_LABELS: dict[str, dict[str, str]] = {
    "el": {
        "warm_up": "Ζέσταμα",
        "theory": "Θεωρία",
        "demonstration": "Επίδειξη",
        "exercises": "Ασκήσεις",
        "common_mistakes": "Συνήθη λάθη",
        "recap": "Ανακεφαλαίωση",
        "homework": "Εργασία για το σπίτι",
        "qa_prompts": "Ερωτήσεις & συζήτηση",
    },
    "en": {
        "warm_up": "Warm-up",
        "theory": "Theory",
        "demonstration": "Demonstration",
        "exercises": "Exercises",
        "common_mistakes": "Common mistakes",
        "recap": "Recap",
        "homework": "Homework",
        "qa_prompts": "Q&A and discussion",
    },
}

# How the taught minutes are spread across the sections on the printed script.
# Derived from the same weights `depth` measures against, so a section that is 25%
# of the words is 25% of the clock — the two numbers cannot drift apart.
def _section_minutes(name: str, teaching_minutes: int, qa_minutes: int) -> int:
    from app.curriculum.depth import SECTION_WEIGHTS

    if name == "qa_prompts":
        return max(1, qa_minutes)
    return max(1, round(SECTION_WEIGHTS[name] * teaching_minutes))


def _render_section(name: str, section: dict) -> str:
    """One section -> the text that lands on `Block.body`.

    `exercises.items` and `qa_prompts.items` are folded into the prose rather than
    stored as separate child blocks: they are what the tutor READS OFF THE PAGE
    while teaching, and a Q&A prompt separated from its answer key by a tree
    boundary is a Q&A prompt he cannot use.
    """
    parts = [section.get("body") or ""]
    for item in section.get("items") or []:
        if not isinstance(item, dict):
            continue
        if "question" in item:
            parts.append(f"\nQ: {item.get('question', '')}\nA: {item.get('answer_key', '')}")
        else:
            mins = item.get("est_minutes")
            head = item.get("title", "")
            if mins:
                head = f"{head} ({mins} min)"
            parts.append(f"\n{head}\n{item.get('instructions', '')}")
    return "\n".join(p for p in parts if p.strip()).strip()


def persist_lesson(
    db,
    lesson_block,
    lesson: dict,
    m: Measurement,
    library: LibraryContext,
    *,
    qa_minutes: int,
    teaching_minutes: int,
) -> None:
    """Replace `lesson_block`'s segments with the drafted lesson's sections, and
    flip it to `ready`. Caller commits.

    IDEMPOTENT — it deletes the existing segments first. That is what makes the
    Deepen button, a re-draft, and a Resume that re-runs a lesson whose worker died
    after the model call but before the commit all safe: none of them can leave a
    lesson with two copies of its theory section.

    THE OLD SEGMENTS ARE FOUND BY QUERY, NOT VIA `lesson_block.children`. The
    relationship is a CACHED collection: a caller that touched `.children` before
    calling this (which the board and every test naturally do) gets the list as it
    was AT LOAD TIME, so the delete loop sees nothing to delete and the new segments
    are simply appended — eight sections become sixteen, and it looks fine until you
    count. `app.lessons.edit` hit the same trap from the other direction (a stale
    collection causing a delete-orphan cascade to eat re-parented items) and solved
    it with `db.expire(..., ["children"])`; the same expire runs at the end here, so
    the caller's next read of `.children` is fresh.
    """
    from sqlalchemy import select

    from app.models.block import Block

    for old in db.scalars(select(Block).where(Block.parent_id == lesson_block.id)).all():
        db.delete(old)
    db.flush()

    # Fallback is "el", not "en" — i18n.py's own rule: anything in this codebase
    # still defaulting to English is a bug. A student row carrying "el-GR" used
    # to make a Greek lesson persist English section headings.
    lang = lesson_block.language if lesson_block.language in SECTION_LABELS else "el"
    citations_all: list[dict] = []

    order = 0
    for name in SECTIONS:
        section = lesson.get(name)
        if not isinstance(section, dict):
            continue
        body = _render_section(name, section)
        if not body:
            continue
        cites = [
            {
                "source_id": str(library.ref_to_source_id.get(str(c["source_id"]))),
                "source_ref": c["source_id"],
                "source_title": next(
                    (s["title"] for s in library.sources if s["ref"] == c["source_id"]), None,
                ),
                "page": c["page"],
            }
            for c in section.get("citations") or []
            if isinstance(c, dict) and str(c.get("source_id")) in library.ref_to_source_id
        ]
        citations_all.extend(cites)
        db.add(Block(
            kind="segment",
            title=SECTION_LABELS[lang][name],
            body=body,
            est_minutes=_section_minutes(name, teaching_minutes, qa_minutes),
            order=order,
            parent_id=lesson_block.id,
            language=lesson_block.language,
            meta={"section": name, "citations": cites},
        ))
        order += 1

    if lesson.get("summary"):
        lesson_block.body = lesson["summary"]

    # See the docstring: the caller's cached `children` collection is stale the
    # moment we delete and re-add. Expiring it makes the next read a fresh SELECT.
    db.expire(lesson_block, ["children"])

    # WHOLE-DICT REASSIGNMENT. `Block.meta` is plain sa.JSON with no MutableDict:
    # `lesson_block.meta["draft_status"] = "ready"` would not persist, the board
    # would poll forever, and it would work perfectly in dev.
    lesson_block.meta = {
        **(lesson_block.meta or {}),
        "draft_status": "ready",
        "word_count": m.total_words,
        "target_words": m.target,
        "floor_words": m.floor,
        "meets_floor": m.meets_floor,
        "thin_sections": m.thin_sections,
        "citations": citations_all,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Progress — a GROUP BY, not a counter
# ---------------------------------------------------------------------------

DRAFT_STATUSES = ("queued", "drafting", "ready", "failed")


def draft_progress(db, root_id: uuid.UUID) -> dict:
    """`{total, ready, drafting, queued, failed, done}` for one curriculum.

    Computed from the LESSON BLOCKS THEMSELVES, every poll. Not from a counter on
    the job row — a counter is a second source of truth, and it would disagree with
    the tree the first time the tutor deleted a lesson while the draft was running.
    The blocks are what he is looking at; the blocks are what we count.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from app.models.block import Block

    module = aliased(Block)
    rows = db.execute(
        select(Block.meta)
        .join(module, Block.parent_id == module.id)
        .where(
            module.parent_id == root_id,
            module.kind == "module",
            Block.kind == "lesson",
        )
    ).all()

    counts = dict.fromkeys(DRAFT_STATUSES, 0)
    total = 0
    for (meta,) in rows:
        total += 1
        status = (meta or {}).get("draft_status") or "queued"
        counts[status if status in counts else "queued"] += 1

    return {
        "total": total,
        **counts,
        # DONE means nothing is left that a worker could still pick up. A curriculum
        # with 19 ready lessons and 1 failed one IS done — the tutor gets his 19
        # lessons and a visible failure he can retry, not a progress bar stuck at
        # 95% forever.
        "done": total > 0 and counts["queued"] == 0 and counts["drafting"] == 0,
    }
