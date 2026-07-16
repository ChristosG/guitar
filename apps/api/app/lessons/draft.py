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

from app.i18n import DEFAULT_LOCALE, answer_in, language_directive
from app.llm.errors import GuidedJSONError
from app.llm.factory import get_provider
from app.models.block import Block
from app.models.knowledge import KnowledgeSource

# guided-JSON schema for {title, sessions:[{title, est_minutes,
# items:[{title, body}]}]} — a lesson drafted from a READER SELECTION, which is a
# different feature from curriculum authoring and keeps its own, smaller shape.
#
# It used to say it mirrored `curriculum.generate.MODULE_SCHEMA`, including that
# schema's `"minimum": 1` on `est_minutes`. Both are gone (Plan 13, Stage 6):
# curriculum authoring now drafts one LESSON per call against
# `curriculum.depth.LESSON_DRAFT_SCHEMA`, and — more to the point — `"minimum"` was
# never enforced by ANYTHING once the provider became Claude. `llm/schema.py`
# strips it before the call, so the SDK cannot even raise on it. `_positive_minutes`
# below is now the only thing keeping `est_minutes` positive, which is exactly why
# it exists.
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


def _page_label(page_from: int, page_to: int) -> str:
    """"page 19" for a single-page selection, "pages 19-21" for a range —
    used both in the prompt (`_build_messages`) and nowhere else; kept as a
    one-liner rather than duplicating the from==to check in two places."""
    return f"page {page_from}" if page_to == page_from else f"pages {page_from}-{page_to}"


