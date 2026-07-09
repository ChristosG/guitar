"""Tool registry for the ReAct agent loop (`app/agent/loop.py`).

`TOOLS` maps each tool's name to a `ToolEntry`: the OpenAI-shape JSON schema
the model sees (`schema`), the Python callable the loop dispatches to
(`fn(db, **args) -> <JSON-serializable result>`), `kind` ("read" or
"mutation" — Plan 5 Task 2 registered only reads; THIS task (3) adds the
"mutation" entries into this SAME dict), and `async_job` (Task 3 — see
below). `loop.py` dispatches the two kinds differently: a "read" executes
inline, same turn; a "mutation" SUSPENDS the turn instead of executing (see
`loop.py`'s own module docstring for the suspend design).

Six reads (Task 2), each a thin wrapper over an existing read path:
  - `search_knowledge` / `explain_concept` -> `app.brain.retrieve.search` /
    `.answer` directly (already exactly the right shape).
  - `list_students` / `list_curricula` / `list_artifacts` / `get_curriculum`
    -> the SAME queries `routers/students.py` / `routers/curriculum.py` /
    `routers/artifacts.py`'s own GET routes run, reimplemented here as thin,
    router-independent queries returning plain dicts (never the routers'
    Pydantic response models, and never imported from the router modules
    themselves — small deliberate duplication over a cross-layer import,
    the same precedent `routers/artifacts.py`'s own `_get_block_or_404`/
    `routers/curriculum.py`'s `_clone_content_subtree` docstrings state for
    avoiding a cross-router import). Router functions also take `db` as a
    `Depends(...)`-defaulted LAST/keyword parameter, which doesn't fit this
    registry's uniform `fn(db, **args)` calling convention (`db` always
    first, always positional) — another reason a thin reimplementation is
    cleaner here than reusing the route handlers directly.

Seven mutations (Task 3), each wrapping a real mutation service the exact
same "thin, router-independent" way the reads above wrap their GET routes —
see each `_`-prefixed fn's own docstring for its specific service and any
deliberate duplication (`assign_curriculum`'s deep-clone in particular
mirrors `routers/curriculum.py`'s own `_clone_content_subtree`, same
cross-router-import-avoidance precedent cited above): `create_student`,
`update_student`, `segment_block`, `update_block`, `assign_curriculum`,
`generate_artifact`, `generate_curriculum`. NONE of these fns are actually
CALLED by this task's own loop change — a mutation `ToolCall` is always
suspended before `entry.fn` would ever run (see `loop.py`); they're
registered now so Task 4's approval-resolve step has a real, working
`fn(db, **args)` to call for each. `generate_curriculum` alone is marked
`async_job=True` (see `ToolEntry` below) since it's a 49-179s/call blocking
LLM generation (`app.curriculum.generate`'s own docstring) that Task 4 must
enqueue as a `GenerationJob` rather than call inline — the other six are
cheap enough to call synchronously at resolve time.

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
from app.curriculum.generate import generate_curriculum as _generate_curriculum_service
from app.curriculum.segment import segment_block as _segment_block_service
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.curriculum import Assignment
from app.models.student import Student
from app.schemas.curriculum import BlockUpdate
from app.schemas.students import StudentCreate, StudentUpdate


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool. Frozen — a registered entry is a value, same
    rationale `app.llm.tools_types.ToolCall` states for its own `frozen=True`
    (nothing should mutate a live registry entry in place; `loop.py`'s tests
    replace a whole entry via `monkeypatch.setitem` instead).

    `async_job` (Task 3): True ONLY for `generate_curriculum` — a marker for
    Task 4's resolve step, which special-cases it to enqueue a
    `GenerationJob` + background runner (Plan 8's pattern) instead of
    calling `fn` inline like every other mutation. Defaults False so every
    Task 2 read entry (and 6 of this task's 7 mutations) doesn't need to
    mention it explicitly.
    """

    schema: dict
    fn: Callable[..., Any]
    kind: str  # "read" | "mutation"
    async_job: bool = False


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

def _search_knowledge(
    db, *, query: str, k: int = 8, domain: str | None = None, language: str | None = None
) -> list[dict]:
    hits = search(db, query, k=k, domain=domain, language=language)
    return [{"source": h.source_title, "text": h.text, "score": round(h.score, 3)} for h in hits]


def _explain_concept(db, *, query: str, locale: str = "en", k: int = 8) -> dict:
    result = answer(db, query, locale=locale, k=k)
    return {
        "text": result.text,
        "citations": [
            {"n": i, "source": h.source_title} for i, h in enumerate(result.citations, start=1)
        ],
    }


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


