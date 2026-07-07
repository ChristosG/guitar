"""Segmentation: deterministically partition a content Block tree
(course -> module -> lesson -> segment, each carrying est_minutes) into
teachable "session" Blocks on the delivery plane.

Controller decision (deviates from this task's original brief): guided_json
generation is slow (49-179s/call - see Plan 3 Task 2's report) - too slow to
call once per session, and unnecessary anyway, since "how many minutes of
already-estimated content fit into a session" is arithmetic, not something an
LLM needs to decide. `partition_by_minutes` is therefore a pure, deterministic
greedy bin-packer (fast, exact, unit-tested without a model or a DB);
`segment_block` walks the real content tree, partitions it, and persists the
result. Session titles/bodies are deterministic (derived from the covered
leaves' own module/title text), not LLM-generated - good enough for this PoC,
and avoids a slow, bounded LLM call entirely.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select

from app.models.block import Block

# Hard ceiling on a session's packed total, as a multiple of session_minutes:
# never let the greedy pack pull in a leaf that would push the running total
# past this - see partition_by_minutes.
_CAP_FACTOR = 1.2
# Soft floor, as a multiple of session_minutes: below this, the module-
# boundary preference is ignored so a session isn't left needlessly tiny.
_FLOOR_FACTOR = 0.5

# Block.title is VARCHAR(300) at the DB level (enforced by Postgres, not just
# a suggestion) - a session title built from joined module names could
# exceed it, so it is truncated defensively before insert.
_TITLE_MAX_LEN = 300


@dataclass
class Leaf:
    """One partitionable unit of content: a lesson (no segment children) or
    a segment (always childless in the generated tree) - i.e. any content
    Block with no children of its own and a positive est_minutes.
    `module_title` is its nearest module-kind ancestor's title, or None.
    """
    block_id: uuid.UUID
    minutes: int
    title: str
    module_title: str | None


def partition_by_minutes(leaves: list[Leaf], session_minutes: int) -> list[list[Leaf]]:
    """Greedily pack consecutive leaves (already in document order) into
    sessions targeting `session_minutes` each. Pure function: no DB, no I/O.

    Two closing rules, checked for every leaf after a session's first:

    1. **Hard cap** - never let a session's running total exceed
       ``session_minutes * 1.2`` by adding the next leaf. A leaf is never
       split: this rule only ever closes-and-reopens a session, so a single
       leaf whose own `minutes` already exceeds the cap always ends up alone
       in its own session (nothing more can be added to it, and it can't be
       added to a non-empty session either).
    2. **Soft module-boundary preference** - once a session's running total
       has reached the ``session_minutes * 0.5`` floor, prefer to close it
       at a module boundary rather than pull in a leaf from the next
       module, even if the cap would still allow it. Below the floor,
       packing continues across the boundary instead, so a module ending
       early doesn't strand a needlessly tiny session.

    A trailing session (the last one overall, or one immediately followed by
    a single oversized leaf) can legitimately land under the floor - there's
    no leaf left to pack it up to size, and leaves are never reordered to
    compensate.
    """
    if session_minutes <= 0:
        raise ValueError("session_minutes must be positive")
    if not leaves:
        return []

    cap = session_minutes * _CAP_FACTOR
    floor = session_minutes * _FLOOR_FACTOR

    sessions: list[list[Leaf]] = []
    current: list[Leaf] = []
    current_minutes = 0
    current_module: str | None = None

    for leaf in leaves:
        if current:
            crosses_module = leaf.module_title != current_module
            would_exceed_cap = current_minutes + leaf.minutes > cap
            prefer_new_session = crosses_module and current_minutes >= floor
            if would_exceed_cap or prefer_new_session:
                sessions.append(current)
                current, current_minutes = [], 0

        current.append(leaf)
        current_minutes += leaf.minutes
        current_module = leaf.module_title

    sessions.append(current)
    return sessions


def _collect_leaves(db, root: Block) -> list[Leaf]:
    """Depth-first, document-order list of content Leaves under `root`.

    Fetches the whole content-plane subtree level-by-level (a handful of
    queries regardless of node count, since course->module->lesson->segment
    is only 4 deep) into an in-memory parent_id -> [children] map, then walks
    that map recursively to emit leaves in genuine depth-first order.

    The level-batched fetch alone would NOT get emission order right: if it
    emitted a leaf the moment a level's batch found one (instead of building
    the full map first and walking it afterward), sibling subtrees of uneven
    depth - e.g. one lesson with no segments next to a sibling lesson that
    has some - would surface in level order, not document order. Building
    the map first and walking it in memory afterward avoids that.

    ``Block.plane == "content"`` is an explicit filter (not just "whatever's
    under root"): after a first `segment_block` call, `root` also has a
    delivery-plane child (the delivery_root and its sessions) alongside its
    original content children. Re-segmenting must never treat the *previous*
    delivery output as new content leaves - that would make re-segmentation
    silently wrong (and, since sessions are themselves childless Blocks with
    est_minutes set, they would otherwise look exactly like valid leaves).
    """
    children_by_parent: dict[uuid.UUID, list[Block]] = {}
    frontier_ids = [root.id]
    while frontier_ids:
        kids = db.scalars(
            select(Block)
            .where(Block.parent_id.in_(frontier_ids), Block.plane == "content")
            .order_by(Block.order)
        ).all()
        for kid in kids:
            children_by_parent.setdefault(kid.parent_id, []).append(kid)
        frontier_ids = [k.id for k in kids]

    leaves: list[Leaf] = []

    def walk(node: Block, module_title: str | None) -> None:
        kids = children_by_parent.get(node.id, [])
        if not kids:
            if node.est_minutes:
                leaves.append(Leaf(
                    block_id=node.id, minutes=node.est_minutes,
                    title=node.title, module_title=module_title,
                ))
            return
        for kid in kids:
            walk(kid, kid.title if kid.kind == "module" else module_title)

    walk(root, root.title if root.kind == "module" else None)
    return leaves


def _session_label(partition: list[Leaf]) -> str:
    """Deterministic, human-readable label for a session's title: the
    module(s) its leaves are drawn from, or - if there's no module context
    at all - its first leaf's own title.
    """
    modules = list(dict.fromkeys(leaf.module_title for leaf in partition if leaf.module_title))
    if len(modules) == 1:
        return modules[0]
    if modules:
        return " / ".join(modules)
    return partition[0].title


def _get_or_create_delivery_root(
    db, content_block_id: uuid.UUID, student_id: uuid.UUID | None, language: str, course_title: str,
) -> Block:
    """Find-or-create the single delivery_root for this (content block,
    student) combo - reused across re-segmentation calls (only its session
    children are replaced each time), so a course ends up with one
    delivery_root per student it has been segmented for, plus one with
    student_id=None for an unassigned/template pacing plan.
    """
    existing = db.scalars(
        select(Block).where(
            Block.parent_id == content_block_id,
            Block.kind == "delivery_root",
            Block.plane == "delivery",
            Block.student_id == student_id,
        )
    ).first()
    if existing is not None:
        return existing

    delivery_root = Block(
        kind="delivery_root", plane="delivery", parent_id=content_block_id, order=0,
        title=f"{course_title} — Delivery Plan"[:_TITLE_MAX_LEN],
        language=language, student_id=student_id,
    )
    db.add(delivery_root)
    db.flush()
    return delivery_root


def segment_block(
    db, block_id: uuid.UUID, *, session_minutes: int, cadence_per_week: int = 1,
    student_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Partition the content subtree under `block_id` into delivery-plane
    "session" Blocks of ~`session_minutes` each, and persist them as ordered
    children of a delivery_root Block. Returns the session ids, in order.

    Idempotent per (block_id, student_id): re-segmenting deletes every
    existing session under that combo's delivery_root before inserting the
    new partition, so calling this again (e.g. with a different
    session_minutes) replaces the prior plan rather than accumulating on top
    of it. `db` is caller-owned (mirrors generate_curriculum/ingest_source):
    not closed here, but committed here - the persisted sessions are this
    function's entire observable output, so the commit is part of its
    contract, not left to the caller.

    `cadence_per_week` is accepted (part of this function's interface per
    the brief) but not yet persisted anywhere - `Block` has no scheduling
    column to hang it on; see this task's report.
    """
    root = db.get(Block, block_id)
    if root is None:
        raise ValueError(f"block not found: {block_id}")

    leaves = _collect_leaves(db, root)
    partitions = partition_by_minutes(leaves, session_minutes)

    delivery_root = _get_or_create_delivery_root(db, block_id, student_id, root.language, root.title)
    db.execute(delete(Block).where(Block.parent_id == delivery_root.id))

    session_ids: list[uuid.UUID] = []
    for i, partition in enumerate(partitions):
        title = f"Session {i + 1}: {_session_label(partition)}"[:_TITLE_MAX_LEN]
        session = Block(
            kind="session", plane="delivery", parent_id=delivery_root.id, order=i,
            est_minutes=sum(leaf.minutes for leaf in partition),
            title=title,
            body="; ".join(leaf.title for leaf in partition),
            language=root.language, student_id=student_id,
        )
        db.add(session)
        db.flush()
        session_ids.append(session.id)

    db.commit()
    return session_ids
