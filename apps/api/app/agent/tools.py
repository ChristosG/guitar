"""Tool registry for the ReAct agent loop (`app/agent/loop.py`).

`TOOLS` maps each tool's name to a `ToolEntry`: the OpenAI-shape JSON schema
the model sees (`schema`), the Python callable the loop dispatches to
(`fn(db, **args) -> <JSON-serializable result>`), `kind` ("read" or
"mutation" — Plan 5 Task 2 registered only reads; Task 3 added the first
"mutation" entries into this SAME dict; Plan 6 Task 6 wired in three more
mutations the identical way), and `async_job` (Task 3 — see below). `loop.py`
dispatches the two kinds differently: a "read" executes inline, same turn; a
"mutation" SUSPENDS the turn instead of executing (see `loop.py`'s own module
docstring for the suspend design).

Six reads (Task 2), each a thin wrapper over an existing read path:
  - `search_knowledge` / `explain_concept` -> `app.brain.retrieve.search` /
    `.answer` directly (already exactly the right shape).
  - `list_students` / `list_curricula` / `list_artifacts` / `get_curriculum`
    -> the SAME queries `routers/students.py` / `routers/curriculum.py` /
    `routers/artifacts.py`'s own GET routes run, reimplemented here as thin,
    router-independent queries returning plain dicts (never the routers'
    Pydantic response models, and never imported from the router modules
    themselves — small deliberate duplication over a cross-layer import,
    the same precedent `routers/artifacts.py`'s own `_get_block_or_404`
    docstring states for avoiding a cross-router import). Router functions
    also take `db` as a
    `Depends(...)`-defaulted LAST/keyword parameter, which doesn't fit this
    registry's uniform `fn(db, **args)` calling convention (`db` always
    first, always positional) — another reason a thin reimplementation is
    cleaner here than reusing the route handlers directly.

A SEVENTH read (Plan 11 Task 2, C5): `find_lesson` — resolves a lesson by a
partial, case-insensitive title match, returning each match plus its
sessions (id/title/order). Not part of Task 2's original six; added to fix a
MEASURED failure (Plan 10's live run needed three guesses to find the right
session id with no lookup tool available) — see `_find_lesson`'s own
docstring further down for the full justification.

Seven mutations (Task 3), each wrapping a real mutation service the exact
same "thin, router-independent" way the reads above wrap their GET routes —
see each `_`-prefixed fn's own docstring for its specific service:
`create_student`, `update_student`, `segment_block`, `update_block`,
`assign_curriculum`, `generate_artifact`, `generate_curriculum`.
`assign_curriculum`'s deep-clone is NOT duplicated — it and the HTTP endpoint
both call the SHARED framework-free `app.curriculum.assign.
clone_content_subtree` (extracted in Task 3 review precisely so the two can
never drift). NONE of these fns are actually
CALLED by this task's own loop change — a mutation `ToolCall` is always
suspended before `entry.fn` would ever run (see `loop.py`); they're
registered now so Task 4's approval-resolve step has a real, working
`fn(db, **args)` to call for each. `generate_curriculum` alone is marked
`async_job=True` (see `ToolEntry` below) since it's a 49-179s/call blocking
LLM generation (`app.curriculum.generate`'s own docstring) that Task 4 must
enqueue as a `GenerationJob` rather than call inline — the other six are
cheap enough to call synchronously at resolve time.

Three more mutations (Plan 6 Task 6), wired in the SAME "thin wrapper,
registered but never called by the loop itself" way, once their backing
services existed: `add_note` (wraps Plan 6 Task 1's `routers/notes.py`
`create_note`), `promote_note_to_knowledge` (wraps Task 1's `app.notes.
promote.promote_note`), and `log_progress` (wraps Task 3's `app.curriculum.
progress.upsert_progress`). These three were deferred out of Plan 5 for
exactly this reason — Plan 5's own recon (`.superpowers/sdd/progress.md`)
notes they had "NO backing model/service yet" until Plan 6 built the Note
model and the Progress/LessonLog services. None is `async_job` (all three
are cheap, single-row DB writes, same cost class as `create_student`/
`update_block`).

Four more mutations (Plan 10 Task 3, "Lesson Authoring") — the payoff of
"an agent that actually checks his lectures... and does stuff for them, e.g.
change/add something, or split/segment the sessions": `draft_lesson_from_
selection`/`split_session`/`merge_sessions`/`add_session` wrap Plan 10 Tasks
1-2's `app.lessons.draft`/`app.lessons.edit` services, the identical "thin
wrapper, registered but never called by loop.py itself" way every mutation
above is. BINDING DECISION B5: every one of these edits the tutor's own
book-derived work, so all four are `kind="mutation"` (HITL-gated by
construction — no loop.py/chat.py change needed for that, see `loop.py`'s
own module docstring and `test_agent_hitl.py`'s section (f)). `draft_
lesson_from_selection` alone is `async_job=True` (a blocking guided-JSON LLM
call, same reasoning as `generate_curriculum`); `split_session`/`merge_
sessions`/`add_session` are cheap deterministic DB writes, same cost class
as `update_block`.

Every fn (read or mutation) returns plain JSON-serializable Python objects
(dicts/lists), never a pre-stringified blob — turning that into the tool
message's `content` string (`json.dumps(result, default=str)`) is
`loop.py`'s job, not these functions'. Kept "reasonably compact" per the
brief: list tools return a handful of scalar fields per row, not the full
ORM/response-model shape, since the model re-reads the whole result on every
subsequent loop step.

A malformed/unknown id argument (the model hallucinating a non-UUID string,
or a real-but-missing id) returns a graceful `{"error": ...}` dict rather
than raising — same "guard, don't crash" spirit `loop.py` applies to unknown
tool names and malformed tool-call JSON, extended here to bad arguments
inside an otherwise-valid, known call. Applied uniformly to the mutation fns
too (not just reads) since Task 4 will need the same graceful handling when
it actually calls them.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.artifacts.generate import generate_artifact as _generate_artifact_service
from app.brain.retrieve import answer, search
from app.canon.search import search_concepts as _search_concepts_service
from app.curriculum.assign import clone_content_subtree
from app.curriculum.generate import generate_curriculum as _generate_curriculum_service
from app.curriculum.progress import upsert_progress as _upsert_progress_service
from app.curriculum.segment import segment_block as _segment_block_service
from app.i18n import DEFAULT_LOCALE
from app.lessons.draft import draft_lesson_from_selection as _draft_lesson_service
from app.lessons.edit import add_session as _add_session_service
from app.lessons.edit import merge_sessions as _merge_sessions_service
from app.lessons.edit import split_session as _split_session_service
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.curriculum import Assignment, Progress
from app.models.note import Note
from app.models.student import Student
from app.notes.promote import promote_note as _promote_note_service
from app.schemas.curriculum import BlockUpdate
from app.schemas.notes import NoteCreate
from app.schemas.students import StudentCreate, StudentUpdate
from app.text.normalize import fold


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool. Frozen — a registered entry is a value, same
    rationale `app.llm.tools_types.ToolCall` states for its own `frozen=True`
    (nothing should mutate a live registry entry in place; `loop.py`'s tests
    replace a whole entry via `monkeypatch.setitem` instead).

    `async_job` (Task 3): True for a tool too slow to call inline at resolve
    time (a blocking guided-JSON LLM call) — a marker for `chat.py`'s
    `resolve_approval`, which enqueues a `GenerationJob` + background runner
    (Plan 8's pattern) instead of calling `fn` directly. Defaults False so
    every read entry (and most mutations) doesn't need to mention it
    explicitly.

    `job_kind` (review fix, Plan 10 Task 3): the `GenerationJob.kind` value
    THIS tool enqueues when `async_job` is True — `"curriculum"` for
    `generate_curriculum`, `"lesson"` for `draft_lesson_from_selection`.
    Required whenever `async_job=True` (unused/None otherwise). Added
    because `resolve_approval` used to hardcode `kind="curriculum"` +
    `run_curriculum_job` for ANY `async_job=True` tool — harmless while
    `generate_curriculum` was the only one, but silently WRONG the moment a
    second async tool (`draft_lesson_from_selection`) was registered: a
    lesson-draft approval would enqueue a `kind="curriculum"` job and run
    `run_curriculum_job` against lesson params (TypeError -> job
    `status="failed"`). `resolve_approval` now dispatches on this field
    (plus a small job_kind -> runner lookup it owns) instead of a hardcoded
    single case, so a THIRD async tool needs no new branching logic there —
    just a `job_kind` here and one runner-lookup entry in `chat.py`.
    """

    schema: dict
    fn: Callable[..., Any]
    kind: str  # "read" | "mutation"
    async_job: bool = False
    job_kind: str | None = None


