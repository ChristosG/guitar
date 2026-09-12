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
from dataclasses import dataclass, replace

from app.curriculum.corpus import LibraryContext, prefix_messages
from app.curriculum.depth import (
    DEEPEN_MAX_PASSES,
    Measurement,
    SECTION_LABELS,  # re-exported: now lives in `depth`, a pure module (see depth.py)
    measure,
)
from app.curriculum.ground import ground_topic
from app.curriculum.outline import TIER_GENERAL, TIER_LIBRARY, TIER_WEB
from app.curriculum.sanitize import strip_inline_citations
from app.i18n import answer_in, curriculum_style, language_directive
from app.prompts.overrides import resolve
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


# The three tier directives, lifted byte-identically so the tutor can rewrite each.
#
# NOTE FOR ANY FUTURE EDIT OF THIS FILE: `TIER_LIBRARY_DIRECTIVE` contains a LITERAL
# `{source_id, page}` — it is showing the model the citation shape, not interpolating
# anything. So these three are resolved and NEVER `.format()`-ed. (They are safe to
# substitute INTO the lesson tail below, because `str.format` scans only the template
# it is called on, never the values it substitutes.)
TIER_LIBRARY_DIRECTIVE = (
    "THIS MODULE IS GROUNDED IN HIS LIBRARY. Teach it from the pages above, "
    "in your own words, and cite the page you used on every section's "
    "citations array — {source_id, page} against the <source> ids and [p.N] "
    "markers you were given. Cite ONLY pages you actually read. Do not invent "
    "a page number; an empty citations array is always better than a wrong "
    "one."
)
TIER_LIBRARY_SLICE_ID = "lesson.tier_library"

TIER_WEB_DIRECTIVE = (
    "His library does not cover this module, and it needs current "
    "information. Write it from your general knowledge, and say plainly "
    "inside the prose where a fact would need checking against a current "
    "source. Cite nothing to his library — you did not read it there. "
    "Leave every citations array empty."
)
TIER_WEB_SLICE_ID = "lesson.tier_web"

TIER_GENERAL_DIRECTIVE = (
    "HIS LIBRARY DOES NOT COVER THIS MODULE — he has agreed to it being written "
    "from your general knowledge instead, and it will be LABELLED as such for "
    "him. So write it well, but cite NOTHING to his library: you did not read "
    "this there. Leave every citations array empty. A fabricated page number is "
    "the one thing that would make this dishonest."
)
TIER_GENERAL_SLICE_ID = "lesson.gap"


def _tier_directive(tier: str, source=None) -> str:
    if tier == TIER_LIBRARY:
        return resolve(source, TIER_LIBRARY_SLICE_ID, TIER_LIBRARY_DIRECTIVE)
    if tier == TIER_WEB:
        return resolve(source, TIER_WEB_SLICE_ID, TIER_WEB_DIRECTIVE)
    return resolve(source, TIER_GENERAL_SLICE_ID, TIER_GENERAL_DIRECTIVE)


# THE LESSON PROMPT, lifted out of the builder byte-identically so the tutor can
# rewrite it. This and `curriculum.outline` are the two Chris named — they are where
# his teaching philosophy belongs, and until now he could not reach either.
#
# The conditional tails (`{course_brief_block}` … `{deepen_block}`) are empty strings
# when absent, which is exactly what the `if course_brief:` appends did.
# `tests/test_prompts_byte_identity.py` holds that claim to the byte.
LESSON_TAIL = (
    "YOUR TASK: write ONE complete lesson — the actual pages the tutor will "
    "teach from, not a plan for them.\n"
    "\nCOURSE: {course_title}\n"
    "MODULE: {module_title} — {module_objective}\n"
    "LESSON: {lesson_title} — {lesson_objective}\n"
    "POSITION: {position}. Do not re-teach what earlier lessons covered; "
    "build on it.\n"
    "\nLENGTH IS NOT OPTIONAL. This lesson is {teaching_minutes} minutes "
    "of teaching, and it must run to about "
    "{target_words} words in total across its sections — roughly four to "
    "five pages. A lesson under {floor_words} words is a rejected "
    "lesson: it will be sent back to you to be written properly. Write the "
    "theory out in full, in real paragraphs. Do not write bullet points and "
    "call them a lesson.\n"
    "\n{tier_directive}\n"
    "\n{language_directive}\n"
    "\n{style_directive}"
    "{course_brief_block}"
    "{student_brief_block}"
    "{retrieved_block}"
    "{revise_block}"
    "{deepen_block}"
    "\n\n{answer_in}"
)
LESSON_SLICE_ID = "lesson.draft"

