"""Per-student curriculum assignment: deep-clone a template's content-plane
Block subtree into a fresh, student-owned instance.

Extracted (Plan 5 Task 3 review) into its own framework-free module so the
single copy of this non-trivial mutation logic (content-plane-only filter,
`is_template=False` stamping, fresh-id flush-then-reparent, defensive
`target_profile` dict-copy) is shared by BOTH callers instead of duplicated:
`routers/curriculum.py`'s `assign_curriculum` endpoint AND `agent/tools.py`'s
`assign_curriculum` tool fn. Two copies would drift — a new `Block` field
added to one clone site, or a plane-filter change made in one, would silently
diverge the chat copilot's assignment from the HTTP endpoint's, with no test
to catch it.

Pure `(db, Block) -> Block` logic with zero FastAPI/HTTP coupling (no
`HTTPException`, no `Depends`) — it lives under `app/curriculum/` alongside
`segment.py`/`generate.py`, the other framework-free curriculum services,
NOT in a router. Caller-owned session convention, same as those siblings:
this flushes (needed for the recursive reparent) but does NOT commit — the
caller owns the transaction boundary (both current callers commit after,
together with their own `Assignment` audit row).
"""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.block import Block


def clone_content_subtree(
    db: Session,
    node: Block,
    *,
    parent_id: UUID | None,
    student_id: UUID | None,
    is_template: bool = False,
    id_map: dict[UUID, UUID] | None = None,
) -> Block:
    """Recursively deep-clone `node`'s CONTENT-plane subtree only.

    TWO CALLERS WITH OPPOSITE STAMPS, ONE WALK. Assignment produces a
    student-owned INSTANCE (`is_template=False`, a real `student_id`);
    `curriculum/duplicate.py` produces another TEMPLATE (`is_template=True`,
    `student_id=None`). Everything else — the plane filter, the ordering, the
    field-by-field copy, the flush-then-reparent — is identical, and this
    module exists precisely because a second copy of it would drift (see the
    module docstring). So the stamp is a parameter and the walk is not
    duplicated. `is_template` defaults to False so the assignment call site
    reads exactly as it did.

    `id_map`, when passed, is filled as `{source_block_id: clone_block_id}` for
    every node this creates, in walk order. Duplicate needs it to re-point
    attached `Artifact` rows at the cloned blocks; assignment passes nothing
    and is unaffected. It is an out-parameter rather than a second return value
    so the recursion stays a plain `-> Block` and the caller keeps owning the
    dict it allocated.

    A template that has already been segmented (`POST /blocks/{id}/segment`
    with `student_id=None`) also carries a delivery-plane child
    (delivery_root + sessions) alongside its content children — that's a
    derived pacing plan, not curriculum content, and is deliberately NOT
    cloned here (the `Block.plane == "content"` filter on the child query
    below), mirroring `segment.py`'s own `_collect_leaves` plane=="content"
    filter and its documented rationale. The assigned student gets a clean
    content copy; segmenting *that* copy for them is a separate, later
    `POST /blocks/{new_root_id}/segment` call (optionally with their own
    `student_id`).

    Every cloned node gets a fresh id (`Block`'s own `default=uuid.uuid4`),
    `is_template=False`, and `student_id` set to the target student —
    regardless of what the source node had — per the brief ("new tree with
    is_template=False and student_id set on every node"). `order`/`kind`/
    `title`/`body`/`est_minutes`/`language`/`plane` are copied verbatim
    (structure preserved). `target_profile` is copied as an independent
    dict (not the same aliased object) — harmless either way for a JSON
    column since nothing mutates it post-clone, but cheap and avoids any
    accidental-aliasing footgun.

    `db.flush()` after `db.add(clone)` is required (not optional), same
    reason as `generate.py`'s `_persist_tree`: `clone.id` is a Python-side
    `default=uuid.uuid4`, resolved at flush not at construction, and is
    needed as the next level's `parent_id` before this function returns.
    """
    clone = Block(
        parent_id=parent_id,
        order=node.order,
        kind=node.kind,
        title=node.title,
        body=node.body,
        est_minutes=node.est_minutes,
        language=node.language,
        is_template=is_template,
        target_profile=dict(node.target_profile) if node.target_profile else None,
        student_id=student_id,
        plane=node.plane,
        # `meta` was the ONE field this clone dropped, and everything Stage 6
        # hangs on meta went with it: segment `section`/`citations` (the
        # provenance chips), module `tier`/`coverage_note` (the badges), lesson
        # `draft_status`/`word_count` — so an assigned copy of a fully-drafted
        # curriculum rendered bare AND read as 0-of-20 'queued' to
        # draft_progress, whose Resume button would then re-draft all twenty
        # cloned lessons at full price. Independent dict copy, same
        # anti-aliasing instinct as `target_profile` above.
        meta=dict(node.meta) if node.meta else None,
    )
    db.add(clone)
    db.flush()
    if id_map is not None:
        id_map[node.id] = clone.id

    children = db.scalars(
        select(Block)
        .where(Block.parent_id == node.id, Block.plane == "content")
        .order_by(Block.order)
    ).all()
    for child in children:
        clone_content_subtree(
            db, child, parent_id=clone.id, student_id=student_id,
            is_template=is_template, id_map=id_map,
        )

    return clone