# THE LOCALE IS NOT THE MODEL'S TO CHOOSE (Plan 13, Stage 5.4).
#
# Three tools used to take the output language as a MODEL-SUPPLIED tool
# argument: `explain_concept.locale` (default "en"), `draft_lesson_from_
# selection.language` (default "en") and — worst — `generate_curriculum.
# language`, which was a REQUIRED parameter the model filled in by guessing.
# The tutor could be looking at a fully Greek UI and get an English
# curriculum because the model decided the course title "looked English".
# `generate_artifact` didn't expose one at all, so its specs were always
# English.
#
# The language is a property of the SESSION (`ChatSession.locale`, itself
# set from the browser's `X-App-Locale`), not of the model's judgment. So the
# parameters are GONE from the schemas the model sees (it cannot supply what
# it isn't offered), and this map is how the loop/router injects the real one
# at dispatch time. The fns keep the keyword — with an `el` default, never
# `en` — because they are also called directly (tests, the job runner, a
# future non-chat caller).
#
# Values are the PARAMETER NAME each fn actually uses; the repo is not
# consistent about `locale` vs `language` and this map is not the place to
# start renaming public function signatures.
LOCALE_ARG: dict[str, str] = {
    "explain_concept": "locale",
    "generate_artifact": "locale",
    "generate_curriculum": "language",
    "draft_lesson_from_selection": "language",
}


def with_locale(tool_name: str, arguments: dict, locale: str) -> dict:
    """`arguments` + the session's locale, for any tool that takes one.

    Returns a NEW dict — never mutates `arguments` in place. `loop.py` holds
    the model's parsed `ToolCall.arguments` and `routers/chat.py` holds an
    `ApprovalRequest.tool_args`/`edited_args` loaded off a plain `sa.JSON`
    column (no `MutableDict`: an in-place mutation there would not even
    persist), so both call sites need a value they can hand on, not a
    surprise side effect on a shared object.

    Injection is UNCONDITIONAL — it OVERWRITES whatever is already under that
    key. That is the point: a resumed/edited approval can still carry a stale
    `language` (an `ApprovalRequest` row proposed before this change, or a
    tutor hand-editing the JSON in the approval card), and the session's
    locale outranks all of it. The other direction matters too: `edited_args`
    REPLACES `tool_args` wholesale at resolve time, so a tutor who edits the
    JSON and simply drops `language` would otherwise hand `run_curriculum_job`
    a params dict with no language at all.
    """
    param = LOCALE_ARG.get(tool_name)
    if param is None:
        return dict(arguments)
    return {**arguments, param: locale}


def _parse_uuid(raw: str) -> UUID | None:
    """Tool arguments arrive as JSON strings (JSON has no native UUID type) —
    a hallucinated or malformed id must not crash the loop, so this returns
    None on failure for the caller to turn into a graceful error result.
    """
    try:
        return UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Brain reads
# ---------------------------------------------------------------------------

def _search_knowledge(db, *, query: str, k: int = 8) -> list[dict]:
    """`domain` and `language` are GONE from this tool, and their absence is the
    fix (Plan 13, Stage 4.4).

    Both were optional filters the MODEL chose the value for. `language` was the
    live hazard: under the Greek default locale the model helpfully passes
    `language="el"`, and the tutor's library is an ENGLISH book — so his entire
    corpus filtered to zero results, in the language he actually works in, and
    the model then answered from pretrained memory with nothing to show it had
    even looked. (`domain` had the identical NULL-exclusion shape; 5d77bd0 fixed
    that one and this deletes the class.) A filter whose value is guessed by the
    model and whose failure mode is a silently empty corpus is not a feature.

    `score` reported to the model is the COSINE (`Hit.vector_score`), not
    `Hit.score` — which is now an RRF fusion score in the ~0.03 range and would
    read to the model as "nothing here is relevant".
    """
    hits = search(db, query, k=k)
    return [
        {"source": h.source_title, "text": h.text, "score": round(h.vector_score, 3)}
        for h in hits
    ]


def _explain_concept(db, *, query: str, locale: str = DEFAULT_LOCALE, k: int = 8) -> dict:
    result = answer(db, query, locale=locale, k=k)
    return {
        "text": result.text,
        "citations": [
            {"n": i, "source": h.source_title} for i, h in enumerate(result.citations, start=1)
        ],
    }


def _search_concepts(db, *, query: str, k: int = 6) -> list[dict]:
    """The concept canon, made answerable in chat (C8).

    This exists because `search_knowledge` STRUCTURALLY CANNOT do what it does:
    it returns chunks from ONE book and has no notion that two books disagree.
    The canon compiled every book into concepts, keeping each author's own
    position, so this returns the CROSS-BOOK picture of a topic — and when the
    books disagree, the DIVERGENCE is the answer, not a bug to smooth over. So the
    result foregrounds it: `divergence: true` plus each position under its own
    `kind` (`consensus`/`divergence`/`only_in`), every one carrying its real
    citations. The model's instruction (this tool's description) is to surface
    both sides, not average them.

    `grounding: "figure"` on a citation is the [FIGURE] contract reaching the
    surface — that page is OUR description of a picture, citable but never
    quotable as the author's words.

    Kept compact (the model re-reads the whole result on every loop step): a hit
    is its label, coverage, the divergence flag, and its positions with citations
    — not the raw claim rows.
    """
    hits = _search_concepts_service(db, query, k=k)
    out: list[dict] = []
    for hit in hits:
        label = hit.label_en
        if hit.label_el:
            label = f"{hit.label_en} / {hit.label_el}"
        out.append({
            "concept": label,
            "coverage": f"{hit.coverage} book{'s' if hit.coverage != 1 else ''}",
            "divergence": hit.divergence,
            "positions": [
                {
                    "kind": p.kind,
                    "position": p.position,
                    "books": p.books,
                    "citations": [
                        {"source": c.source_title, "pages": c.pages_label,
                         "grounding": c.grounding}
                        for c in p.citations
                    ],
                }
                for p in hit.positions
            ],
        })
    return out


# ---------------------------------------------------------------------------
# Roster / curriculum / artifact reads — thin, router-independent queries
# ---------------------------------------------------------------------------

def _list_students(db) -> list[dict]:
    students = db.scalars(select(Student).order_by(Student.created_at.desc())).all()
    return [
        {
            "id": s.id, "name": s.name, "level": s.level,
            "instrument": s.instrument, "status": s.status,
        }
        for s in students
    ]


def _list_curricula(db) -> list[dict]:
    roots = db.scalars(
        select(Block)
        .where(Block.is_template.is_(True), Block.parent_id.is_(None))
        .order_by(Block.created_at.desc())
    ).all()
    return [
        {"id": b.id, "title": b.title, "language": b.language, "created_at": b.created_at}
        for b in roots
    ]