def _clone_content_subtree(db, node: Block, *, parent_id: UUID | None, student_id: UUID) -> Block:
    """Recursively deep-clone `node`'s CONTENT-plane subtree — the exact same
    logic as `routers/curriculum.py`'s own module-private
    `_clone_content_subtree` (see that function's docstring for the full
    rationale: content-plane-only, fresh ids, `is_template=False`,
    `student_id` stamped on every node). Deliberately re-implemented here
    rather than imported from the router: that helper is module-private
    (leading underscore) and FastAPI-router-shaped, and this file's own
    module docstring already establishes "small deliberate duplication over
    a cross-router import" as this registry's precedent for exactly this
    situation (its read tools cite the very same router docstring for their
    own duplicated `_get_block_or_404`-style lookups).
    """
    clone = Block(
        parent_id=parent_id,
        order=node.order,
        kind=node.kind,
        title=node.title,
        body=node.body,
        est_minutes=node.est_minutes,
        language=node.language,
        is_template=False,
        target_profile=dict(node.target_profile) if node.target_profile else None,
        student_id=student_id,
        plane=node.plane,
    )
    db.add(clone)
    db.flush()

    children = db.scalars(
        select(Block)
        .where(Block.parent_id == node.id, Block.plane == "content")
        .order_by(Block.order)
    ).all()
    for child in children:
        _clone_content_subtree(db, child, parent_id=clone.id, student_id=student_id)

    return clone


def _assign_curriculum(db, *, root_id: str, student_id: str) -> dict:
    """Wraps `routers/curriculum.py`'s `assign_curriculum` (`POST
    /curricula/{root_id}/assign`): deep-clones the template's content
    subtree for `student_id` and records an `Assignment` audit row.
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

    new_root = _clone_content_subtree(db, template_root, parent_id=None, student_id=parsed_student_id)
    # curriculum_block_id references the TEMPLATE block, mirroring the
    # router's own Assignment row — see routers/curriculum.py's identical
    # comment for why (the audit link is "this student was assigned this
    # template", not the new clone's own id).
    db.add(Assignment(student_id=parsed_student_id, curriculum_block_id=template_root.id))
    db.commit()
    return _block_tree(new_root)


def _generate_artifact(
    db, *, kind: str, prompt: str, block_id: str | None = None, ground: bool = False,
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
    )
    return {
        "id": artifact.id, "kind": artifact.kind, "title": artifact.title,
        "spec": artifact.spec, "block_id": artifact.block_id,
    }


def _generate_curriculum(
    db, *, title: str, language: str, profile: dict,
    domain: str | None = None, target_minutes_total: int | None = None,
) -> dict:
    """Thin wrapper over `app.curriculum.generate.generate_curriculum` for
    REGISTRY COMPLETENESS ONLY — Task 4 does NOT call this fn directly at
    resolve time. `generate_curriculum` blocks for 49-179s/call (that
    module's own docstring), which is exactly why Plan 8 gave it a
    `GenerationJob` + background-runner path (`routers/curriculum.py`'s
    `POST /curricula/generate`); this `ToolEntry` is registered with
    `async_job=True` specifically so Task 4 special-cases it to enqueue a
    `GenerationJob(kind="curriculum", params=...)` + schedule
    `run_curriculum_job` instead, never calling this fn inline on the
    request path. Kept as a genuinely working function anyway (rather than a
    stub that raises) so the registry entry isn't a dead end — e.g. still
    directly callable/testable, or usable by a future non-HTTP caller.
    """
    root_id = _generate_curriculum_service(
        db, title=title, language=language, profile=profile,
        domain=domain, target_minutes_total=target_minutes_total,
    )
    return {"root_id": root_id}


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
                        "domain": {
                            "type": "string",
                            "description": "optional: only this knowledge domain, e.g. 'tone', 'theory'",
                        },
                        "language": {
                            "type": "string",
                            "description": "optional: only sources in this 2-letter language, e.g. 'en'/'el'",
                        },
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
                        "locale": {
                            "type": "string",
                            "description": "answer language, 'en' or 'el' (default 'en')",
                        },
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
                    "Generate a brand-new curriculum (course -> modules -> "
                    "lessons -> segments) from a title/profile/domain using "
                    "the LLM, grounded in the knowledge base. Slow (roughly "
                    "1-3 minutes) — runs as a background job once approved. "
                    "This is a MUTATION — it requires the tutor's explicit "
                    "approval before it starts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "the course title"},
                        "language": {
                            "type": "string",
                            "description": "2-letter language for the generated content, e.g. 'en'/'el'",
                        },
                        "profile": {
                            "type": "object",
                            "description": "student/target profile, e.g. {\"level\": \"beginner\", \"age\": 10}",
                        },
                        "domain": {
                            "type": "string",
                            "description": "optional: knowledge-base domain to ground generation in, e.g. 'tone', 'theory'",
                        },
                        "target_minutes_total": {
                            "type": "integer",
                            "description": "optional: target total course length in minutes",
                        },
                    },
                    "required": ["title", "language", "profile"],
                },
            },
        },
        fn=_generate_curriculum,
        kind="mutation",
        async_job=True,
    ),
}