LESSON_COURSE_BRIEF_BLOCK = "\n\nWHAT THE TUTOR WANTS FROM THIS COURSE:\n{course_brief}"
LESSON_STUDENT_BRIEF_BLOCK = "\n\n{student_brief}"

# The oversized-library fallback: the whole book did not fit, so this lesson gets the
# passages retrieval found for it instead. The tutor is told this on the board
# (`meta.library.full_context = false`).
LESSON_RETRIEVED_BLOCK = (
    "\n\nHis library was too large to read in full for this course, so here "
    "are the passages retrieved for THIS lesson. Ground it in these, and "
    "cite them:\n\n{retrieved}"
)
LESSON_RETRIEVED_SLICE_ID = "lesson.draft.retrieved"

# THE "revise means revise, not regenerate" BLOCK (Spec D). A `modify_lesson`
# re-draft used to see only the tutor's instruction folded into the volatile
# objective (`jobs/curriculum_draft.py:_draft_one`) — never the lesson's own
# existing content, so the model rewrote it from a blank page every time and the
# tutor's surviving material was whatever it happened to reproduce. This shows it
# the lesson AS IT STANDS, section by section, so "make the exercises harder"
# changes the exercises rather than re-teaching the whole lesson from scratch.
#
# Populated by `jobs/curriculum_draft.py:_draft_one` ONLY when a `revise_instruction`
# was consumed at claim time AND the lesson already has segments with real bodies —
# a fresh lesson (no prior draft to preserve) or an instruction-less redraft renders
# this block empty, byte-identical to before (`test_surgical_revise.py`'s
# same-kwargs pin).
LESSON_REVISE_BLOCK = (
    "\n\nΤο τρέχον περιεχόμενο του μαθήματος ακολουθεί — εφάρμοσε την "
    "αναθεώρηση και κράτησε όλα τα υπόλοιπα ουσιαστικά ανέπαφα.\n{current}"
)
LESSON_REVISE_SLICE_ID = "lesson.revise"

# THE TUTOR'S SECTIONS, SHOWN AS FIXED. On a redraft that must keep his hand-
# edited theory (or on the lesson panel, the sections he left unticked), those
# sections are removed from the guided-json schema and shown here instead, so
# the model writes the others TO FIT them. Per-section cap is generous — a
# hand-written theory can run 16K chars and must be seen whole.
LESSON_FIXED_BLOCK = (
    "\n\nΟι παρακάτω ενότητες είναι ΤΟΥ ΚΑΘΗΓΗΤΗ και ΔΕΝ αλλάζουν — δεν τις "
    "γράφεις ξανά. Γράψε τις υπόλοιπες ενότητες ώστε να δένουν απόλυτα με αυτές: "
    "ίδια ορολογία, ίδια παραδείγματα, ίδια σειρά ιδεών, καμία αντίφαση.\n{fixed}"
)
LESSON_FIXED_SLICE_ID = "lesson.fixed"
FIXED_SECTION_CHAR_LIMIT = 20_000
FIXED_SECTION_TRUNCATION_MARKER = "\n…[το υπόλοιπο περικόπηκε — υπάρχει και ισχύει]"


def _fixed_body(text: str | None) -> str:
    t = (text or "").strip()
    if len(t) <= FIXED_SECTION_CHAR_LIMIT:
        return t
    return t[:FIXED_SECTION_CHAR_LIMIT] + FIXED_SECTION_TRUNCATION_MARKER