def _list_artifacts(db, *, block_id: str | None = None, kind: str | None = None) -> list[dict] | dict:
    stmt = select(Artifact).order_by(Artifact.created_at.desc())
    if block_id is not None:
        parsed = _parse_uuid(block_id)
        if parsed is None:
            return {"error": f"invalid block_id: {block_id!r}"}
        stmt = stmt.where(Artifact.block_id == parsed)
    if kind is not None:
        stmt = stmt.where(Artifact.kind == kind)
    artifacts = db.scalars(stmt).all()
    return [
        {"id": a.id, "kind": a.kind, "title": a.title, "tags": a.tags, "block_id": a.block_id}
        for a in artifacts
    ]


def _block_tree(block: Block) -> dict:
    """Recursive content-tree summary for `get_curriculum` — same shape
    `routers/curriculum.py`'s `block_to_tree` returns, minus `order`/
    `language`/`plane`/`student_id` (internal bookkeeping the chat model has
    no use for; `order` in particular is redundant once children are already
    returned pre-sorted). Deliberately NOT imported from that module — see
    this module's docstring for why. Faithfully mirrors `block_to_tree`'s
    one quirk too: no `plane` filter on `block.children`, so a curriculum
    that has already been segmented would show its delivery-plane child
    alongside its content children here exactly as `GET /curricula/{id}`
    does today — not "fixing" a pre-existing characteristic of the route
    this tool mirrors while implementing an unrelated task.
    """
    return {
        "id": block.id,
        "kind": block.kind,
        "title": block.title,
        "body": block.body,
        "est_minutes": block.est_minutes,
        "children": [_block_tree(c) for c in sorted(block.children, key=lambda b: b.order)],
    }


def _get_curriculum(db, *, root_id: str) -> dict:
    parsed = _parse_uuid(root_id)
    if parsed is None:
        return {"error": f"invalid root_id: {root_id!r}"}
    block = db.get(Block, parsed)
    if block is None:
        return {"error": "curriculum not found"}
    return _block_tree(block)


# ---------------------------------------------------------------------------
# find_lesson (Plan 11 Task 2, C5) — resolve a lesson by title so the model
# stops GUESSING a session uuid it was never handed.
# ---------------------------------------------------------------------------

def _find_lesson(db, *, title_query: str) -> list[dict]:
    """Fixes a MEASURED failure (Plan 10's live run, `.superpowers/sdd/
    progress.md`): the agent needed THREE attempts to split the right
    session, because nothing resolved "that lesson" by name — `split_
    session`/`merge_sessions`/`add_session` all need a real session/lesson
    uuid, and with no lookup tool the model had to guess one, repeatedly
    wrong (HITL caught every wrong guess — the gate working exactly as
    designed — but the guessing itself was the actual bug this fixes).

    A (partial, case- AND ACCENT-insensitive) title match over `Block(
    kind="lesson")` rows (see `app.lessons.draft`'s own B1 note: a lesson is
    `Block(kind="lesson")` with `Block(kind="session")` children) — `kind="read"`,
    it only ever SELECTs, same as every other read tool in this registry.

    THE ACCENT PART IS A LIVE BUG FIX (Plan 13, Stage 4.7). This used to be
    `Block.title.ilike(f"%{title_query}%")`, and **Postgres's ILIKE is
    accent-SENSITIVE**: `ILIKE '%τονικοτητα%'` does not match `Τονικότητα`. It
    lowercases, it does not fold. So a tutor asking about his own lesson in Greek
    the way people actually type — without accents — got ZERO hits, and the model
    went straight back to guessing uuids, which is the exact failure this tool
    exists to end. Greek accents also MOVE under inflection (μάθημα ->
    μαθήματα), so this is not an edge case people type their way around.

    Fixed with `app.text.normalize.fold` in PYTHON, not with an accent-insensitive
    SQL predicate: the honest SQL answer is the `unaccent` extension, which is one
    more thing to install on the tutor's iMac (and one more thing to forget) for a
    table that holds a few dozen lesson titles. Filtering `kind="lesson"` in SQL
    and folding the survivors in memory is exact, portable, and free at this size.
    """
    candidates = db.scalars(
        select(Block).where(Block.kind == "lesson").order_by(Block.created_at.desc())
    ).all()
    needle = fold(title_query)
    lessons = [b for b in candidates if needle in fold(b.title or "")]
    return _lesson_rows(db, lessons)


def _lesson_rows(db, lessons: list[Block]) -> list[dict]:
    """Shape each matched lesson for the model.

    Each match returns `id`/`title`/`provenance` — the `{"source_id",
    "page_no"}` a lesson drafted via `draft_lesson_from_selection` records on
    its root block's `target_profile` (B3), or `None` for a lesson with no
    recorded provenance (e.g. authored inside a curriculum tree rather than
    drafted from a Library selection) — PLUS `sessions` (each child
    session's `id`/`title`/`order`). The sessions are included so ONE
    `find_lesson` call is enough for the model to then call `split_session`/
    `merge_sessions`/`add_session` with a real id, instead of needing a
    SECOND round trip through `get_curriculum` just to learn which session
    ids exist under a lesson it already found by name — directly closing the
    "three attempts" gap this tool exists to fix.
    """
    results = []
    for lesson in lessons:
        provenance = None
        if isinstance(lesson.target_profile, dict):
            provenance = lesson.target_profile.get("provenance")
        sessions = db.scalars(
            select(Block)
            .where(Block.parent_id == lesson.id, Block.kind == "session")
            .order_by(Block.order)
        ).all()
        results.append({
            "id": lesson.id,
            "title": lesson.title,
            "provenance": provenance,
            "sessions": [
                {"id": s.id, "title": s.title, "order": s.order} for s in sessions
            ],
        })
    return results


# ---------------------------------------------------------------------------
# Mutations (Task 3) — NEVER called by this task's own loop change; `loop.py`
# suspends on every one of these before `entry.fn` would run. Registered here
# (real, working `fn(db, **args)` callables, not stubs) for Task 4's resolve
# step to call. Each wraps a real mutation service the same "thin,
# router-independent" way the reads above wrap their GET routes.
# ---------------------------------------------------------------------------

def _create_student(
    db, *, name: str, birthdate: str | None = None, level: str | None = None,
    instrument: str | None = None, preferred_language: str = "el",
) -> dict:
    """Wraps `routers/students.py`'s `create_student` (`POST /students`).
    Routed through `StudentCreate` (not constructed by hand) so a bad
    `birthdate` string or any future validation rule on that schema applies
    here too, instead of drifting out of sync with the HTTP boundary's own
    rules; `StudentCreate` also owns `preferred_language`'s "el" default, so
    a tool call that omits it gets the identical default the HTTP route
    would give it.
    """
    payload = StudentCreate(
        name=name, birthdate=birthdate, level=level,
        instrument=instrument, preferred_language=preferred_language,
    )
    student = Student(
        name=payload.name, birthdate=payload.birthdate, level=payload.level,
        instrument=payload.instrument, preferred_language=payload.preferred_language,
    )
    db.add(student)
    db.commit()
    return {
        "id": student.id, "name": student.name, "birthdate": student.birthdate,
        "level": student.level, "instrument": student.instrument,
        "preferred_language": student.preferred_language, "status": student.status,
    }


def _update_student(db, *, student_id: str, **fields) -> dict:
    """Wraps `routers/students.py`'s `update_student` (`PATCH
    /students/{id}`) — PATCH semantics: only the fields the caller actually
    passed in `fields` are applied (`StudentUpdate(**fields).model_dump(
    exclude_unset=True)`, exactly mirroring the router's own
    `payload.model_dump(exclude_unset=True)`), so an omitted field leaves the
    stored value untouched while an explicit `null` still clears a nullable
    one — forwarding the raw `**fields` (rather than naming every field with
    a `None` default) is what preserves that "was this key present at all"
    distinction through to `model_dump`.
    """
    parsed_id = _parse_uuid(student_id)
    if parsed_id is None:
        return {"error": f"invalid student_id: {student_id!r}"}
    student = db.get(Student, parsed_id)
    if student is None:
        return {"error": "student not found"}
    payload = StudentUpdate(**fields)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(student, field, value)
    db.commit()
    return {
        "id": student.id, "name": student.name, "birthdate": student.birthdate,
        "level": student.level, "instrument": student.instrument,
        "preferred_language": student.preferred_language, "status": student.status,
    }


