"""Brain-grounded curriculum generation: a title/profile/domain -> a guided-JSON
tree shape from the LLM -> persisted as a `Block` hierarchy
(course -> module -> lesson -> segment).

Plan 12 Task 2 (Chris, verbatim, on the deployed app): "when i click
'Generate a curriculum' seems like it just uses the llm general knowledge,
but i think it would be beneficial here to select things from our library".
He was right: the old single-phase version ran exactly ONE guided-JSON call,
seeded with a single whole-course library search, and never touched the
library again while drafting every module's actual content — the tree's
titles/objectives/lesson bodies were free to be pure LLM-general-knowledge
prose that merely happened to be schema-shaped.

TWO-PHASE now (G1):
  Phase 1 — PLAN: one guided-JSON call producing the module OUTLINE only
    (titles + one-line objectives, no lesson content yet).
  Phase 2 — GROUND + DRAFT: for EACH planned module, `app.curriculum.ground.
    ground_topic` retrieves that module's own passages from the library
    (optionally scoped to `source_ids`, the sources the tutor chose), and a
    SEPARATE guided-JSON call drafts that module's lessons/segments FROM
    those passages, with an explicit "invent nothing beyond CONTEXT"
    instruction (mirrors `app.lessons.draft`'s anti-invention posture).

This makes generation N+1 calls (1 plan + 1 per module) instead of 1 — see
this task's report for the measured real-model wall-clock; the endpoint was
already async (`GenerationJob`/`BackgroundTasks`, Plan 8 Task 2/3) precisely
because a single guided-JSON call already ran 49-179s, so a slower multi-call
path still fits the same "poll a job" UX, it just polls longer.

GAPS (G3) — never silently filled. A module whose retrieval returns nothing
above `app.curriculum.ground`'s relevance floor is marked
`target_profile["gap"] = True` and gets an honest body saying his library
doesn't cover it — NO lesson content is invented for it, unless the caller
passes `allow_general=True`, in which case it IS filled from the model's
general knowledge but its body is explicitly labelled as not coming from his
material. Same principle Plan 11 used for chat: general knowledge isn't the
danger, UNLABELLED general knowledge is.

PROVENANCE (mirrors Plan 10's `app.lessons.draft` B3 exactly — no migration):
a grounded module records `target_profile["provenance"] = {"passages":
[{"source_id", "source_title", "page_no"}, ...]}` on its own Block row.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.curriculum.ground import Passage, ground_topic
from app.llm.factory import get_provider
from app.models.block import Block

# Review fix (PERFORMANCE): a 5-module curriculum was measured at 267.8s
# (~4m28s) — 6 sequential guided_json calls (~45s each), one plan + one per
# module. Module drafts (Phase 2 below) don't depend on each other at all,
# so they run through a bounded thread pool instead of one at a time.
# Bounded (not "one thread per module"): the GPU serving both the LLM and
# the embed server is shared with Chris's other apps — an unbounded fan-out
# would stampede it. 4 is a deliberately modest starting bound, not a
# measured ceiling.
_MAX_CONCURRENT_MODULE_DRAFTS = 4

# Phase 1 output shape: the module OUTLINE only (titles + objectives) — no
# lesson/segment content yet, that's Phase 2, drafted per-module FROM its own
# retrieved passages. Deliberately no array "minItems" (see the old
# single-phase schema's identical note, kept below on PLAN_SCHEMA) —
# unverified keyword, not worth risking on a schema still being iterated on.
PLAN_SCHEMA = {
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
                },
                "required": ["title", "objective"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "modules"],
    "additionalProperties": False,
}

# Phase 2 output shape: one module's lessons/segments. Identical lesson/
# segment item shape the old single-phase CURRICULUM_SCHEMA used — kept
# verbatim, including its verified `"minimum": 1` on `est_minutes` (live
# A/B-verified — see this schema's own history in git blame / the original
# task's report — that vLLM's guided decoding actually enforces integer
# "minimum", not just accepts-and-ignores it).
MODULE_SCHEMA = {
    "type": "object",
    "properties": {
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
    "required": ["lessons"],
    "additionalProperties": False,
}

# Body text for a module the library has nothing on and `allow_general=False`
# left unfilled — plain, unambiguous, no invented content anywhere near it.
_GAP_BODY = (
    "Your library doesn't cover this topic — no lesson content was "
    "generated from your material. Add a source on this topic, or "
    "regenerate with general knowledge allowed."
)

# Prefix stamped onto a gap module's body when `allow_general=True` fills it
# anyway — the label lives in the body text ITSELF (not only in
# `target_profile`), deterministically, rather than trusting the model to
# say this about its own output.
_GENERAL_KNOWLEDGE_PREFIX = "[General knowledge — not from your library] "


def _build_plan_messages(
    *, title: str, language: str, profile: dict, domain: str | None,
    target_minutes_total: int | None,
) -> list[dict]:
    """Pure function: the {system,user} messages for Phase 1 (the module
    OUTLINE, titles + objectives only — no CONTEXT, nothing to ground yet).
    Kept separate from `generate_curriculum` so the prompt shape is
    unit-testable without a model or a DB, mirroring every other
    `_build_*_messages` in this codebase (`app.lessons.draft`, `app.brain.
    retrieve`, `app.artifacts.generate`).
    """
    system = (
        "You generate ONLY the JSON module outline matching the given "
        "schema — no prose, no markdown, no commentary outside the JSON "
        f"object. Write every title/objective in {language} (el=Greek, "
        "en=English). Produce ONLY titles and one-sentence objectives for "
        "each module — do NOT draft lesson or segment content yet, that "
        "happens separately per module. Keep the outline compact: about "
        "2-6 modules covering the course title's topic end to end."
    )
    minutes_line = (
        f"Target total course length: about {target_minutes_total} minutes.\n"
        if target_minutes_total else ""
    )
    user = (
        f"Course title: {title}\n"
        f"Student profile: {profile}\n"
        f"Domain: {domain}\n"
        f"{minutes_line}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_module_draft_messages(
    *, module_title: str, objective: str, language: str, passages: list[Passage],
) -> list[dict]:
    """Pure function: the {system,user} messages for Phase 2's GROUNDED
    draft of one module — the retrieved `passages`' text is embedded
    VERBATIM in the user message (not summarized/paraphrased here) so both
    the model and this module's own test
    (`test_the_retrieved_passage_text_reaches_the_module_draft_prompt`) can
    verify it was actually given the real retrieved text, not a description
    of it — same reasoning as `app.lessons.draft._build_messages`.
    """
    system = (
        "You generate ONLY the JSON module content (lessons/segments) "
        "matching the given schema — no prose, no markdown, no commentary "
        f"outside the JSON object. Write every title/body in {language} "
        "(el=Greek, en=English). Ground the ENTIRE module in the numbered "
        "CONTEXT below: every lesson and segment must teach something the "
        "CONTEXT actually supports. Do NOT invent gear, techniques, "
        "citations, or facts the CONTEXT does not contain. Give every "
        "lesson and segment a realistic, non-zero est_minutes. Produce 1-5 "
        "lessons with 1-4 segments each."
    )
    context = "\n\n".join(f"[{i}] {p.text}" for i, p in enumerate(passages, start=1))
    user = (
        f"Module title: {module_title}\n"
        f"Module objective: {objective}\n"
        f"\nCONTEXT:\n{context}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _build_general_module_messages(
    *, module_title: str, objective: str, language: str,
) -> list[dict]:
    """Pure function: the {system,user} messages for Phase 2's UNGROUNDED
    (general-knowledge) fill of a gap module — only reached when the caller
    passed `allow_general=True` for a module retrieval found nothing above
    the relevance floor for. No CONTEXT block: there is nothing retrieved to
    ground in. The resulting content is still labelled as general knowledge
    at persist time (`_GENERAL_KNOWLEDGE_PREFIX`, applied deterministically
    in `generate_curriculum` below) rather than trusting the model to say so
    about its own output inside the guided-JSON shape.
    """
    system = (
        "You generate ONLY the JSON module content (lessons/segments) "
        "matching the given schema — no prose, no markdown, no commentary "
        f"outside the JSON object. Write every title/body in {language} "
        "(el=Greek, en=English). The tutor's own library has no material on "
        "this topic, so draw on general guitar-teaching knowledge instead. "
        "Give every lesson and segment a realistic, non-zero est_minutes. "
        "Produce 1-5 lessons with 1-4 segments each."
    )
    user = f"Module title: {module_title}\nModule objective: {objective}\n"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _positive_minutes(value) -> int:
    """Clamp a model-supplied est_minutes to >=1 — defense in depth.

    PLAN_SCHEMA/MODULE_SCHEMA already declare `"minimum": 1` on both
    est_minutes fields, and this was live-verified to actually be enforced
    by vLLM's guided decoding, not just accepted-and-ignored (see this
    schema's own history for the A/B verification). This clamp is a second,
    independent guarantee of the same invariant: callers (pacing/
    segmentation math) rely on est_minutes > 0, so that must hold even if a
    future prompt/schema change, a different provider, or a provider bug
    ever lets a non-positive value through.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return n if n > 0 else 1