# WHAT MUST CHANGE, SECTION BY SECTION (Task 2.2, the lesson AI panel's APPLY).
# The fixed block above says what must NOT change; this one says, for each section
# the model IS writing, the one line the planner produced (and the tutor read and
# approved) about what has to be different this time. Without it the apply call
# would carry only the tutor's overall instruction, and a section ticked for a
# reason the planner spelled out — "it now contradicts the new theory" — would be
# rewritten from the general instruction alone, i.e. from less than the tutor was
# shown on the card he approved.
#
# Rendered AFTER the fixed block, appended to the same `revise_block` value: the
# briefs are about the sections being written, so they read last, closest to the
# task. Empty/absent renders nothing — byte-identical to before this existed.
LESSON_SECTION_BRIEFS_BLOCK = (
    "\n\nΓια κάθε ενότητα που ξαναγράφεις, αυτό ακριβώς πρέπει να αλλάξει:\n{briefs}"
)
LESSON_SECTION_BRIEFS_SLICE_ID = "lesson.section_briefs"


LESSON_DEEPEN_BLOCK = (
    "\n\nYOUR PREVIOUS DRAFT CAME BACK AT {total_words} WORDS — "
    "under the {floor}-word floor. Rewrite it in full, keeping "
    "what is good, and EXPAND these sections, which are the thin ones: "
    "{thin}. Add real teaching substance — worked explanations, more "
    "exercises, the things students actually ask — not padding, and not a "
    "longer introduction.\n\nYOUR PREVIOUS DRAFT:\n"
    "{previous}"
)
LESSON_DEEPEN_SLICE_ID = "lesson.deepen"