def _segment_block(
    db, *, block_id: str, session_minutes: int, cadence_per_week: int = 1,
    student_id: str | None = None,
) -> dict:
    """Wraps `app.curriculum.segment.segment_block` (also `POST
    /blocks/{id}/segment`'s service). Idempotent per (block_id, student_id)
    — see that function's own docstring — so re-approving this with a
    different `session_minutes` replaces the prior delivery plan rather than
    accumulating on top of it, same as the HTTP route.
    """
    parsed_block_id = _parse_uuid(block_id)
    if parsed_block_id is None:
        return {"error": f"invalid block_id: {block_id!r}"}
    parsed_student_id = None
    if student_id is not None:
        parsed_student_id = _parse_uuid(student_id)
        if parsed_student_id is None:
            return {"error": f"invalid student_id: {student_id!r}"}
    try:
        session_ids = _segment_block_service(
            db, parsed_block_id, session_minutes=session_minutes,
            cadence_per_week=cadence_per_week, student_id=parsed_student_id,
        )
    except ValueError as e:
        # segment_block raises ValueError("block not found: ...") — graceful
        # dict, same "guard, don't crash" spirit as every read tool's id guard.
        return {"error": str(e)}
    return {"session_ids": session_ids}


def _update_block(db, *, block_id: str, **fields) -> dict:
    """Wraps `routers/curriculum.py`'s `update_block` (`PATCH
    /blocks/{id}`) — same PATCH/`exclude_unset` + `exclude_none` semantics
    (see `BlockUpdate`'s own docstring for why `null` is uniformly a no-op
    rather than a clear, even for nullable columns) and the same empty-title
    rejection, reimplemented directly here rather than importing the router
    function (which is FastAPI/`Depends`-shaped, not `fn(db, **args)`-shaped,
    and raises `HTTPException` — an HTTP-layer concept this tool layer
    doesn't use; graceful `{"error": ...}` dicts instead, same as every other
    tool in this registry).
    """
    parsed_id = _parse_uuid(block_id)
    if parsed_id is None:
        return {"error": f"invalid block_id: {block_id!r}"}
    block = db.get(Block, parsed_id)
    if block is None:
        return {"error": "block not found"}
    payload = BlockUpdate(**fields)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if updates.get("title") == "":
        return {"error": "title cannot be empty"}
    for field, value in updates.items():
        setattr(block, field, value)
    db.commit()
    return {
        "id": block.id, "kind": block.kind, "title": block.title,
        "body": block.body, "est_minutes": block.est_minutes,
    }


def _assign_curriculum(db, *, root_id: str, student_id: str) -> dict:
    """Wraps `routers/curriculum.py`'s `assign_curriculum` (`POST
    /curricula/{root_id}/assign`): deep-clones the template's content
    subtree for `student_id` (via the SHARED `app.curriculum.assign.
    clone_content_subtree` this tool and that endpoint both call — extracted
    in Task 3 review so the two never drift) and records an `Assignment`
    audit row.
    """
    parsed_root_id = _parse_uuid(root_id)
    if parsed_root_id is None:
        return {"error": f"invalid root_id: {root_id!r}"}
    parsed_student_id = _parse_uuid(student_id)
    if parsed_student_id is None:
        return {"error": f"invalid student_id: {student_id!r}"}

    template_root = db.get(Block, parsed_root_id)
    if template_root is None:
        return {"error": "curriculum not found"}
    student = db.get(Student, parsed_student_id)
    if student is None:
        return {"error": "student not found"}

    new_root = clone_content_subtree(db, template_root, parent_id=None, student_id=parsed_student_id)
    # curriculum_block_id references the TEMPLATE block, mirroring the
    # router's own Assignment row — see routers/curriculum.py's identical
    # comment for why (the audit link is "this student was assigned this
    # template", not the new clone's own id).
    db.add(Assignment(student_id=parsed_student_id, curriculum_block_id=template_root.id))
    db.commit()
    return _block_tree(new_root)


def _generate_artifact(
    db, *, kind: str, prompt: str, block_id: str | None = None, ground: bool = False,
    locale: str = DEFAULT_LOCALE,
) -> dict:
    """Wraps `app.artifacts.generate.generate_artifact` (also `POST
    /artifacts/generate`'s service) — a blocking guided-JSON LLM call
    (49-179s/call per that module's own docstring), but NOT marked
    `async_job`: unlike `generate_curriculum` below, Plan 4/8 never gave
    artifact generation a `GenerationJob` path, so Task 4 calls this inline
    at resolve time same as every other non-async mutation here.

    `block_id`, when given, is checked for existence BEFORE the slow
    generation call — mirrors `routers/artifacts.py`'s own "check first,
    don't waste a 49-179s call on an attach target that 404s" ordering.
    Anything the LLM call itself can raise (`GuidedJSONError`, a transport
    timeout, or a `pydantic.ValidationError` surviving generate_artifact's
    one repair retry) is deliberately left to propagate uncaught — those are
    genuine service failures, not "bad but plausible tool argument" cases
    like a malformed/missing id, so they don't get the graceful-dict
    treatment; Task 4's resolve endpoint maps them to HTTP status the same
    way `routers/artifacts.py`'s own `generate_artifact_endpoint` already does.
    """
    parsed_block_id = None
    if block_id is not None:
        parsed_block_id = _parse_uuid(block_id)
        if parsed_block_id is None:
            return {"error": f"invalid block_id: {block_id!r}"}
        if db.get(Block, parsed_block_id) is None:
            return {"error": "block not found"}

    artifact = _generate_artifact_service(
        db, kind=kind, prompt=prompt, block_id=parsed_block_id, ground=ground,
        locale=locale,
    )
    return {
        "id": artifact.id, "kind": artifact.kind, "title": artifact.title,
        "spec": artifact.spec, "block_id": artifact.block_id,
    }


def _generate_curriculum(
    db, *, title: str, profile: dict | None = None, language: str = DEFAULT_LOCALE,
    brief: str | None = None, weeks: int | None = None,
    minutes_per_session: int | None = None, target_minutes_total: int | None = None,
    student_id: str | None = None,
) -> dict:
    """Thin wrapper over `app.curriculum.generate.generate_curriculum` for
    REGISTRY COMPLETENESS ONLY — the loop does NOT call this fn at resolve time.
    It is registered with `async_job=True` so `routers/chat.py` enqueues a
    `GenerationJob(kind="curriculum")` and schedules `run_curriculum_job` instead,
    never calling this inline on the request path. Kept as a genuinely working
    function (rather than a stub that raises) so the registry entry isn't a dead
    end.

    `domain` IS GONE from the signature and from the schema below. It was one dead
    prompt line and a retrieval filter that could no longer exclude anything;
    `brief` is what the model should have been passing all along.

    `student_id` is NEW here, and its absence was the bug: the tool could name a
    student in `profile` and that string reached the outline prompt and nowhere
    else. Now it resolves a real `Student` and reaches every lesson draft.
    """
    parsed_student_id = None
    if student_id is not None:
        parsed_student_id = _parse_uuid(student_id)
        if parsed_student_id is None:
            return {"error": f"invalid student_id: {student_id!r}"}
        if db.get(Student, parsed_student_id) is None:
            return {"error": f"student not found: {student_id}"}

    root_id = _generate_curriculum_service(
        db, title=title, language=language, profile=profile or {}, brief=brief,
        weeks=weeks, minutes_per_session=minutes_per_session,
        target_minutes_total=target_minutes_total, student_id=parsed_student_id,
    )
    return {"root_id": root_id}