def _persist_module_lessons(db, module_block: Block, lessons: list[dict], *, language: str) -> None:
    """Persist ONE module's already schema-valid `lessons` list as
    `lesson -> segment` Block children of `module_block`, `order` = sibling
    index. Extracted from the old single-phase `_persist_tree` (still the
    same shape/flush discipline) since Phase 2 now drafts and persists one
    module at a time rather than one giant combined tree.
    """
    for l_i, lesson in enumerate(lessons):
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


def generate_curriculum(
    db, *, title: str, language: str, profile: dict,
    domain: str | None = None, target_minutes_total: int | None = None,
    source_ids: list[uuid.UUID] | None = None, allow_general: bool = False,
) -> uuid.UUID:
    """Retrieval-augmented curriculum generation (G1/G3), FOUR phases (review
    fix split Phase 2 in two — grounding and drafting used to be a single
    per-module step, interleaved with this Session's own writes; see the
    PERFORMANCE note on the SAFE SHAPE below):

      Phase 1 (sequential) — PLAN the module outline (titles + objectives),
        one guided-JSON call, no library access yet.
      Phase 2 (sequential, cheap DB reads) — for EACH planned module, ground
        it in the tutor's own library (`app.curriculum.ground.ground_topic`,
        optionally scoped to `source_ids`/`domain`). Kept on this one
        Session, one module at a time — `ground_topic` reads via `db`, and
        SQLAlchemy Sessions are not thread-safe.
      Phase 3 (PARALLEL, no DB) — draft each module's lessons/segments FROM
        its own already-retrieved passages, one guided-JSON call per module.
        Pure LLM I/O, no Session access at all, so this phase runs through a
        bounded `ThreadPoolExecutor` (`_MAX_CONCURRENT_MODULE_DRAFTS`) —
        SAFE SHAPE, the reviewer's explicit caution: naively parallelizing
        the OLD single per-module loop would have shared this Session across
        concurrent tasks (it read AND wrote through it in the same
        iteration as the LLM call) — SQLAlchemy does not support that and it
        would corrupt or crash. Splitting grounding (Session-owning) from
        drafting (Session-free) is what makes parallelizing safe. A module
        with no passages above the relevance floor is a GAP: left unfilled
        (honest body, `target_profile["gap"] = True`, no draft call at all)
        unless `allow_general=True`, in which case it's filled from general
        knowledge but its body is labelled as such.
      Phase 4 (sequential) — persist every module's tree onto this one
        Session, in planned order, from the Phase 3 results collected above.

    Persists the result as a `Block` hierarchy (course -> module -> lesson ->
    segment) — same tree shape the board UI already renders; only NEW
    information (provenance/gap) lives in `target_profile`, an existing JSON
    column, so no migration. Returns the root (course) Block id.

    `db` is a caller-owned SQLAlchemy Session (mirrors the old single-phase
    version/`app.lessons.draft.draft_lesson_from_selection`): not closed
    here, but committed here — the persisted tree is this function's entire
    observable output, so the commit is part of its contract, not left to
    the caller.

    `source_ids`, when supplied via `GenerationJob.params` (a JSON column),
    round-trips as plain strings, not `uuid.UUID` objects — normalized here,
    once, rather than on every module's `ground_topic` call below.
    """
    if source_ids is not None:
        source_ids = [s if isinstance(s, uuid.UUID) else uuid.UUID(str(s)) for s in source_ids]

    plan_messages = _build_plan_messages(
        title=title, language=language, profile=profile, domain=domain,
        target_minutes_total=target_minutes_total,
    )
    plan = get_provider().guided_json(plan_messages, PLAN_SCHEMA)

    course = Block(
        kind="course", title=title, order=0, language=language,
        is_template=True, target_profile=profile,
    )
    db.add(course)
    db.flush()

    modules_plan = plan.get("modules") or []

    # Phase 2 (sequential, cheap): ground every module's topic against the
    # tutor's library — DB reads only, kept on this one shared Session.
    grounded_modules = []
    for module in modules_plan:
        module_title = module["title"]
        objective = module.get("objective") or ""
        query = f"{module_title} {objective}"
        passages = ground_topic(db, query, source_ids=source_ids, k=5, domain=domain)
        grounded_modules.append(
            {"title": module_title, "objective": objective, "passages": passages}
        )

    # Phase 3 (PARALLEL, no DB): one guided_json draft call per module. See
    # this function's own docstring for why this is safe only BECAUSE Phase
    # 2's Session-owning work already happened above, sequentially.
    def _draft_one(entry: dict) -> dict | None:
        passages = entry["passages"]
        if passages:
            draft_messages = _build_module_draft_messages(
                module_title=entry["title"], objective=entry["objective"],
                language=language, passages=passages,
            )
            return get_provider().guided_json(draft_messages, MODULE_SCHEMA)
        elif allow_general:
            draft_messages = _build_general_module_messages(
                module_title=entry["title"], objective=entry["objective"], language=language,
            )
            return get_provider().guided_json(draft_messages, MODULE_SCHEMA)
        else:
            return None  # unfilled gap — no draft call needed

    drafts: list[dict | None] = [None] * len(grounded_modules)
    if grounded_modules:
        workers = min(_MAX_CONCURRENT_MODULE_DRAFTS, len(grounded_modules))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_draft_one, entry): i for i, entry in enumerate(grounded_modules)
            }
            for future, i in futures.items():
                drafts[i] = future.result()  # re-raises here if _draft_one raised

    # Phase 4 (sequential): persist every module's tree from the Phase 2/3
    # results collected above, in the planned order.
    for m_i, (entry, draft) in enumerate(zip(grounded_modules, drafts)):
        passages: list[Passage] = entry["passages"]
        objective = entry["objective"]

        if passages:
            lessons = draft.get("lessons") or []
            body = objective
            target_profile = {
                "provenance": {
                    "passages": [
                        {
                            "source_id": str(p.source_id),
                            "source_title": p.source_title,
                            "page_no": p.page_no,
                        }
                        for p in passages
                    ],
                },
            }
        elif allow_general:
            lessons = draft.get("lessons") or []
            body = _GENERAL_KNOWLEDGE_PREFIX + objective
            target_profile = {"gap": True, "general_knowledge": True}
        else:
            lessons = []
            body = _GAP_BODY
            target_profile = {"gap": True}

        module_block = Block(
            kind="module", title=entry["title"], body=body,
            order=m_i, parent_id=course.id, language=language,
            target_profile=target_profile,
        )
        db.add(module_block)
        db.flush()

        _persist_module_lessons(db, module_block, lessons, language=language)

    db.commit()
    return course.id
