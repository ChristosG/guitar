"""The lesson fan-out: 20 lessons, 2 at a time, each resumable, none able to take
the other 19 down with it.

THE CONNECTION DISCIPLINE IS THE WHOLE DESIGN, AND IT IS NOT ABOUT ELEGANCE.

Each worker opens its OWN `SessionLocal` and uses it for exactly TWO SHORT
TRANSACTIONS — claim the lesson (`queued` -> `drafting`), and later write the
result. In between it makes a Claude call that runs for 60-120 seconds, and during
that call IT HOLDS NO DATABASE CONNECTION AT ALL.

If it held one, here is what the tutor would see. The pool was 5+10. Two workers
sitting on connections for two minutes each, plus the board polling
`GET /curricula/{root}/progress` every 2 seconds from the request path — and the
poll is what loses. It times out. A timed-out progress poll on the flagship
feature is indistinguishable, from the outside, from the flagship feature being
broken. It isn't; it just cannot get a connection to say so. (The pool is also
raised — `settings.db_pool_size` — because being right about this twice is
cheaper than being wrong about it once.)

FAILURE IS PER-LESSON, AND A 429 IS NOT A FAILURE.

    an exception in lesson 7   -> lesson 7 is `failed`, the other 19 finish
    a 429 (rate limit)         -> lesson 7 goes back to `queued`, NOT `failed`

That second line is the one that matters on a fresh Anthropic account. Concurrent
32k-output drafts hit a per-minute output limit; if a 429 marked a lesson `failed`,
half the curriculum would die because the tutor generated it too fast, and the
flagship feature would look broken on exactly the demo it exists for. A `queued`
lesson is one Resume click from finished.

RESUME IS A BUTTON, NOT A DAEMON. `POST /curricula/{root}/draft` re-enqueues
whatever is still `queued`. It is a REQUEST — the only thing in this app that can
schedule a `BackgroundTask`. There is no worker process (the compose `worker`
service is a stub that sleeps), and this stage deliberately does not add one:
`jobs/sweep.py` flips interrupted `drafting` lessons back to `queued` on boot, and
the tutor clicks Resume. That is the entire recovery story, and it fits in a
paragraph.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.config import settings
from app.curriculum.corpus import (
    CurriculumContextError,
    build_curriculum_context,
    build_retrieval_context,
)
from app.curriculum.depth import floor_words, target_words
from app.curriculum.blueprint import blueprint_from_course_meta
from app.curriculum.draft import LessonContext, draft_lesson, draft_progress, persist_lesson
from app.curriculum.outline import TIER_GENERAL
from app.curriculum.segment_generate import drain_queued_segments
from app.curriculum.shape import MIN_TEACHING_MINUTES, QA_MINUTES
from app.db import SessionLocal
from app.llm.errors import LLMError, LLMNotConfigured
from app.models.block import Block
from app.models.generation_job import GenerationJob
from app.prompts import overrides
from app.students.context import build_student_brief

log = logging.getLogger(__name__)


def _queued_lesson_ids(db, root_id: uuid.UUID) -> list[uuid.UUID]:
    """Lessons still waiting, in teaching order — module order, then lesson order.

    Order matters for a reason the tutor sees: he opens the board and reads module
    1 while module 5 is still being written. Drafting in tree order is what makes
    the first thing he looks at the first thing that finishes.

    `failed` IS included. Every surface — the job error ("press Resume to
    retry"), the progress bar's failedHint, this module's own docstring — tells
    the tutor Resume retries failed lessons, and `_claim` has always accepted
    them; this list was the one link in the chain that silently filtered them
    out, so the button the UI pointed at did nothing for exactly the lessons it
    was pointed at for.
    """
    module = aliased(Block)
    rows = db.execute(
        select(Block.id, Block.meta)
        .join(module, Block.parent_id == module.id)
        .where(
            module.parent_id == root_id,
            module.kind == "module",
            Block.kind == "lesson",
        )
        .order_by(module.order, Block.order)
    ).all()
    return [
        lesson_id
        for lesson_id, meta in rows
        if (meta or {}).get("draft_status", "queued") in ("queued", "failed")
    ]


def _claim(db, lesson_id: uuid.UUID) -> tuple[Block, bool, str | None] | None:
    """Transaction 1: `queued` -> `drafting`. Returns `(lesson, deepen,
    revise_instruction)`, or None if somebody else already has it.

    THE ROW IS LOCKED FOR THE CHECK. This used to be a plain read-check-write,
    which held between two overlapping fan-outs (a double-clicked Resume, or
    Deepen pressed while the confirm fan-out was still running): both read
    `queued` in the same few milliseconds, both flipped it to `drafting`, and
    the same 60-120s, 32k-output Claude call was made — and billed — twice.
    `WITH FOR UPDATE` serializes the claimers; the loser re-reads `drafting`
    and walks away. The lock spans only this transaction (microseconds), so the
    "no connection held during the model call" discipline is untouched.

    The `deepen` flag (set by `POST /blocks/{id}/deepen`) is CONSUMED here rather
    than read later: it is an instruction for exactly this draft, and a flag left
    on the row would silently make every future Resume redraft this lesson long.

    `revise_instruction` (set by `revise.apply_revision` on a `modify_lesson` op)
    rides the SAME discipline: it is a one-shot instruction for exactly this
    re-draft, consumed here so a later unrelated Resume never re-applies it. Scrubbed
    from `meta` alongside `deepen` and returned to the worker, which folds it into
    this lesson's volatile objective (outside the cached library prefix — no cache
    impact).
    """
    lesson = db.execute(
        select(Block).where(Block.id == lesson_id).with_for_update()
    ).scalar_one_or_none()
    if lesson is None:
        db.rollback()
        return None
    meta = lesson.meta or {}
    if meta.get("draft_status", "queued") not in ("queued", "failed"):
        db.rollback()
        return None
    deepen = bool(meta.get("deepen"))
    revise_instruction = meta.get("revise_instruction")
    lesson.meta = {
        **{k: v for k, v in meta.items() if k not in ("deepen", "revise_instruction")},
        "draft_status": "drafting",
        "error": None,
    }
    db.commit()
    return lesson, deepen, revise_instruction


def _release(db, lesson_id: uuid.UUID, status: str, error: str | None = None) -> None:
    lesson = db.get(Block, lesson_id)
    if lesson is None:
        return
    lesson.meta = {**(lesson.meta or {}), "draft_status": status, "error": error}
    db.commit()


# A deepen pass asks for 40% more than the lesson's own target — enough that the
# tutor SEES the difference (2,200 -> 3,080 words is another two pages), and not so
# much that the model starts padding to reach a number. The floor rises with it, so
# `draft_lesson`'s own thin-check is measuring against the new ask, not the old one.
DEEPEN_TARGET_RATIO = 1.4

# `revise_current`'s per-section cap. A typical Greek section runs ~300-450 words,
# ~1800-2700 chars — routinely OVER this, not under it. Truncating silently would
# hand the model a body that just stops mid-sentence, with no way to tell "that's
# the whole section" from "that's where I cut it off"; a model asked to PRESERVE
# content it doesn't know was cut can't. So a truncated body carries a directive
# marker naming the cut, not just the cut.
REVISE_CURRENT_CHAR_LIMIT = 2000
REVISE_CURRENT_TRUNCATION_MARKER = (
    "\n…[το υπόλοιπο περικόπηκε — διατήρησέ το ως έχει]"
)


def _revise_current_body(body: str | None) -> str:
    """One section's value in the `revise_current` map: stripped, capped at
    `REVISE_CURRENT_CHAR_LIMIT` chars, with the truncation marker appended when
    (and only when) it was actually cut."""
    text = (body or "").strip()
    if len(text) <= REVISE_CURRENT_CHAR_LIMIT:
        return text
    return text[:REVISE_CURRENT_CHAR_LIMIT] + REVISE_CURRENT_TRUNCATION_MARKER


def _lesson_size(lesson: Block, plan: dict, *, deepen: bool) -> dict:
    """How long THIS lesson should be — the course shape, unless the tutor said
    otherwise about this one specifically.

    `Block.est_minutes` is editable in the outline editor (and in the board), and a
    tutor who sets one lesson to 90 minutes has said something we must not ignore:
    drafting it to the course's 40-minute word target would hand him a lesson half
    the length of the slot he just gave it. So the words follow the minutes,
    through the same `depth.py` functions `shape.py` itself uses — there is exactly
    one words-per-minute constant in this codebase and this is not a second one.
    """
    minutes = lesson.est_minutes or plan["minutes_per_lesson"]
    if minutes == plan["minutes_per_lesson"]:
        teaching = plan["teaching_minutes"]
        target, floor = plan["target_words"], plan["floor_words"]
    else:
        teaching = max(MIN_TEACHING_MINUTES, minutes - QA_MINUTES)
        target, floor = target_words(teaching), floor_words(teaching)

    if deepen:
        target = int(target * DEEPEN_TARGET_RATIO)
        floor = int(floor * DEEPEN_TARGET_RATIO)

    return {
        "minutes": minutes,
        "teaching_minutes": teaching,
        "qa_minutes": max(0, minutes - teaching),
        "target_words": target,
        "floor_words": floor,
    }


def _draft_one(lesson_id: uuid.UUID, plan: dict) -> None:
    """One worker, one lesson, its own session, no connection held across the model
    call. Never raises — a lesson that blows up must not take the pool's other
    worker with it."""
    db = SessionLocal()
    try:
        claimed = _claim(db, lesson_id)
        if claimed is None:
            return
        lesson, deepen, revise_instruction = claimed
        module = db.get(Block, lesson.parent_id)
        course = db.get(Block, module.parent_id)
        size = _lesson_size(lesson, plan, deepen=deepen)
        # A `modify_lesson` revision rides the per-lesson VOLATILE objective — after
        # the cached library prefix, so it costs no cache — and is consumed at claim
        # time above, so a later unrelated Resume never re-applies it.
        objective = (lesson.meta or {}).get("objective") or (lesson.body or "")
        # `revise_current` (Spec D, "revise means revise, not regenerate"): the
        # lesson's LIVE segments, shown to the model alongside the instruction above
        # so the re-draft changes what the tutor asked and keeps the rest — instead
        # of rewriting from a blank page, which is all the objective fold gave it
        # before this. Built ONLY when there is something to preserve: a fresh
        # lesson (no prior draft) or an instruction-less redraft leaves this `None`,
        # and `build_lesson_messages` renders nothing for it (byte-identical to
        # before). Read while the connection is still open — see `db.close()` below.
        revise_current = None
        if revise_instruction:
            objective = f"{objective}\n\nΑναθεώρηση από τον καθηγητή: {revise_instruction}"
            segments = db.scalars(
                select(Block)
                .where(Block.parent_id == lesson.id, Block.kind == "segment")
                .order_by(Block.order)
            ).all()
            # `section_or_title`: every segment `persist_lesson` writes carries
            # `meta.section` (a blueprint key, or `custom:<slug>` for a surgically
            # added one — CUSTOM SEGMENTS ARE INCLUDED, they are part of the lesson
            # the tutor knows); the title is the fallback for a segment with none.
            # Capped at `REVISE_CURRENT_CHAR_LIMIT` chars each — enough to show the
            # model what is there without re-sending 45,000 words of a lesson it is
            # about to rewrite — via `_revise_current_body`, which appends the
            # truncation marker so a cut section reads as "keep the rest", not as
            # "this is all there ever was". A freshly-queued placeholder segment
            # (`add_segment`, body="") is excluded — there's nothing yet to preserve.
            current = {
                (seg.meta or {}).get("section") or seg.title: _revise_current_body(seg.body)
                for seg in segments if (seg.body or "").strip()
            }
            if current:
                revise_current = current
        ctx = LessonContext(
            lesson_title=lesson.title,
            lesson_objective=objective,
            module_title=module.title,
            module_objective=(module.meta or {}).get("objective") or "",
            course_title=course.title,
            tier=(module.meta or {}).get("tier") or TIER_GENERAL,
            position=plan["positions"][str(lesson_id)],
            minutes=size["minutes"],
            teaching_minutes=size["teaching_minutes"],
            target_words=size["target_words"],
            floor_words=size["floor_words"],
        )
        library = plan["library"]
        language = lesson.language
        source_ids = plan["source_ids"]
        student_brief = plan["student_brief"]
        prompts = plan["prompts"]
        course_brief = plan["course_brief"]
        # HAND THE CONNECTION BACK before the model call. `expire_on_commit=False`
        # (app/db.py) keeps every attribute read above usable afterwards; `close()`
        # on a Session with no open transaction returns its connection to the pool
        # and leaves the Session reusable — the next `db.get` simply checks one out
        # again. This one line is what keeps the progress poll answering.
        db.close()
    except Exception:
        log.exception("draft: could not set up lesson %s", lesson_id)
        try:
            _release(db, lesson_id, "failed", "could not be prepared for drafting")
        finally:
            db.close()
        return

    try:
        lesson_json, m = draft_lesson(
            db, ctx=ctx, library=library, language=language,
            blueprint=plan["blueprint"],
            student_brief=student_brief, course_brief=course_brief,
            source_ids=source_ids, prompts=prompts,
            revise_current=revise_current,
        )
    except LLMNotConfigured:
        # The key vanished mid-run (cleared in Settings, or ENCRYPTION_SECRET
        # rotated). Not this lesson's fault and not a permanent failure: back to
        # `queued`, so Resume finishes it once the key is back.
        log.warning("draft: no API key while drafting lesson %s — requeueing", lesson_id)
        _release(db, lesson_id, "queued", "no API key — open Settings, then Resume")
        db.close()
        return
    except LLMError as e:
        if e.kind == "rate_limit":
            # THE 429 RULE. Not `failed` — `queued`. See the module docstring.
            log.warning("draft: rate-limited on lesson %s — back to queued", lesson_id)
            _release(db, lesson_id, "queued", "rate limited — Resume to finish")
        else:
            log.warning("draft: lesson %s failed (%s)", lesson_id, e.kind)
            _release(db, lesson_id, "failed", str(e) or e.kind)
        db.close()
        return
    except Exception as e:
        log.exception("draft: lesson %s failed unexpectedly", lesson_id)
        _release(db, lesson_id, "failed", f"{type(e).__name__}: {e}"[:500])
        db.close()
        return

    try:
        # Transaction 2. Re-fetch: this Session's identity map has been through a
        # close() and the row may have been edited (renamed, reordered) by the
        # tutor while the model was writing.
        lesson_block = db.get(Block, lesson_id)
        if lesson_block is None:
            log.info("draft: lesson %s was deleted while it was being drafted", lesson_id)
            return
        persist_lesson(
            db, lesson_block, lesson_json, m, library, plan["blueprint"],
            qa_minutes=size["qa_minutes"], teaching_minutes=size["teaching_minutes"],
        )
        db.commit()
    except Exception:
        log.exception("draft: could not persist lesson %s", lesson_id)
        try:
            db.rollback()
            _release(db, lesson_id, "failed", "the draft could not be saved")
        except Exception:
            log.warning("draft: could not even record the failure for %s", lesson_id)
    finally:
        db.close()


def run_curriculum_draft_job(job_id: uuid.UUID) -> None:
    """Draft (or RESUME) every `queued` lesson under `params["root_id"]`.

    Phase A (this thread, one short transaction): rebuild the library context and
    the student brief once, for everybody. Phase B: `draft_concurrency` workers
    over the lessons.

    THE LIBRARY IS BUILT ONCE AND SHARED. It is a read-only value object
    (`LibraryContext`) — building it per worker would re-run `count_tokens` and
    re-read every page of the book from Postgres N times, and, far worse, could
    produce a DIFFERENT prefix string per worker if a source were edited mid-run.
    A different prefix is a cache MISS, at 1.25x, on every lesson.
    """
    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        if job is None:
            log.warning("run_curriculum_draft_job: job %s not found", job_id)
            return

        job.status = "running"
        job.progress = {"phase": "preparing"}
        db.commit()

        root_id = uuid.UUID(str(job.params["root_id"]))
        course = db.get(Block, root_id)
        if course is None:
            job.status = "failed"
            job.error_kind = "internal"
            job.error = "That curriculum no longer exists."
            db.commit()
            return

        meta = course.meta or {}
        shape = meta.get("shape") or {}
        # THE LESSON BLUEPRINT, resolved ONCE here in Phase A where a session is
        # legitimately held, then handed to every worker as plain data on the `plan`
        # dict — threaded exactly like `course_brief` (invariant #6). A course frozen
        # with its own blueprint drafts from it; a blueprintless legacy course falls
        # back to the CODE default and drafts byte-identically to before (invariant
        # #2). The settings table is never consulted on this path.
        blueprint = blueprint_from_course_meta(meta)
        # `[]` and `None` are DIFFERENT answers and stay different all the way
        # down: the interview documents [] as "deliberately none of my sources"
        # (an honest, ungrounded course), while None/missing means "everything".
        # The old `or None` coercion silently turned the tutor's explicit
        # "none" into "read the whole library" — and billed him for it.
        raw_sources = meta.get("source_ids")
        source_ids = None if raw_sources is None else [uuid.UUID(s) for s in raw_sources]
        student_id = uuid.UUID(meta["student_id"]) if meta.get("student_id") else None

        # `grounding="retrieval"` is set by the revise chain (`jobs/curriculum_revise`):
        # a revise CHANGES lessons, so each is drafted from PER-LESSON retrieval plus
        # the model's own knowledge — never the whole-library gate, and never a refusal.
        # Any other value (the normal generation/Resume path) keeps the routing below.
        grounding = job.params.get("grounding")

        if grounding == "retrieval":
            # Skip the canon router entirely: force the too-large-to-send flag so every
            # worker grounds via `draft.draft_lesson`'s `ground_topic` fallback (all
            # chunks, compiled or not). No whole library, no `CurriculumContextError`.
            library = build_retrieval_context(db, source_ids)
        else:
            # SAME ROUTING as the outline call — same source_ids, same compile
            # states, so the same representation (library or canon) and therefore the
            # same byte-identical prefix the outline warmed. Routing here and at
            # outline time must never disagree, or the fan-out is a cache miss.
            #
            # The former REFUSE case (too large to read whole AND a contributing book
            # not yet compiled) no longer fails the run: it DEGRADES to the same
            # per-lesson retrieval, which covers every chunk regardless of compile
            # status — so nothing is silently left out, which was the refusal's whole
            # concern. The happy paths are untouched: a fitting selection still reads
            # whole, a large fully-compiled selection still uses the canon.
            try:
                library = build_curriculum_context(db, source_ids)
            except CurriculumContextError as e:
                log.warning(
                    "run_curriculum_draft_job: job %s selection too large to read whole "
                    "with an uncompiled book (%s) — grounding per-lesson via retrieval "
                    "instead of refusing", job_id, e,
                )
                library = build_retrieval_context(db, source_ids)
        student_brief = build_student_brief(db, student_id)
        # THE TUTOR'S PROMPT OVERRIDES, resolved ONCE, here, where a session is
        # legitimately held — never inside a worker. `_draft_one` hands its
        # connection back before the model call on purpose (see its :215 comment and
        # this module's own docstring on the pool); a `resolve(db, ...)` down there
        # would hold one for the whole call, per worker. Same move this function
        # already makes for `student_brief` two lines up, and for the same reason.
        prompt_overrides = overrides.snapshot(db)

        lesson_ids = _queued_lesson_ids(db, root_id)
        positions = _positions(db, root_id)

        plan = {
            "library": library,
            "student_brief": student_brief,
            "prompts": prompt_overrides,
            "blueprint": blueprint,
            "course_brief": meta.get("brief"),
            "source_ids": source_ids,
            "positions": positions,
            "minutes_per_lesson": shape.get("minutes_per_lesson", 50),
            "teaching_minutes": shape.get("teaching_minutes", 40),
            "qa_minutes": shape.get("minutes_per_lesson", 50) - shape.get("teaching_minutes", 40),
            "target_words": shape.get("target_words_per_lesson", 2200),
            "floor_words": shape.get("floor_words_per_lesson", 1760),
        }

        job.progress = {"phase": "drafting"}
        db.commit()
    except Exception:
        log.exception("run_curriculum_draft_job: setup failed for job %s", job_id)
        _fail_job(db, job_id, "internal", "The draft could not be started. Try again.")
        return
    finally:
        # Phase A is over. The fan-out below must start with this connection back
        # in the pool — see the module docstring on why a held connection here is
        # what makes the progress poll time out.
        db.close()

    if lesson_ids:
        workers = min(settings.draft_concurrency, len(lesson_ids))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # `_draft_one` never raises, so there is nothing to collect and nothing
            # to re-raise — a `result()` loop here would only be able to report a
            # bug in the error handling itself. The outcome of every lesson is on
            # its own Block row, which is the only place the tutor ever looks.
            list(pool.map(lambda lid: _draft_one(lid, plan), lesson_ids))

    db = SessionLocal()
    try:
        report = draft_progress(db, root_id)
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.progress = {"phase": "done", **report}
        job.result_root_id = root_id
        if report["failed"] and report["ready"] == 0:
            job.status = "failed"
            job.error_kind = "upstream"
            job.error = "No lesson could be drafted. Check Settings, then Resume."
        else:
            # PARTIAL SUCCESS IS SUCCESS. 19 of 20 lessons drafted is a curriculum
            # the tutor can teach from tomorrow, plus one row with a Retry button —
            # not a failed job with a red banner over 19 good lessons.
            job.status = "succeeded"
            job.error = (
                f"{report['failed']} lesson(s) could not be drafted — press Resume to retry."
                if report["failed"] else None
            )
        db.commit()
    except Exception:
        log.exception("run_curriculum_draft_job: could not finalize job %s", job_id)
    finally:
        db.close()


def resume_queued_segments(root_id: uuid.UUID) -> None:
    """Resume's OTHER half (whole-branch review, finding #3): a segment
    `apply_revision`'s `add_segment`/`edit_segment` created or marked
    `segment_status == "queued"` had, until now, exactly ONE way back to
    `done` — the revise job's own apply-mode chain generating it right after
    apply, in the same run. If that chain never ran (a segment-only/blueprint-
    only plan does not even attempt one — see `_has_queued_lessons` in
    `jobs/curriculum_revise.py`), or was interrupted before reaching it (the
    process restarted mid-generation), the segment is stranded: `jobs/sweep.py`
    resets an interrupted LESSON draft on boot, but nothing does the same for
    a segment, because nothing was watching it.

    Scheduled as its OWN `BackgroundTask` alongside the existing
    `curriculum_draft` job (`resume_curriculum_draft`, `routers/curriculum.py`)
    — not folded into that job's params/report, and not a second
    `GenerationJob` row of its own. A queued segment is not a queued lesson;
    `curriculum_draft`'s job (`draft_progress`, its own `report`) counts and
    reports on LESSONS only, so wiring segments through it would mean either
    silently misreporting them or growing that job's shape for a case its own
    `report`/UI never expected. `Block.meta.segment_status` is already this
    tree's honest record of what happened to a segment (`queued` -> `done`/
    `failed`, same as `generate_segment`/`drain_queued_segments` write
    everywhere else) — good enough for a plain background drain with no
    poller of its own, same reasoning `apply_revision`'s segment ops already
    lean on.

    Opens its OWN short-lived session — same connection discipline as the
    lesson fan-out above (see the module docstring) — and never raises: a
    drain failure here must not take the request-triggered Resume down with
    it (the lesson job it runs alongside is unaffected either way)."""
    db = SessionLocal()
    try:
        drain_queued_segments(db, root_id)
    except Exception:
        log.exception("resume_queued_segments: drain failed for root %s", root_id)
    finally:
        db.close()


def _positions(db, root_id: uuid.UUID) -> dict[str, str]:
    """`{lesson_id: "lesson 3 of 4, module 2 of 5"}`.

    The model is drafting one lesson with no sight of the other 19. Without this it
    re-teaches the basics in lesson 12 — it has no way to know lesson 1 already
    did. Cheap to compute, and it is the difference between a course and twenty
    standalone lessons with the same title on top.
    """
    modules = db.scalars(
        select(Block)
        .where(Block.parent_id == root_id, Block.kind == "module")
        .order_by(Block.order)
    ).all()
    out: dict[str, str] = {}
    for m_i, module in enumerate(modules, start=1):
        lessons = db.scalars(
            select(Block)
            .where(Block.parent_id == module.id, Block.kind == "lesson")
            .order_by(Block.order)
        ).all()
        for l_i, lesson in enumerate(lessons, start=1):
            out[str(lesson.id)] = (
                f"lesson {l_i} of {len(lessons)} in module {m_i} of {len(modules)}"
            )
    return out


def _fail_job(db, job_id: uuid.UUID, kind: str, message: str) -> None:
    try:
        db.rollback()
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error_kind = kind
        job.error = message
        db.commit()
    except Exception:
        log.warning("could not record failure for job %s", job_id, exc_info=True)