# ---------------------------------------------------------------------------
# Mutations (Plan 6 Task 6) — wired the same way once their backing services
# existed: `add_note`/`promote_note_to_knowledge` wrap Plan 6 Task 1's Note
# create + promote logic; `log_progress` wraps Task 3's `app.curriculum.
# progress.upsert_progress`. Same "never called by loop.py itself, registered
# for the resolve step to dispatch once approved" treatment as the seven
# mutations above — nothing about the suspend mechanism changes for these
# (`loop.py`'s `_first_mutation_index` is generic over `kind == "mutation"`).
# ---------------------------------------------------------------------------

_ALLOWED_PROGRESS_STATUSES = {"not_started", "introduced", "practicing", "mastered"}


def _add_note(
    db, *, title: str, body: str, tags: list[str] | None = None, student_id: str | None = None,
) -> dict:
    """Wraps `routers/notes.py`'s `create_note` (`POST /notes`). Routed
    through `NoteCreate` (not constructed by hand) — same reasoning
    `_create_student` states for `StudentCreate`: a future validation rule on
    that schema (e.g. `title`'s `max_length=300`) applies here too instead of
    drifting out of sync with the HTTP boundary's own rules. Pydantic has no
    opinion on an empty-but-present title or a dangling `student_id`, so both
    of the router's own guards are reimplemented here, graceful-dict style,
    exactly like every other tool fn in this registry.
    """
    parsed_student_id = None
    if student_id is not None:
        parsed_student_id = _parse_uuid(student_id)
        if parsed_student_id is None:
            return {"error": f"invalid student_id: {student_id!r}"}
        if db.get(Student, parsed_student_id) is None:
            return {"error": "student not found"}

    payload = NoteCreate(title=title, body=body, tags=tags or [], student_id=parsed_student_id)
    if payload.title == "":
        return {"error": "title cannot be empty"}

    note = Note(
        title=payload.title, body=payload.body, tags=payload.tags,
        student_id=payload.student_id,
    )
    db.add(note)
    db.commit()
    return {"note_id": note.id, "title": note.title}


def _promote_note_to_knowledge(db, *, note_id: str) -> dict:
    """Wraps `app.notes.promote.promote_note` (also `POST
    /notes/{id}/promote`'s service): creates a `kind="text"` KnowledgeSource
    from the note's title/body and flips `promoted_to_knowledge`. Mirrors
    `routers/notes.py`'s `promote_note_endpoint`'s three precondition guards
    (missing note, already-promoted, empty body) as graceful `{"error": ...}`
    dicts instead of that endpoint's 404/409/422 `HTTPException`s — same
    "guard, don't crash" spirit as every other tool fn in this registry.

    `promote_note` itself also raises `ValueError` when ingestion didn't
    complete (source status != "ready" — the flag is deliberately NOT
    flipped in that case, see its docstring / whole-plan review Important 2);
    caught here into a graceful dict too, same as `_segment_block` catches
    `segment_block`'s `ValueError`. The note's `promoted_to_knowledge` stays
    False, so a retry stays possible.
    """
    parsed_id = _parse_uuid(note_id)
    if parsed_id is None:
        return {"error": f"invalid note_id: {note_id!r}"}
    note = db.get(Note, parsed_id)
    if note is None:
        return {"error": "note not found"}
    if note.promoted_to_knowledge:
        return {"error": "note already promoted to knowledge"}
    if not note.body or not note.body.strip():
        return {"error": "cannot promote a note with an empty body"}

    try:
        source = _promote_note_service(db, note)
    except ValueError as e:
        return {"error": str(e)}
    return {"note_id": note.id, "title": note.title, "source_id": source.id}


def _log_progress(
    db, *, student_id: str, block_id: str, status: str, notes: str | None = None,
) -> dict:
    """Wraps `app.curriculum.progress.upsert_progress` (also `POST
    /students/{id}/progress`'s service) — insert-or-update the single
    Progress row for (student_id, block_id); see that function's own
    docstring for the upsert/overwrite semantics (a fresh call always states
    the row's new status/notes wholesale, never merges).

    `status` is checked against `_ALLOWED_PROGRESS_STATUSES` here even though
    `Progress.status` is itself a soft, unconstrained string column (`app.
    models.curriculum.Progress`'s own comment: "soft, relabelable") — the
    HTTP route trusts its caller (a fixed-choice UI control) to only ever
    send one of the four values, but a chat model has no such fixed picker
    and could otherwise persist an invented status string, so this tool
    layer adds the guard the HTTP boundary doesn't need.

    `notes` is PRESERVE-BY-DEFAULT here, unlike the HTTP route (whole-plan
    review, Important 1): `upsert_progress` overwrites notes WHOLESALE (its
    own docstring — an omitted `notes` clears the row's prior note). The web
    UI copes by resending the existing `notes` unchanged on every status
    click (`progress-row.tsx`), but the AGENT structurally CAN'T — there is
    no read tool exposing a student's Progress rows, so a plain "mark barre
    chords as mastered for Maria" makes the model call this with `notes`
    OMITTED (-> None), which would silently NULL a previously-recorded note,
    and the approval card (proposed args only) can't even show the loss. So
    when `notes is None` AND a Progress row already exists for this
    (student, block), its current `notes` is carried through to the service
    rather than None. An explicitly-provided `notes` (the model restating
    the note) still applies, and a brand-new row with no `notes` still
    stores None — only the "omitted on an existing row" case is preserved.
    Deliberately scoped to THIS wrapper (not `upsert_progress`/`ProgressIn`/
    the HTTP route, all left untouched): the UI's wholesale-overwrite
    contract is correct for the UI; only this one structurally-blind caller
    needs the guard.

    Student/block existence is checked here, BEFORE calling the service, for
    the same reason `routers/students.py`'s `upsert_student_progress` checks
    first (`upsert_progress`'s own docstring: "student/block existence is the
    CALLER's responsibility" — an unchecked bad id would otherwise reach
    `Progress(...)`'s insert and fail as a raw FK IntegrityError/500 instead
    of a clean graceful dict).
    """
    parsed_student_id = _parse_uuid(student_id)
    if parsed_student_id is None:
        return {"error": f"invalid student_id: {student_id!r}"}
    parsed_block_id = _parse_uuid(block_id)
    if parsed_block_id is None:
        return {"error": f"invalid block_id: {block_id!r}"}
    if status not in _ALLOWED_PROGRESS_STATUSES:
        return {
            "error": f"invalid status: {status!r}; must be one of {sorted(_ALLOWED_PROGRESS_STATUSES)}",
        }
    if db.get(Student, parsed_student_id) is None:
        return {"error": "student not found"}
    if db.get(Block, parsed_block_id) is None:
        return {"error": "block not found"}

    notes_to_apply = notes
    if notes_to_apply is None:
        existing = db.scalars(
            select(Progress).where(
                Progress.student_id == parsed_student_id,
                Progress.block_id == parsed_block_id,
            )
        ).first()
        if existing is not None:
            notes_to_apply = existing.notes  # preserve, don't clobber (see docstring)

    progress = _upsert_progress_service(
        db, student_id=parsed_student_id, block_id=parsed_block_id,
        status=status, notes=notes_to_apply,
    )
    return {"status": progress.status, "block_id": progress.block_id}