def _build_messages(
    *, text: str, page_from: int, page_to: int, source_title: str, language: str,
) -> list[dict]:
    """Pure function: the {system,user} messages for lesson drafting — kept
    separate from `draft_lesson_from_selection` (which also calls the live
    model) so the prompt shape is unit-testable without a model or a DB,
    mirroring `app.curriculum.generate._build_messages`.

    Tool-first/imperative and short, per vllm_interract's agentic-gotchas #1.
    The PASSAGE is embedded verbatim in the user message (not summarized/
    paraphrased here) so the model — and this function's own test,
    `test_the_selected_passage_is_actually_given_to_the_model` — can verify
    it was actually given the exact text the tutor selected, not just a
    description of it. G4 (Plan 12 Task 4): the passage may now span
    `page_from`..`page_to` (a selection made in the continuous-scroll
    Reader can cross a page boundary) — `text` is still the WHOLE selected
    passage, verbatim, regardless of how many pages it spans.

    `language` is the tutor's UI locale, threaded all the way from the
    browser's `X-App-Locale` header (Plan 13, Stage 5.5 — it used to be a
    dead parameter: `SelectionIn` had no field for it, so the job runner
    always read `"en"` and a Greek tutor selecting Greek text got an English
    lesson). The PASSAGE it grounds on is usually ENGLISH regardless, which
    is exactly the case `app.i18n.language_directive` is written for, and why
    `answer_in` is repeated after the passage: those are the last tokens
    before generation and they are in the wrong language.
    """
    system = (
        "You generate ONLY the JSON lesson tree matching the given schema — "
        "no prose, no markdown, no commentary outside the JSON object. "
        "Ground the ENTIRE lesson in the PASSAGE below: every session and "
        "item must teach something the PASSAGE actually says. Do NOT invent "
        "facts, gear, techniques, citations, or URLs the PASSAGE does not "
        "support. A lesson may span multiple teaching sessions (this is a "
        "unit of instruction, not a single hour) — break it into 1-4 "
        "sessions, each with 1-5 items, that build on each other. Give every "
        "session a realistic, non-zero est_minutes.\n\n"
        f"{language_directive(language)}"
    )
    user = (
        f"Source: {source_title}, {_page_label(page_from, page_to)}\n\n"
        f"PASSAGE:\n{text}\n\n{answer_in(language)}"
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
    db, tree: dict, *, source_id: uuid.UUID, page_from: int, page_to: int, language: str,
) -> Block:
    """Recursively persist an already schema-valid `tree` as a Block
    hierarchy: lesson -> session -> item, `order` = sibling index.
    `db.flush()` after each parent (not just at the end) so its `id` — a
    Python-side `default=uuid.uuid4`, resolved at flush — is available for
    the next level's `parent_id`, mirroring `generate.py`'s `_persist_tree`.

    Provenance (B3) now records the RANGE (G4, Plan 12 Task 4): `page_no`
    is kept, set to `page_from`, so the EXISTING UI that only knows about
    `{source_id, page_no}` (the lessons list row, the editor's
    `ProvenanceChip`) keeps working unchanged and still links into the
    Reader at the start of the passage. `page_from`/`page_to` are added
    alongside it for anything that wants to show/link the whole range.
    """
    lesson = Block(
        kind="lesson", title=tree["title"], order=0, language=language,
        is_template=False, plane="content",
        target_profile={
            "provenance": {
                "source_id": str(source_id),
                "page_no": page_from,
                "page_from": page_from,
                "page_to": page_to,
            },
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
    db, *, source_id: uuid.UUID, text: str, language: str = DEFAULT_LOCALE,
    page_from: int | None = None, page_to: int | None = None,
    page_no: int | None = None,
) -> uuid.UUID:
    """Guided-JSON a lesson tree grounded in `text` (the passage the tutor
    selected from `source_id`, spanning `page_from`..`page_to`), persist it
    as a `Block` hierarchy, return the root (lesson) Block id.

    G4 (Plan 12 Task 4): the Reader's continuous scroll lets a selection
    cross a page boundary, so this grounds on the WHOLE range, not a single
    page. `page_no` is kept as a backward-compatible single-page alias — for
    direct callers (this function predates the range and existing tests
    still call it with just `page_no`) — and is resolved into an equivalent
    one-page range below. Exactly one of `page_no` or `page_from`+`page_to`
    must be given (mirrors `SelectionIn`'s own resolution at the API
    boundary, `app.schemas.lessons`, since this function is also called
    directly, off a `GenerationJob`'s params, not just through that schema).

    Raises `ValueError` if `source_id` doesn't name a real `KnowledgeSource`
    — a caller/programming error (the router validates this itself and
    returns 404 before ever calling this, mirroring `run_reingest_job`'s
    identical unknown-source guard), not a generation failure. Also raises
    `ValueError` for an inconsistent/missing page range — same "caller
    error, not a generation failure" reasoning.

    `db` is a caller-owned SQLAlchemy Session (mirrors `generate_curriculum`):
    not closed here, but committed here — the persisted tree is this
    function's entire observable output, so the commit is part of its
    contract, not left to the caller.
    """
    if page_from is None and page_to is None:
        if page_no is None:
            raise ValueError(
                "draft_lesson_from_selection requires page_from/page_to or page_no"
            )
        page_from = page_to = page_no
    elif page_from is None or page_to is None:
        raise ValueError("page_from and page_to must both be given together")
    elif page_to < page_from:
        raise ValueError("page_to must be >= page_from")

    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise ValueError(f"KnowledgeSource {source_id!r} not found")

    messages = _build_messages(
        text=text, page_from=page_from, page_to=page_to,
        source_title=source.title, language=language,
    )
    tree = get_provider().guided_json(messages, LESSON_SCHEMA)

    # SCHEMA-VALID IS NOT SUBSTANTIVE. Structured outputs cannot enforce
    # minItems (`llm/schema.py` strips it before the call), so `{"title": ...,
    # "sessions": []}` is a perfectly valid response — and it used to persist
    # as a SUCCEEDED job: a billed call, a lesson editor with nothing in it,
    # and no error explaining why. Same guard class as curriculum's
    # `enforce_shape` and the artifacts' G6 spec checks: an empty tree is a
    # failed generation, said out loud.
    sessions = tree.get("sessions") or []
    if not sessions or not any(s.get("items") for s in sessions if isinstance(s, dict)):
        raise GuidedJSONError(
            "the model returned a lesson with no sessions/items — the paid call "
            "produced nothing to teach; try a longer or cleaner selection"
        )

    lesson = _persist_tree(
        db, tree, source_id=source_id, page_from=page_from, page_to=page_to,
        language=language,
    )
    db.commit()
    return lesson.id
