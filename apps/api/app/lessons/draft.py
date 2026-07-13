"""Draft a Lesson from a Library selection (Plan 10 Task 1 — "Lesson
Authoring").

Where this fits: Plan 9 put the tutor's book INTO the app — OCR'd,
page-addressable, searchable, citable (`app.brain`/`routers/library.py`).
This is the payoff: turning a passage he selected while reading into a
LESSON he can actually teach — one that may span multiple sessions (his own
words: "when i say lesson i not mean 1-hour session but more like the whole
lesson which might take multiple sessions").

BINDING DECISION B1 — NO NEW TABLE. A Lesson is `Block(kind="lesson")` with
`Block(kind="session")` children and `Block(kind="item")` leaves. `Block.kind`
is documented in the model itself as "soft, relabelable" — the recursive
tree, its cascade, and its ordering already exist and are tested
(`app.curriculum.generate`'s course/module/lesson/segment tree is the
precedent this mirrors).

BINDING DECISION B3 — provenance WITHOUT a migration: recorded on the lesson
ROOT block in the existing `target_profile` JSON column as
`{"provenance": {"source_id": str(source_id), "page_no": page_no}}`. This is
what lets a lesson cite the page it came from and link back to the scan.

GROUNDING IS THE WHOLE POINT: unlike `generate_curriculum` (which retrieves
Brain context via `search()`), this function is handed the passage directly
— the tutor already selected it while reading. The prompt embeds that
passage verbatim and instructs the model to build the lesson FROM it,
inventing no facts, citations, or URLs — same anti-invention posture as
`app.agent.prompts.SYSTEM_PROMPT` and `app.curriculum.generate`'s system
prompt.
"""
import uuid

from app.llm.factory import get_provider
from app.models.block import Block
from app.models.knowledge import KnowledgeSource

# guided-JSON schema for {title, sessions:[{title, est_minutes,
# items:[{title, body}]}]}. Mirrors MODULE_SCHEMA's (app.curriculum.generate) shape/verified
# keywords (`app.curriculum.generate`): `est_minutes` carries `"minimum": 1`
# (live A/B-verified there to be actually enforced, not just accepted).
LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "est_minutes": {"type": "integer", "minimum": 1},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["title", "body"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "est_minutes", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "sessions"],
    "additionalProperties": False,
}


def _build_messages(*, text: str, page_no: int, source_title: str, language: str) -> list[dict]:
    """Pure function: the {system,user} messages for lesson drafting — kept
    separate from `draft_lesson_from_selection` (which also calls the live
    model) so the prompt shape is unit-testable without a model or a DB,
    mirroring `app.curriculum.generate._build_messages`.

    Tool-first/imperative and short, per vllm_interract's agentic-gotchas #1.
    The PASSAGE is embedded verbatim in the user message (not summarized/
    paraphrased here) so the model — and this function's own test,
    `test_the_selected_passage_is_actually_given_to_the_model` — can verify
    it was actually given the exact text the tutor selected, not just a
    description of it.
    """
    system = (
        "You generate ONLY the JSON lesson tree matching the given schema — "
        "no prose, no markdown, no commentary outside the JSON object. "
        f"Write every title/body in {language} (el=Greek, en=English). "
        "Ground the ENTIRE lesson in the PASSAGE below: every session and "
        "item must teach something the PASSAGE actually says. Do NOT invent "
        "facts, gear, techniques, citations, or URLs the PASSAGE does not "
        "support. A lesson may span multiple teaching sessions (this is a "
        "unit of instruction, not a single hour) — break it into 1-4 "
        "sessions, each with 1-5 items, that build on each other. Give every "
        "session a realistic, non-zero est_minutes."
    )
    user = (
        f"Source: {source_title}, page {page_no}\n\n"
        f"PASSAGE:\n{text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _positive_minutes(value) -> int:
    """Clamp a model-supplied est_minutes to >=1 — defense in depth, mirrors
    `app.curriculum.generate._positive_minutes` exactly (same reasoning:
    LESSON_SCHEMA's `"minimum": 1` is the first guarantee, this is the
    second, independent one)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


def _persist_tree(
    db, tree: dict, *, source_id: uuid.UUID, page_no: int, language: str,
) -> Block:
    """Recursively persist an already schema-valid `tree` as a Block
    hierarchy: lesson -> session -> item, `order` = sibling index.
    `db.flush()` after each parent (not just at the end) so its `id` — a
    Python-side `default=uuid.uuid4`, resolved at flush — is available for
    the next level's `parent_id`, mirroring `generate.py`'s `_persist_tree`.
    """
    lesson = Block(
        kind="lesson", title=tree["title"], order=0, language=language,
        is_template=False, plane="content",
        target_profile={
            "provenance": {"source_id": str(source_id), "page_no": page_no},
        },
    )
    db.add(lesson)
    db.flush()

    for s_i, session in enumerate(tree.get("sessions") or []):
        session_block = Block(
            kind="session", title=session["title"],
            est_minutes=_positive_minutes(session.get("est_minutes")),
            order=s_i, parent_id=lesson.id, language=language,
            is_template=False, plane="content",
        )
        db.add(session_block)
        db.flush()

        for i_i, item in enumerate(session.get("items") or []):
            db.add(Block(
                kind="item", title=item["title"], body=item.get("body"),
                order=i_i, parent_id=session_block.id, language=language,
                is_template=False, plane="content",
            ))

    return lesson


def draft_lesson_from_selection(
    db, *, source_id: uuid.UUID, page_no: int, text: str, language: str = "en",
) -> uuid.UUID:
    """Guided-JSON a lesson tree grounded in `text` (a passage the tutor
    selected from `source_id`/`page_no`), persist it as a `Block` hierarchy,
    return the root (lesson) Block id.

    Raises `ValueError` if `source_id` doesn't name a real `KnowledgeSource`
    — a caller/programming error (the router validates this itself and
    returns 404 before ever calling this, mirroring `run_reingest_job`'s
    identical unknown-source guard), not a generation failure.

    `db` is a caller-owned SQLAlchemy Session (mirrors `generate_curriculum`):
    not closed here, but committed here — the persisted tree is this
    function's entire observable output, so the commit is part of its
    contract, not left to the caller.
    """
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise ValueError(f"KnowledgeSource {source_id!r} not found")

    messages = _build_messages(
        text=text, page_no=page_no, source_title=source.title, language=language,
    )
    tree = get_provider().guided_json(messages, LESSON_SCHEMA)

    lesson = _persist_tree(
        db, tree, source_id=source_id, page_no=page_no, language=language,
    )
    db.commit()
    return lesson.id