# ---------------------------------------------------------------------------
# Mutations (Plan 10 Task 3) — "Lesson Authoring" agent tools: wire the
# agent up to Plan 10 Tasks 1-2's lesson drafting/editing services, the
# SAME "thin wrapper, registered but never called by loop.py itself" way
# every mutation above is (BINDING DECISION B5: every one of these edits the
# tutor's own book-derived work, so it is `kind="mutation"` and therefore
# HITL-gated by construction — the suspend mechanism itself needed no
# change, see `test_agent_hitl.py` section (f)). `draft_lesson_from_
# selection` alone is `async_job=True`, same reasoning as `generate_
# curriculum`: a blocking guided-JSON LLM call unsuitable for a synchronous
# resolve-time dispatch. `split_session`/`merge_sessions`/`add_session` are
# cheap deterministic DB writes, same cost class as `update_block`.
# ---------------------------------------------------------------------------

def _draft_lesson_from_selection(
    db, *, source_id: str, page_no: int, text: str, language: str = DEFAULT_LOCALE,
) -> dict:
    """Wraps `app.lessons.draft.draft_lesson_from_selection` (also `POST
    /lessons/from-selection`'s service, Plan 10 Task 1) — REGISTRY
    COMPLETENESS ONLY, same status `_generate_curriculum` above has: a
    blocking guided-JSON LLM call (that function's own docstring), too slow
    for a synchronous resolve-time dispatch, which is exactly why this entry
    is `async_job=True`.

    `source_id` is parsed here (a malformed/hallucinated id never reaches
    the service); an UNKNOWN source_id, by contrast, is checked INSIDE the
    real service itself (`draft_lesson_from_selection` raises `ValueError`
    for that, BEFORE ever calling the LLM) — caught here into a graceful
    dict either way, same "guard, don't crash" spirit as every other tool fn
    in this registry.
    """
    parsed_source_id = _parse_uuid(source_id)
    if parsed_source_id is None:
        return {"error": f"invalid source_id: {source_id!r}"}
    try:
        lesson_id = _draft_lesson_service(
            db, source_id=parsed_source_id, page_no=page_no, text=text, language=language,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"lesson_id": lesson_id}