def build_lesson_messages(
    *,
    ctx: LessonContext,
    library: LibraryContext,
    language: str,
    blueprint: dict | None = None,
    student_brief: str | None,
    course_brief: str | None,
    retrieved: str | None = None,
    deepen: Measurement | None = None,
    previous: dict | None = None,
    revise_current: dict | None = None,
    fixed_sections: dict[str, str] | None = None,
    section_briefs: dict[str, str] | None = None,
    source=None,
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

    `revise_current` turns this into the `modify_lesson` re-draft prompt (Spec D):
    `{section_or_title: body}` for the lesson's LIVE segments, built by
    `jobs/curriculum_draft.py:_draft_one` before the model call. `None`/falsy
    (a fresh lesson, or an instruction-less redraft) renders `LESSON_REVISE_BLOCK`
    empty — byte-identical to before this parameter existed.

    `fixed_sections` is `{section_key: body}` for the sections the model must NOT
    write this time — the tutor's hand-edited ones on a redraft, or the ones he
    left unticked on the lesson AI panel. They are ALSO removed from the
    guided-json schema (`build_lesson_schema(bp, exclude=...)`), so this block is
    the only way their text reaches the model: it writes the rest to fit them.
    Rendered by APPENDING to `revise_block`, not through a placeholder of its own
    — `LESSON_TAIL` is untouched, so a tutor override of `lesson.draft` written
    before this existed still renders it. `None`/`{}` is byte-identical to before.

    `section_briefs` is `{section_key: one line}` for the sections the model IS
    writing — the lesson AI panel's plan, as the tutor approved it. Appended after
    the fixed block for the same reason and with the same guarantee: keys with an
    empty brief are dropped, and an absent (or all-empty) map renders nothing.
    """
    messages = prefix_messages(library, source)

    # `blueprint` is accepted for symmetry with the rest of the draft path (and for
    # future per-section prose), but it does NOT change the tail text today: the
    # section descriptions reach the model through the guided-json SCHEMA
    # (`build_lesson_schema(blueprint)`), which `draft_lesson` passes to the provider,
    # not through this prompt. `LESSON_TAIL` is therefore untouched — the prompt
    # byte-identity tests stay green. See the plan's Task 2.
    _ = blueprint

    # ---- volatile, and strictly after the cache breakpoint ----
    deepen_block = ""
    if deepen is not None and previous is not None:
        import json

        deepen_block = resolve(
            source, LESSON_DEEPEN_SLICE_ID, LESSON_DEEPEN_BLOCK,
        ).format(
            total_words=f"{deepen.total_words:,}",
            floor=f"{deepen.floor:,}",
            thin=", ".join(deepen.thin_sections) or "all of them",
            previous=json.dumps(previous, ensure_ascii=False),
        )

    revise_block = ""
    if revise_current:
        import json

        revise_block = resolve(
            source, LESSON_REVISE_SLICE_ID, LESSON_REVISE_BLOCK,
        ).format(current=json.dumps(revise_current, ensure_ascii=False))

    fixed_block = ""
    if fixed_sections:
        import json

        fixed_block = resolve(source, LESSON_FIXED_SLICE_ID, LESSON_FIXED_BLOCK).format(
            fixed=json.dumps({k: _fixed_body(v) for k, v in fixed_sections.items()}, ensure_ascii=False),
        )

    briefs_block = ""
    # Empty briefs are dropped BEFORE the emptiness check, not after: a plan whose
    # every brief came back blank has nothing to say, and rendering the heading
    # over a bare `{}` would be an instruction that instructs nothing.
    briefs = {k: v for k, v in (section_briefs or {}).items() if v}
    if briefs:
        import json

        briefs_block = resolve(
            source, LESSON_SECTION_BRIEFS_SLICE_ID, LESSON_SECTION_BRIEFS_BLOCK,
        ).format(briefs=json.dumps(briefs, ensure_ascii=False))

    content = resolve(source, LESSON_SLICE_ID, LESSON_TAIL).format(
        course_title=ctx.course_title,
        module_title=ctx.module_title,
        module_objective=ctx.module_objective,
        lesson_title=ctx.lesson_title,
        lesson_objective=ctx.lesson_objective,
        position=ctx.position,
        teaching_minutes=ctx.teaching_minutes,
        target_words=f"{ctx.target_words:,}",
        floor_words=f"{ctx.floor_words:,}",
        tier_directive=_tier_directive(ctx.tier, source),
        language_directive=language_directive(language, source),
        style_directive=curriculum_style(language, source),
        course_brief_block=(
            LESSON_COURSE_BRIEF_BLOCK.format(course_brief=course_brief)
            if course_brief else ""
        ),
        student_brief_block=(
            LESSON_STUDENT_BRIEF_BLOCK.format(student_brief=student_brief)
            if student_brief else ""
        ),
        retrieved_block=(
            resolve(
                source, LESSON_RETRIEVED_SLICE_ID, LESSON_RETRIEVED_BLOCK,
            ).format(retrieved=retrieved) if retrieved else ""
        ),
        # Appended to the EXISTING placeholder rather than given one of its own:
        # `LESSON_TAIL` stays byte-identical, so a `lesson.draft` override the
        # tutor saved before today still renders the fixed block.
        revise_block=revise_block + fixed_block + briefs_block,
        deepen_block=deepen_block,
        answer_in=answer_in(language, source),
    )

    messages.append({"role": "user", "content": content})
    return messages


# ---------------------------------------------------------------------------
# Citations
# ---------------------------------------------------------------------------

def _iter_citation_lists(lesson: dict, blueprint: dict | None = None):
    from app.curriculum import blueprint as _bp

    bp = blueprint if blueprint is not None else _bp.default_blueprint()
    for name in _bp.section_keys(bp):
        section = lesson.get(name)
        if isinstance(section, dict) and isinstance(section.get("citations"), list):
            yield name, section


def invalid_citations(
    lesson: dict, library: LibraryContext, blueprint: dict | None = None,
) -> list[tuple[str, str, int]]:
    """Every `(section, source_ref, page)` the model cited that it was never shown.

    Checked against `LibraryContext.page_index` — the pages ACTUALLY in the prompt
    — and not against the database. Those are different sets: a page whose OCR
    produced 12 characters is in the DB and is NOT in the prompt (`corpus.
    MIN_PAGE_CHARS` drops it), so the model cannot have read it there, so a cite to
    it is a fabrication even though the row exists.
    """
    bad: list[tuple[str, str, int]] = []
    for name, section in _iter_citation_lists(lesson, blueprint):
        for cite in section["citations"]:
            if not isinstance(cite, dict):
                continue
            ref, page = cite.get("source_id"), cite.get("page")
            pages = library.page_index.get(str(ref))
            if pages is None or not isinstance(page, int) or page not in pages:
                bad.append((name, str(ref), page if isinstance(page, int) else -1))
    return bad


def strip_invalid_citations(
    lesson: dict, library: LibraryContext, blueprint: dict | None = None,
) -> dict:
    """Drop the citations that do not resolve, keep the lesson. See the module
    docstring: after one repair attempt, a false citation is the only part worth
    destroying."""
    for _name, section in _iter_citation_lists(lesson, blueprint):
        section["citations"] = [
            c for c in section["citations"]
            if isinstance(c, dict)
            and isinstance(c.get("page"), int)
            and c.get("page") in library.page_index.get(str(c.get("source_id")), ())
        ]
    return lesson


REPAIR_MESSAGE = (
    "STOP. You cited pages that do not exist in what I gave you: {detail}. "
    "The library you were shown has: {available}. The tutor CLICKS these "
    "citations and lands on the page — a wrong page number is worse than no "
    "citation at all. Produce the whole lesson again, identical in "
    "substance, citing ONLY [p.N] markers you actually read. Where you are "
    "not certain of the page, use an empty citations array."
)
REPAIR_SLICE_ID = "lesson.repair"


def _repair_message(
    bad: list[tuple[str, str, int]], library: LibraryContext, source=None,
) -> dict:
    detail = "; ".join(f"{name} cites {ref} p.{page}" for name, ref, page in bad)
    available = ", ".join(
        f"{ref} has pages {min(pages)}-{max(pages)}"
        for ref, pages in sorted(library.page_index.items())
    ) or "no sources at all"
    return {
        "role": "user",
        "content": resolve(source, REPAIR_SLICE_ID, REPAIR_MESSAGE).format(
            detail=detail, available=available,
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
    blueprint: dict | None = None,
    student_brief: str | None = None,
    course_brief: str | None = None,
    source_ids: list[uuid.UUID] | None = None,
    prompts: dict[str, str] | None = None,
    revise_current: dict | None = None,
    fixed_sections: dict[str, str] | None = None,
    exclude_sections: set[str] | frozenset[str] = frozenset(),
    section_briefs: dict[str, str] | None = None,
) -> tuple[dict, Measurement]:
    """One lesson: draft -> validate citations (one repair) -> measure -> at most
    one deepen pass. Returns `(lesson, measurement)`.

    `revise_current` (Spec D) is `_draft_one`'s snapshot of the lesson's LIVE
    segments, threaded straight through to every `build_lesson_messages` call
    below (the first draft AND any deepen pass) so a `modify_lesson` re-draft
    keeps seeing what it is revising even if it also runs long/thin and needs
    deepening. `None` on every other draft path — see that function's docstring.

    `fixed_sections`/`exclude_sections` are the two halves of drafting AROUND the
    tutor's work, and they travel together: the excluded keys leave the guided-json
    schema (the model physically cannot return them) and the same sections' text is
    shown as `LESSON_FIXED_BLOCK` so the rest is written to fit. `measure()` counts
    the drafted sections PLUS the fixed ones — a lesson whose theory the tutor wrote
    is not a thin lesson, and deepening it against a count that pretends his 900
    words do not exist would burn a model call to pad sections that are fine.

    `section_briefs` rides with them on the lesson AI panel's apply: `{key: one
    line}` for the sections that ARE being written, threaded to every
    `build_lesson_messages` call below — a deepen pass must still know what it was
    asked to change. `None` on every other draft path.

    `db` IS ONLY TOUCHED FOR THE RETRIEVAL FALLBACK, and only when the library did
    not fit whole. In the normal (full-context) path this function performs NO
    database access at all — which is what lets `jobs/curriculum_draft.py` run it
    on a worker thread holding no connection while the board polls progress on the
    request path.

    `prompts` IS AN ALREADY-RESOLVED SNAPSHOT (`overrides.snapshot`), NOT A SESSION,
    AND THAT IS THE WHOLE POINT. The tutor's prompt overrides have to reach this
    call, but reading them HERE would mean a `db.get` inside `build_lesson_messages`
    — which would check a connection back out of the pool and hold it for the entire
    multi-minute model call, once per concurrent worker, undoing the `db.close()`
    that `jobs/curriculum_draft.py:_draft_one` performs three lines before calling
    this ("HAND THE CONNECTION BACK before the model call ... This one line is what
    keeps the progress poll answering"). So Phase A resolves once and hands the
    workers a plain dict — exactly what it already does with `student_brief`.

    `None` means the code defaults, which is what an un-edited install sends.
    """
    provider = get_provider()

    # The blueprint decides the section shape the model is asked for, the sections
    # `measure` counts, and the sections whose citations are validated. `None` is the
    # code default — a byte-identical reproduction of the old fixed skeleton.
    from app.curriculum import blueprint as _bp

    bp = blueprint if blueprint is not None else _bp.default_blueprint()
    schema = _bp.build_lesson_schema(bp, exclude=set(exclude_sections))

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
        ctx=ctx, library=library, language=language, blueprint=bp,
        student_brief=student_brief, course_brief=course_brief, retrieved=retrieved,
        revise_current=revise_current, fixed_sections=fixed_sections,
        section_briefs=section_briefs, source=prompts,
    )
    lesson = provider.guided_json(messages, schema, role="draft")

    bad = invalid_citations(lesson, library, bp)
    if bad:
        log.warning("lesson %r cited %d page(s) it was never shown — repairing",
                    ctx.lesson_title, len(bad))
        repaired = provider.guided_json(
            [*messages, _repair_message(bad, library, prompts)], schema,
            role="draft",
        )
        lesson = repaired
        if invalid_citations(lesson, library, bp):
            log.warning("lesson %r still cites unknown pages after one repair — "
                        "dropping the bad citations, keeping the prose", ctx.lesson_title)
            lesson = strip_invalid_citations(lesson, library, bp)

    # The fixed sections are part of the lesson even though the model did not write
    # them this time; `measure` must see their words or the deepen loop fires on a
    # lesson that is already long enough.
    def _with_fixed(d: dict) -> dict:
        if not fixed_sections:
            return d
        return {**d, **{k: {"body": v} for k, v in fixed_sections.items() if k not in d}}

    # A FIXED SECTION IS NEVER "THIN". Its words are counted (above), but the key
    # is struck from `thin_sections` — that list goes two places that both break
    # otherwise: the deepen prompt, which would order the model to EXPAND a section
    # it physically cannot return (it is not in the schema), and the persisted
    # `meta.thin_sections`, which would show the tutor his own hand-written theory
    # flagged as the thin part of his lesson.
    def _measured(d: dict) -> Measurement:
        m = measure(_with_fixed(d), bp, teaching_minutes=ctx.teaching_minutes)
        if not fixed_sections:
            return m
        return replace(
            m, thin_sections=[k for k in m.thin_sections if k not in fixed_sections],
        )

    m = _measured(lesson)
    passes = 0
    while m.needs_deepening and passes < DEEPEN_MAX_PASSES:
        passes += 1
        log.info("lesson %r came back at %d words (floor %d) — deepen pass %d",
                 ctx.lesson_title, m.total_words, m.floor, passes)
        deeper = provider.guided_json(
            build_lesson_messages(
                ctx=ctx, library=library, language=language, blueprint=bp,
                student_brief=student_brief, course_brief=course_brief,
                retrieved=retrieved, deepen=m, previous=lesson,
                revise_current=revise_current, fixed_sections=fixed_sections,
                section_briefs=section_briefs, source=prompts,
            ),
            schema, role="draft",
        )
        if invalid_citations(deeper, library, bp):
            deeper = strip_invalid_citations(deeper, library, bp)
        deeper_m = _measured(deeper)
        # Keep the LONGER draft. A deepen pass that came back shorter has not
        # deepened anything, and silently accepting it would make the tutor's
        # "Deepen" button able to shrink his lesson.
        if deeper_m.total_words > m.total_words:
            lesson, m = deeper, deeper_m

    return lesson, m


# ---------------------------------------------------------------------------
# Persisting a drafted lesson
# ---------------------------------------------------------------------------

# How the session's minutes are spread across the sections on the printed script.
# Derived from the same weights `depth` measures against, so a section that is 25%
# of the words is 25% of the clock — the two numbers cannot drift apart.
#
# Weights are NORMALIZED by the enabled total, not read raw: the tutor edits
# weights section-by-section in the blueprint editor and disables sections
# per-course, so the enabled weights routinely sum to 0.94 or 1.10 — and a raw
# multiply would print a 47-minute clock on a 50-minute lesson. Shares of the
# whole are what he means; shares are what the clock shows. Q&A gets no special
# case any more (Chris, 2026-07-21: "if he has Q&A on his blueprint... it just
# takes it from his weight set") — a `qa`-kind section is clocked from its
# weight exactly like prose, and a disabled one simply isn't here.
def _section_minutes(
    name: str, teaching_minutes: int, blueprint: dict | None = None,
) -> int:
    from app.curriculum import blueprint as _bp

    bp = blueprint if blueprint is not None else _bp.default_blueprint()
    weights = _bp.section_weights(bp)
    total = sum(weights.values())
    share = weights[name] / total if total > 0 else 1.0 / max(1, len(weights))
    return max(1, round(share * teaching_minutes))


def _render_section(name: str, section: dict) -> str:
    """One section -> the text that lands on `Block.body`.

    `exercises.items` and `qa_prompts.items` are folded into the prose rather than
    stored as separate child blocks: they are what the tutor READS OFF THE PAGE
    while teaching, and a Q&A prompt separated from its answer key by a tree
    boundary is a Q&A prompt he cannot use.

    `strip_inline_citations` runs HERE, on the joined text, because this is the
    single point where every draft path (first draft, deepen, modify re-draft)
    turns a section dict into the string that lands on `Block.body` — the
    structured citations array is untouched, it is the prose the markers must
    never reach.

    EXERCISE HEADS CARRY NO "(N min)" SUFFIX (Chris, 2026-07-23). The model
    still budgets `est_minutes` per item — the schema keeps the field and the
    model plans with it — but the number does not print: three exercises
    stamped 10+10+8 under a section whose blueprint clock says 3′ read as a
    contradiction on the page, and the tutor paces exercises himself. The
    SECTION clock (`_section_minutes`) is the only time the page shows.
    """
    parts = [section.get("body") or ""]
    for item in section.get("items") or []:
        if not isinstance(item, dict):
            continue
        if "question" in item:
            parts.append(f"\nQ: {item.get('question', '')}\nA: {item.get('answer_key', '')}")
        else:
            parts.append(f"\n{item.get('title', '')}\n{item.get('instructions', '')}")
    return strip_inline_citations("\n".join(p for p in parts if p.strip()))


def persist_lesson(
    db,
    lesson_block,
    lesson: dict,
    m: Measurement,
    library: LibraryContext,
    blueprint: dict | None = None,
    *,
    teaching_minutes: int,
    keep: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Write the drafted sections onto `lesson_block`'s segments and flip it to
    `ready`. Caller commits.

    IN PLACE, BY `meta.section`. A segment that already exists for a blueprint
    key is UPDATED (same row, same id — an artifact attached to it stays
    attached); a key with no row gets a new one; a row whose key the blueprint
    no longer has is deleted. `keep` names sections that are NOT touched at all
    (the tutor's hand-edited sections on a redraft; the unticked sections on the
    lesson panel) — they keep body, meta and `tutor_edited`. Every rewritten
    section loses its `tutor_edited` marker: the AI just wrote it.

    STILL IDEMPOTENT, and that is what makes the Deepen button, a re-draft, and a
    Resume that re-runs a lesson whose worker died after the model call but before
    the commit all safe: writing by key can no more leave a lesson with two copies
    of its theory than the old delete-all could.

    THE OLD SEGMENTS ARE FOUND BY QUERY, NOT VIA `lesson_block.children`. The
    relationship is a CACHED collection: a caller that touched `.children` before
    calling this (which the board and every test naturally do) gets the list as it
    was AT LOAD TIME, so this function would see none of the rows it is meant to
    write over and would simply append — eight sections become sixteen, and it
    looks fine until you count. `app.lessons.edit` hit the same trap from the other
    direction (a stale collection causing a delete-orphan cascade to eat re-parented
    items) and solved it with `db.expire(..., ["children"])`; the same expire runs
    at the end here, so the caller's next read of `.children` is fresh.

    CUSTOM SEGMENTS ARE NEVER DELETED (2026-07-20, Spec A). `revise.apply_revision`'s
    `add_segment` op can surgically add a segment outside the blueprint entirely
    (`meta.custom is True`) — a redraft that blew those away along with the
    regenerated blueprint sections would silently undo a tutor's surgical edit the
    next time he re-drafted the lesson. So they are skipped by the key map below
    and re-ordered AFTER the freshly-written blueprint sections, in their prior
    relative order, with `order` continuing on from where those left off.
    """
    from sqlalchemy import select

    from app.models.block import Block

    from app.curriculum import blueprint as _bp

    bp = blueprint if blueprint is not None else _bp.default_blueprint()

    # SEGMENTS ONLY. The stray sweep below deletes non-custom rows this function
    # cannot address by key — which is right for a segment and catastrophic for a
    # child of any other kind (an item, an attachment): it has nothing to do with
    # the blueprint, and a redraft would silently eat it.
    children = db.scalars(
        select(Block)
        .where(Block.parent_id == lesson_block.id, Block.kind == "segment")
        .order_by(Block.order)
    ).all()
    customs = [b for b in children if (b.meta or {}).get("custom")]
    by_key: dict[str, Block] = {}
    # Non-custom rows this function cannot address BY KEY: no `meta.section` at all
    # (`restore.restore_version` writes one when the snapshot had no section), or a
    # second row for a key another already claimed (the eight-becomes-sixteen bug,
    # if a pre-fix database still carries it). The delete-all this replaced took
    # them; leaving them would leave a row that is never rewritten, never removed
    # and never re-ordered — so its stale `order` would collide with a real section
    # and the printed script would show two segments fighting for one slot.
    strays: list[Block] = []
    for b in children:
        key = (b.meta or {}).get("section")
        if (b.meta or {}).get("custom"):
            continue
        if key and key not in by_key:
            by_key[key] = b
        else:
            strays.append(b)

    # Fallback is "el", not "en" — i18n.py's own rule: anything in this codebase
    # still defaulting to English is a bug. A student row carrying "el-GR" used
    # to make a Greek lesson persist English section headings. `section_labels`
    # applies the same el-fallback internally; the SECTION_LABELS membership check
    # keeps `lang` to a known key so a default course's titles are byte-identical.
    lang = lesson_block.language if lesson_block.language in SECTION_LABELS else "el"
    labels = _bp.section_labels(bp, lang)
    sections_by_key = {s["key"]: s for s in bp["sections"]}
    citations_all: list[dict] = []
    seen: set[str] = set()

    order = 0
    for name in _bp.section_keys(bp):
        if name in keep and name in by_key:
            # Untouched: body, meta, `tutor_edited` and citations all stay his.
            # Only `order` moves, so the blueprint's section order still holds.
            existing = by_key[name]
            existing.order = order
            citations_all.extend((existing.meta or {}).get("citations") or [])
            seen.add(name)
            order += 1
            continue
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
        # WHOLE-DICT REASSIGNMENT, and it is load-bearing twice over: `Block.meta`
        # is plain sa.JSON with no MutableDict, and a fresh dict is what DROPS
        # `tutor_edited`/`prev_body` — the AI has just rewritten this section, so
        # the marker that says "the tutor wrote this" must not survive it.
        new_meta = {
            "section": name,
            "audience": sections_by_key[name].get("audience"),
            "citations": cites,
        }
        existing = by_key.get(name)
        if existing is not None:
            existing.title = labels[name]
            existing.body = body
            existing.est_minutes = _section_minutes(name, teaching_minutes, bp)
            existing.order = order
            existing.meta = new_meta
        else:
            db.add(Block(
                kind="segment",
                title=labels[name],
                body=body,
                est_minutes=_section_minutes(name, teaching_minutes, bp),
                order=order,
                parent_id=lesson_block.id,
                language=lesson_block.language,
                meta=new_meta,
            ))
        seen.add(name)
        order += 1

    # Rows for keys the blueprint no longer has (or that came back empty) go —
    # unless kept. Customs are not in `by_key` at all, so they never go.
    for key, row in by_key.items():
        if key not in seen and key not in keep:
            db.delete(row)
    for stray in strays:
        db.delete(stray)

    # The preserved customs (see docstring): re-ordered AFTER the blueprint
    # sections, in their prior relative order (`children` above was already
    # ordered), `order` continuing on from where the loop left off. These rows are
    # the SAME blocks — never deleted/recreated — so id/body/meta are untouched.
    for custom in customs:
        custom.order = order
        order += 1
    db.flush()

    if lesson.get("summary"):
        lesson_block.body = strip_inline_citations(lesson["summary"])

    # See the docstring: the caller's cached `children` collection is stale the
    # moment we add or delete a row. Expiring it makes the next read a fresh SELECT.
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
