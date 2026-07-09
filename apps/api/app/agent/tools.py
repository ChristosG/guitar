"""Read-only tool registry for the ReAct agent loop (`app/agent/loop.py`).

`TOOLS` maps each tool's name to a `ToolEntry`: the OpenAI-shape JSON schema
the model sees (`schema`), the Python callable the loop dispatches to
(`fn(db, **args) -> <JSON-serializable result>`), and `kind` — always "read"
in this task (Plan 5 Task 2 registers ONLY reads; Task 3 adds "mutation"
entries into this SAME dict, which `loop.py` will dispatch differently:
suspended for human approval instead of executed inline).

This task registers six reads, each a thin wrapper over an existing
read path:
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

Every fn returns plain JSON-serializable Python objects (dicts/lists), never
a pre-stringified blob — turning that into the tool message's `content`
string (`json.dumps(result, default=str)`) is `loop.py`'s job, not these
functions'. Kept "reasonably compact" per the brief: list tools return a
handful of scalar fields per row, not the full ORM/response-model shape,
since the model re-reads the whole result on every subsequent loop step.

A malformed/unknown id argument (the model hallucinating a non-UUID string,
or a real-but-missing id) returns a graceful `{"error": ...}` dict rather
than raising — same "guard, don't crash" spirit `loop.py` applies to unknown
tool names and malformed tool-call JSON, extended here to bad arguments
inside an otherwise-valid, known call.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.brain.retrieve import answer, search
from app.models.artifact import Artifact
from app.models.block import Block
from app.models.student import Student


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool. Frozen — a registered entry is a value, same
    rationale `app.llm.tools_types.ToolCall` states for its own `frozen=True`
    (nothing should mutate a live registry entry in place; `loop.py`'s tests
    replace a whole entry via `monkeypatch.setitem` instead).
    """

    schema: dict
    fn: Callable[..., Any]
    kind: str  # "read" here; Task 3 adds "mutation"


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
}