def _split_session(db, *, session_id: str, session_minutes: int) -> dict:
    """Wraps `app.lessons.edit.split_session` (also `POST /lessons/
    {lesson_id}/sessions/{session_id}/split`'s service, Plan 10 Task 2): cuts
    one over-long session into several, packed to ~session_minutes each by
    the same deterministic bin-packer `segment_block` uses. `split_session`
    raises `ValueError` for an unknown/wrong-kind block, a session with no
    items to split, or a non-positive `session_minutes` — all caught here
    into a graceful dict, same as `_segment_block` catches its own service's
    `ValueError`.

    Kept "reasonably compact" per this module's own docstring: each new
    session is summarized as id/title/est_minutes, not its full item tree
    (get_curriculum already exposes that for any block id, lesson roots
    included).
    """
    parsed_session_id = _parse_uuid(session_id)
    if parsed_session_id is None:
        return {"error": f"invalid session_id: {session_id!r}"}
    try:
        new_sessions = _split_session_service(
            db, parsed_session_id, session_minutes=session_minutes,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {
        "sessions": [
            {"id": s.id, "title": s.title, "est_minutes": s.est_minutes} for s in new_sessions
        ],
    }


def _merge_sessions(db, *, session_ids: list[str]) -> dict:
    """Wraps `app.lessons.edit.merge_sessions` (also `POST /lessons/
    {lesson_id}/sessions/merge`'s service, Plan 10 Task 2): folds >=2
    ADJACENT sessions of the same lesson into the first, concatenating items
    in order. `merge_sessions` raises `ValueError` for fewer than 2 ids, an
    unknown/wrong-kind block, sessions from different lessons, or — the
    case this task's brief specifically flags — NON-ADJACENT sessions (a
    model choosing session ids freely will eventually pick non-adjacent
    ones). All of those are caught here into a graceful dict rather than
    raising into the ReAct loop, same "guard, don't crash" spirit as every
    other tool fn in this registry.

    Every id is parsed BEFORE the service is ever called, so one malformed/
    hallucinated id among several well-formed ones still short-circuits
    cleanly (mirrors `_assign_curriculum`'s "parse every id first" order).
    """
    parsed_ids = []
    for sid in session_ids:
        parsed = _parse_uuid(sid)
        if parsed is None:
            return {"error": f"invalid session_id: {sid!r}"}
        parsed_ids.append(parsed)
    try:
        survivor = _merge_sessions_service(db, parsed_ids)
    except ValueError as e:
        return {"error": str(e)}
    return {"id": survivor.id, "title": survivor.title, "est_minutes": survivor.est_minutes}


def _add_session(
    db, *, lesson_id: str, title: str, est_minutes: int | None = None, after: str | None = None,
) -> dict:
    """Wraps `app.lessons.edit.add_session` (also `POST /lessons/{lesson_id}
    /sessions`'s service, Plan 10 Task 2): creates a new, empty session under
    `lesson_id`, appended at the end or inserted right after `after`.
    `add_session` raises `ValueError` for an unknown/wrong-kind lesson, or an
    `after` that isn't actually a session of that lesson — caught here into
    a graceful dict, same as every other tool fn in this registry.
    """
    parsed_lesson_id = _parse_uuid(lesson_id)
    if parsed_lesson_id is None:
        return {"error": f"invalid lesson_id: {lesson_id!r}"}
    parsed_after = None
    if after is not None:
        parsed_after = _parse_uuid(after)
        if parsed_after is None:
            return {"error": f"invalid after: {after!r}"}
    try:
        new_session = _add_session_service(
            db, parsed_lesson_id, title=title, est_minutes=est_minutes, after=parsed_after,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"id": new_session.id, "title": new_session.title, "est_minutes": new_session.est_minutes}


TOOLS: dict[str, ToolEntry] = {
    "search_knowledge": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": (
                    "Search the guitar-teaching knowledge base for raw excerpts "
                    "relevant to a query (technique, theory, gear, tone). Returns "
                    "a ranked list of short excerpts with their source title and "
                    "similarity score. Use explain_concept instead when you want "
                    "a ready-written, cited answer rather than raw excerpts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "what to search for"},
                        "k": {
                            "type": "integer",
                            "description": "how many results to return (default 8)",
                        },
                        # NO `domain`, NO `language`. See `_search_knowledge`'s
                        # docstring — a model-chosen `language="el"` filtered the
                        # tutor's English library to zero hits under his own
                        # default locale. The library is searched cross-lingually
                        # by `retrieve.search`, which translates the query itself.
                    },
                    "required": ["query"],
                },
            },
        },
        fn=_search_knowledge,
        kind="read",
    ),
    "explain_concept": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "explain_concept",
                "description": (
                    "Get a grounded, cited explanation of a guitar concept from "
                    "the knowledge base. Use this for 'what is X' / 'explain X' "
                    "questions — it returns a ready-to-use written answer plus "
                    "its citations, strictly grounded in retrieved material "
                    "(never invented)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "the concept or question to explain"},
                        # NO `locale` — the answer language comes from the
                        # session, injected at dispatch (`LOCALE_ARG`/
                        # `with_locale`). It used to be a model-chosen
                        # parameter that defaulted to English.
                        "k": {
                            "type": "integer",
                            "description": "how many source chunks to ground the answer in (default 8)",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        fn=_explain_concept,
        kind="read",
    ),
    "search_concepts": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "search_concepts",
                "description": (
                    "Search the CONCEPT CANON — the tutor's ENTIRE library "
                    "distilled into concepts, each carrying what EVERY book "
                    "says about it. Use this for 'what do my books say about "
                    "X?' / 'do my books agree on X?' questions (pickup height, "
                    "tone, bias, string gauge, technique). Unlike "
                    "search_knowledge, which returns raw excerpts from a SINGLE "
                    "book, this returns the CROSS-BOOK picture: where the books "
                    "agree, and — most valuable — where they DISAGREE, with each "
                    "author's own position and its real page citations. When a "
                    "result reports divergence:true, TEACH THE DISAGREEMENT: "
                    "present BOTH positions with their citations and let the "
                    "tutor choose — never average two disagreeing authors into "
                    "one bland claim. A citation with grounding:'figure' is our "
                    "description of a picture/diagram — cite it, never quote it "
                    "as the author's words."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "the concept or topic to look up across all the books",
                        },
                        "k": {
                            "type": "integer",
                            "description": "how many concepts to return (default 6)",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        fn=_search_concepts,
        kind="read",
    ),
    "list_students": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "list_students",
                "description": (
                    "List every student on the roster (id, name, level, "
                    "instrument, status). Use this to look up a student's id "
                    "before calling a tool that needs one, or to answer 'who "
                    "are my students' type questions."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        fn=_list_students,
        kind="read",
    ),
    "list_curricula": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "list_curricula",
                "description": (
                    "List every curriculum template (id, title, language, "
                    "created_at). Use this to find a curriculum's root id "
                    "before calling get_curriculum."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        fn=_list_curricula,
        kind="read",
    ),
    "list_artifacts": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "list_artifacts",
                "description": (
                    "List generated/uploaded teaching artifacts (id, kind, "
                    "title, tags, block_id), optionally filtered by the "
                    "block/lesson they're attached to or by kind (e.g. "
                    "'chord_diagram', 'tab', 'tone_recipe')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {
                            "type": "string",
                            "description": "optional: only artifacts attached to this block id (UUID)",
                        },
                        "kind": {
                            "type": "string",
                            "description": "optional: only this artifact kind",
                        },
                    },
                    "required": [],
                },
            },
        },
        fn=_list_artifacts,
        kind="read",
    ),
    "get_curriculum": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "get_curriculum",
                "description": (
                    "Get the full content tree (course -> modules -> lessons -> "
                    "segments, with titles/objectives/bodies) for one curriculum "
                    "by its root id. Call list_curricula first if you don't "
                    "already have the id."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root_id": {
                            "type": "string",
                            "description": "the curriculum's root block id (UUID), from list_curricula",
                        },
                    },
                    "required": ["root_id"],
                },
            },
        },
        fn=_get_curriculum,
        kind="read",
    ),
    "find_lesson": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "find_lesson",
                "description": (
                    "Find a lesson by a partial, case-insensitive title "
                    "match (e.g. \"pick gauge\" matches \"Pick Gauge and "
                    "Tone\"). Use this BEFORE split_session/merge_sessions/"
                    "add_session whenever you don't already have a real "
                    "lesson/session id — never guess one. Returns each "
                    "match's id, title, provenance (if drafted from a "
                    "Library selection), and its sessions (id, title, "
                    "order) so one call is usually enough."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title_query": {
                            "type": "string",
                            "description": "text to match against lesson titles",
                        },
                    },
                    "required": ["title_query"],
                },
            },
        },
        fn=_find_lesson,
        kind="read",
    ),
    "create_student": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "create_student",
                "description": (
                    "Add a new student to the roster. This is a MUTATION — "
                    "it requires the tutor's explicit approval before the "
                    "student is actually created."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "the student's full name"},
                        "birthdate": {
                            "type": "string",
                            "description": "optional: birthdate as YYYY-MM-DD",
                        },
                        "level": {
                            "type": "string",
                            "description": "optional: skill level, e.g. 'beginner', 'intermediate', 'advanced'",
                        },
                        "instrument": {
                            "type": "string",
                            "description": "optional: main instrument, e.g. 'guitar', 'bass'",
                        },
                        "preferred_language": {
                            "type": "string",
                            "description": "optional: 2-letter preferred language, e.g. 'en'/'el' (default 'el')",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        fn=_create_student,
        kind="mutation",
    ),
    "update_student": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "update_student",
                "description": (
                    "Update fields on an existing student — a partial "
                    "update, only the fields you provide are changed. This "
                    "is a MUTATION — it requires the tutor's explicit "
                    "approval before it's actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_id": {
                            "type": "string",
                            "description": "the student's id (UUID), from list_students",
                        },
                        "name": {"type": "string", "description": "optional: new name"},
                        "birthdate": {"type": "string", "description": "optional: new birthdate, YYYY-MM-DD"},
                        "level": {"type": "string", "description": "optional: new skill level"},
                        "instrument": {"type": "string", "description": "optional: new main instrument"},
                        "preferred_language": {
                            "type": "string",
                            "description": "optional: new preferred language, e.g. 'en'/'el'",
                        },
                    },
                    "required": ["student_id"],
                },
            },
        },
        fn=_update_student,
        kind="mutation",
    ),
    "segment_block": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "segment_block",
                "description": (
                    "Partition a curriculum block's content into ~"
                    "session_minutes teaching sessions on the delivery "
                    "plane. Replaces any existing session plan for the same "
                    "block/student. This is a MUTATION — it requires the "
                    "tutor's explicit approval before it's actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {
                            "type": "string",
                            "description": "the curriculum block id (UUID) to segment, from list_curricula/get_curriculum",
                        },
                        "session_minutes": {
                            "type": "integer",
                            "description": "target minutes per teaching session",
                        },
                        "cadence_per_week": {
                            "type": "integer",
                            "description": "optional: sessions per week (default 1)",
                        },
                        "student_id": {
                            "type": "string",
                            "description": (
                                "optional: scope the delivery plan to this student id "
                                "(UUID); omit for the template's unscoped plan"
                            ),
                        },
                    },
                    "required": ["block_id", "session_minutes"],
                },
            },
        },
        fn=_segment_block,
        kind="mutation",
    ),
    "update_block": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "update_block",
                "description": (
                    "Update fields on a curriculum block (course/module/"
                    "lesson/segment/session) — a partial update, only the "
                    "fields you provide are changed. This is a MUTATION — "
                    "it requires the tutor's explicit approval before it's "
                    "actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string", "description": "the block id (UUID) to update"},
                        "title": {"type": "string", "description": "optional: new title (cannot be empty)"},
                        "body": {"type": "string", "description": "optional: new body/description text"},
                        "est_minutes": {"type": "integer", "description": "optional: new estimated minutes"},
                        "order": {"type": "integer", "description": "optional: new sibling order"},
                        "kind": {"type": "string", "description": "optional: new kind label"},
                    },
                    "required": ["block_id"],
                },
            },
        },
        fn=_update_block,
        kind="mutation",
    ),
    "assign_curriculum": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "assign_curriculum",
                "description": (
                    "Assign a curriculum template to a student: deep-clones "
                    "the template's content tree for that student and "
                    "records the assignment. This is a MUTATION — it "
                    "requires the tutor's explicit approval before it's "
                    "actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root_id": {
                            "type": "string",
                            "description": "the curriculum template's root block id (UUID), from list_curricula",
                        },
                        "student_id": {
                            "type": "string",
                            "description": "the student id (UUID) to assign it to, from list_students",
                        },
                    },
                    "required": ["root_id", "student_id"],
                },
            },
        },
        fn=_assign_curriculum,
        kind="mutation",
    ),
    "generate_artifact": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "generate_artifact",
                "description": (
                    "Generate a new teaching artifact (chord diagram, "
                    "scale, tab, tone recipe, signal chain, amp dial "
                    "settings, gear card) from a free-text prompt using the "
                    "LLM, and persist it. This is a MUTATION — it requires "
                    "the tutor's explicit approval before it's actually "
                    "generated and saved."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": (
                                "artifact kind, e.g. 'chord_diagram', 'scale', 'tab', "
                                "'tone_recipe', 'signal_chain', 'amp_settings', 'gear_card'"
                            ),
                        },
                        "prompt": {
                            "type": "string",
                            "description": "free-text description of what to generate",
                        },
                        "block_id": {
                            "type": "string",
                            "description": "optional: attach the artifact to this curriculum block id (UUID)",
                        },
                        "ground": {
                            "type": "boolean",
                            "description": "optional: ground the generation in the knowledge base (default false)",
                        },
                    },
                    "required": ["kind", "prompt"],
                },
            },
        },
        fn=_generate_artifact,
        kind="mutation",
    ),
    "generate_curriculum": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "generate_curriculum",
                "description": (
                    "Author a brand-new curriculum (course -> modules -> lessons) "
                    "by reading the tutor's ENTIRE library and outlining from it, "
                    "then drafting every lesson in the background. Slow — runs as "
                    "a background job once approved, and the tutor watches the "
                    "lessons arrive. This is a MUTATION — it requires his explicit "
                    "approval before it starts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "the course title"},
                        # NO `language` — it was a REQUIRED, model-chosen parameter
                        # here, which is how a Greek tutor got an English
                        # curriculum. Injected from the session at dispatch AND at
                        # suspend (`with_locale`), so the approval card, the wire
                        # message and the enqueued job's params all agree.
                        #
                        # NO `domain` either — it was one dead prompt line and a
                        # filter that could no longer filter. `brief` replaces it.
                        "brief": {
                            "type": "string",
                            "description": (
                                "what this course is FOR, in the tutor's own words — "
                                "what the student should be able to do at the end"
                            ),
                        },
                        "weeks": {
                            "type": "integer",
                            "description": "how many weeks the course runs (one session a week)",
                        },
                        "minutes_per_session": {
                            "type": "integer",
                            "description": "minutes per session, e.g. 50",
                        },
                        "student_id": {
                            "type": "string",
                            "description": (
                                "optional: the student this is for. Omit it for a "
                                "course aimed at no one in particular — that is a "
                                "normal, expected answer, not a missing field."
                            ),
                        },
                        "profile": {
                            "type": "object",
                            "description": "optional: target profile, e.g. {\"level\": \"beginner\"}",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        fn=_generate_curriculum,
        kind="mutation",
        async_job=True,
        job_kind="curriculum",
    ),
    "add_note": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "add_note",
                "description": (
                    "Add a free-form teaching note — e.g. an observation "
                    "about a student's progress, a technique they struggled "
                    "with, or a reminder for next lesson. This is a "
                    "MUTATION — it requires the tutor's explicit approval "
                    "before it's actually saved."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "a short title for the note"},
                        "body": {"type": "string", "description": "the note's content"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "optional: free-form tags, e.g. ['technique', 'chords']",
                        },
                        "student_id": {
                            "type": "string",
                            "description": (
                                "optional: the student id (UUID) this note is about, "
                                "from list_students"
                            ),
                        },
                    },
                    "required": ["title", "body"],
                },
            },
        },
        fn=_add_note,
        kind="mutation",
    ),
    "promote_note_to_knowledge": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "promote_note_to_knowledge",
                "description": (
                    "Promote an existing note into the knowledge base, so "
                    "its content can ground future search/curriculum/"
                    "artifact generation. This is a MUTATION — it requires "
                    "the tutor's explicit approval before it's actually "
                    "applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note_id": {
                            "type": "string",
                            "description": "the note id (UUID) to promote, from list_students/context",
                        },
                    },
                    "required": ["note_id"],
                },
            },
        },
        fn=_promote_note_to_knowledge,
        kind="mutation",
    ),
    "log_progress": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "log_progress",
                "description": (
                    "Record or update a student's mastery status against a "
                    "curriculum block — upserts, so calling this again for "
                    "the same student+block replaces the prior status. This "
                    "is a MUTATION — it requires the tutor's explicit "
                    "approval before it's actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "student_id": {
                            "type": "string",
                            "description": "the student id (UUID), from list_students",
                        },
                        "block_id": {
                            "type": "string",
                            "description": (
                                "the curriculum block id (UUID), from "
                                "list_curricula/get_curriculum"
                            ),
                        },
                        "status": {
                            "type": "string",
                            "description": (
                                "one of: not_started, introduced, practicing, mastered"
                            ),
                        },
                        "notes": {
                            "type": "string",
                            "description": "optional: free-text notes about this progress update",
                        },
                    },
                    "required": ["student_id", "block_id", "status"],
                },
            },
        },
        fn=_log_progress,
        kind="mutation",
    ),
    "draft_lesson_from_selection": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "draft_lesson_from_selection",
                "description": (
                    "Draft a brand-new lesson (title, teaching sessions, "
                    "items) grounded in a passage of text the tutor "
                    "selected from a source in the Library — every session "
                    "and item is generated FROM that exact passage, nothing "
                    "invented. Slow (roughly 1-3 minutes) — runs as a "
                    "background job once approved. This is a MUTATION — it "
                    "requires the tutor's explicit approval before it "
                    "starts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_id": {
                            "type": "string",
                            "description": "the knowledge source id (UUID) the passage was selected from",
                        },
                        "page_no": {
                            "type": "integer",
                            "description": "the page number the passage was selected from",
                        },
                        "text": {
                            "type": "string",
                            "description": "the exact passage text to ground the lesson in",
                        },
                        # NO `language` — same reasoning as
                        # `generate_curriculum` above: injected from the
                        # session, never chosen by the model. The passage the
                        # lesson grounds on is his ENGLISH book; the lesson
                        # is written in HIS language.
                    },
                    "required": ["source_id", "page_no", "text"],
                },
            },
        },
        fn=_draft_lesson_from_selection,
        kind="mutation",
        async_job=True,
        job_kind="lesson",
    ),
    "split_session": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "split_session",
                "description": (
                    "Split one over-long teaching session into several "
                    "shorter ones, packed to ~session_minutes each — every "
                    "item the session had is preserved, just re-grouped, in "
                    "order. This is a MUTATION — it requires the tutor's "
                    "explicit approval before it's actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "the session block id (UUID) to split "
                                "(see get_curriculum for a lesson's session ids)"
                            ),
                        },
                        "session_minutes": {
                            "type": "integer",
                            "description": "target minutes per resulting session",
                        },
                    },
                    "required": ["session_id", "session_minutes"],
                },
            },
        },
        fn=_split_session,
        kind="mutation",
    ),
    "merge_sessions": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "merge_sessions",
                "description": (
                    "Merge two or more ADJACENT teaching sessions of the "
                    "same lesson into one, concatenating their items in "
                    "order. Non-adjacent sessions are rejected (merging "
                    "them would silently reorder whatever sits between "
                    "them). This is a MUTATION — it requires the tutor's "
                    "explicit approval before it's actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "at least 2 session block ids (UUID), in the "
                                "order to concatenate them; must be adjacent "
                                "sessions of the same lesson (see "
                                "get_curriculum for a lesson's session ids)"
                            ),
                        },
                    },
                    "required": ["session_ids"],
                },
            },
        },
        fn=_merge_sessions,
        kind="mutation",
    ),
    "add_session": ToolEntry(
        schema={
            "type": "function",
            "function": {
                "name": "add_session",
                "description": (
                    "Add a new, empty teaching session to a lesson — "
                    "appended at the end by default, or inserted right "
                    "after another session. This is a MUTATION — it "
                    "requires the tutor's explicit approval before it's "
                    "actually applied."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lesson_id": {
                            "type": "string",
                            "description": "the lesson block id (UUID) to add a session to",
                        },
                        "title": {"type": "string", "description": "the new session's title"},
                        "est_minutes": {
                            "type": "integer",
                            "description": "optional: estimated minutes for the new session",
                        },
                        "after": {
                            "type": "string",
                            "description": (
                                "optional: insert immediately after this "
                                "existing session id (UUID); omit to append "
                                "at the end"
                            ),
                        },
                    },
                    "required": ["lesson_id", "title"],
                },
            },
        },
        fn=_add_session,
        kind="mutation",
    ),
}
