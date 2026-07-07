"""Brain-grounded curriculum generation: a title/profile/domain -> a guided-JSON
tree shape from the LLM -> persisted as a `Block` hierarchy
(course -> module -> lesson -> segment).

Mirrors `app.brain.retrieve`'s shape (search() for grounding, a pure messages
builder, then the live call) but asks for structured JSON via `guided_json`
instead of free text, and persists a tree instead of returning an answer.
"""
import uuid

from app.brain.retrieve import search
from app.llm.factory import get_provider
from app.models.block import Block

# The nested tree shape the model must return. vLLM's guided decoding
# (response_format json_schema — see QwenVLLM.guided_json) CONSTRAINS output to
# this shape server-side, so the result is always structurally valid JSON.
# `est_minutes` carries `"minimum": 1` deliberately — live A/B-verified (see
# this task's report) that this backend actually enforces integer "minimum"
# (a control question with a true answer of 0, schema'd with minimum:1,
# reliably came back non-zero instead), unlike a 3-digit-threshold minimum
# (e.g. 100) which was only enforced as a digit-count floor, not a true clamp.
# No array "minItems" — not verified, and the prompt already asks for a
# compact-but-plural tree; deliberately not risking an unverified keyword on a
# deeply-nested schema.
CURRICULUM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "lessons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "objectives": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "est_minutes": {"type": "integer", "minimum": 1},
                                "segments": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "kind": {"type": "string"},
                                            "title": {"type": "string"},
                                            "body": {"type": "string"},
                                            "est_minutes": {"type": "integer", "minimum": 1},
                                        },
                                        "required": ["kind", "title", "body", "est_minutes"],
                                        "additionalProperties": False,
                                    },
                                },
                            },
                            "required": ["title", "objectives", "est_minutes", "segments"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "objective", "lessons"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "modules"],
    "additionalProperties": False,
}


def _build_messages(
    *, title: str, language: str, profile: dict, domain: str | None,
    target_minutes_total: int | None, hits: list,
) -> list[dict]:
    """Pure function: the {system,user} messages for curriculum generation —
    kept separate from `generate_curriculum` (which also calls the live model)
    so the prompt shape is unit-testable without a model or a DB, mirroring
    `app.brain.retrieve.build_grounded_messages`.

    Tool-first/imperative and short, per vllm_interract's agentic-gotchas #1:
    a prose-heavy system prompt makes the model write the structured thing as
    prose instead of complying with structure. Guided decoding already
    constrains the JSON *shape* regardless of prompt wording, but a rambling
    prompt still degrades *content* quality inside that shape (generic filler,
    ignoring CONTEXT, un-grounded gear/technique claims) — hence also an
    explicit "don't invent facts" instruction, matching retrieve.py's grounded
    system prompt.
    """
    system = (
        "You generate ONLY the JSON curriculum tree matching the given schema "
        "— no prose, no markdown, no commentary outside the JSON object. "
        f"Write every title/objective/body in {language} (el=Greek, en=English). "
        "Ground every module, lesson, and segment in the numbered CONTEXT "
        "below: do not invent gear, techniques, or facts the CONTEXT does not "
        "support. Give every lesson and segment a realistic, non-zero "
        "est_minutes (minutes of actual practice/teaching time). Keep the tree "
        "compact regardless of the target total: about 2-6 modules, 1-5 "
        "lessons per module, 1-4 segments per lesson — express a long total "
        "via each node's est_minutes, not via many small nodes."
    )
    context = "\n\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
    minutes_line = (
        f"Target total course length: about {target_minutes_total} minutes.\n"
        if target_minutes_total else ""
    )
    user = (
        f"Course title: {title}\n"
        f"Student profile: {profile}\n"
        f"Domain: {domain}\n"
        f"{minutes_line}"
        f"\nCONTEXT:\n{context}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _positive_minutes(value) -> int:
    """Clamp a model-supplied est_minutes to >=1 — defense in depth.

    CURRICULUM_SCHEMA already declares `"minimum": 1` on both est_minutes
    fields, and this was live-verified to actually be enforced by vLLM's
    guided decoding, not just accepted-and-ignored (see this task's report).
    This clamp is a second, independent guarantee of the same invariant:
    callers (pacing/segmentation math) rely on est_minutes > 0, so that must
    hold even if a future prompt/schema change, a different provider, or a
    provider bug ever lets a non-positive value through.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


def _persist_tree(db, tree: dict, *, title: str, language: str, profile: dict) -> Block:
    """Recursively persist an already schema-valid `tree` as a Block hierarchy:
    course -> module -> lesson -> segment, `order` = sibling index.

    `Block.kind` is a fixed literal per tree depth (course/module/lesson/
    segment), same as every other level — a segment's own LLM-authored `kind`
    (e.g. "warmup"/"drill"/"explanation") shapes the model's content but isn't
    given its own column in this PoC (Block has none); it guides generation,
    it isn't persisted separately. `db.flush()` after each parent is required
    (not optional) so its `id` — a Python-side `default=uuid.uuid4`, resolved
    at flush, not at construction — is available for the next level's
    `parent_id` before the final commit.
    """
    course = Block(
        kind="course", title=title, order=0, language=language,
        is_template=True, target_profile=profile,
    )
    db.add(course)
    db.flush()

    for m_i, module in enumerate(tree.get("modules") or []):
        module_block = Block(
            kind="module", title=module["title"], body=module.get("objective"),
            order=m_i, parent_id=course.id, language=language,
        )
        db.add(module_block)
        db.flush()

        for l_i, lesson in enumerate(module.get("lessons") or []):
            objectives = lesson.get("objectives") or []
            lesson_block = Block(
                kind="lesson", title=lesson["title"],
                body="\n".join(objectives) if objectives else None,
                est_minutes=_positive_minutes(lesson.get("est_minutes")),
                order=l_i, parent_id=module_block.id, language=language,
            )
            db.add(lesson_block)
            db.flush()

            for s_i, segment in enumerate(lesson.get("segments") or []):
                db.add(Block(
                    kind="segment", title=segment["title"], body=segment.get("body"),
                    est_minutes=_positive_minutes(segment.get("est_minutes")),
                    order=s_i, parent_id=lesson_block.id, language=language,
                ))

    return course


def generate_curriculum(
    db, *, title: str, language: str, profile: dict,
    domain: str | None = None, target_minutes_total: int | None = None,
) -> uuid.UUID:
    """Retrieve Brain context, guided-JSON a curriculum tree grounded in it,
    persist it as a `Block` hierarchy. Returns the root (course) Block id.

    `db` is a caller-owned SQLAlchemy Session (mirrors `ingest_source`/
    `search`): not closed here, but committed here — the persisted tree is
    this function's entire observable output, so the commit is part of its
    contract, not left to the caller.
    """
    query = f"{title} {profile} {domain}"
    hits = search(db, query, k=12, domain=domain)

    messages = _build_messages(
        title=title, language=language, profile=profile, domain=domain,
        target_minutes_total=target_minutes_total, hits=hits,
    )
    tree = get_provider().guided_json(messages, CURRICULUM_SCHEMA)

    course = _persist_tree(db, tree, title=title, language=language, profile=profile)
    db.commit()
    return course.id
