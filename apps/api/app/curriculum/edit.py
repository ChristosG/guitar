"""Editing the OUTLINE — add / delete / reorder a module or a lesson — before the
expensive draft runs.

This is the engagement the tutor said was missing. He was handed a curriculum and
asked to accept or reject it; what he wanted was to work on it. The whole point of
materializing the tree at CONFIRM is that from then on it is a normal tree, and
editing it is normal CRUD.

SIBLING ORDER IS RENORMALISED ON EVERY STRUCTURAL CHANGE, WHICH NOTHING IN THIS
CODEBASE DID BEFORE. `DELETE /blocks/{id}` has always left a hole ([0, 1, 3]) and
`order` collisions have always been possible on insert. It never showed, because
the board sorted by `order` and a hole sorts fine. It shows the moment you can
INSERT: a new module appended at `len(siblings)` collides with an existing sibling
that a previous delete left at that index, and the tie is broken by whatever
Postgres feels like — so the tutor's new module lands in the middle of his course.
`_renormalise` is lifted straight from `app.lessons.edit`, which solved this for
sessions; there was no reason to invent a second answer.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.block import Block


class EditError(ValueError):
    """A structural edit the tree cannot accept (wrong kind, wrong parent, not
    found). The router turns it into a 404/422 — it is never a 500."""


def _get(db, block_id: uuid.UUID, *, kind: str | None = None) -> Block:
    block = db.get(Block, block_id)
    if block is None:
        raise EditError(f"block not found: {block_id}")
    if kind is not None and block.kind != kind:
        raise EditError(f"block {block_id} is a {block.kind!r}, not a {kind!r}")
    return block


def _renormalise(db, parent_id: uuid.UUID) -> None:
    """Reset `order` to a contiguous 0..n-1 run, preserving the current relative
    order. Identical to `app.lessons.edit._renormalise_order`."""
    siblings = db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ).all()
    for i, sib in enumerate(siblings):
        sib.order = i


def add_module(db, root_id: uuid.UUID, *, title: str, objective: str = "",
               tier: str = "general_knowledge", after: uuid.UUID | None = None) -> Block:
    """A new, empty module under a course. Appended, or inserted after `after`."""
    course = _get(db, root_id, kind="course")
    siblings = db.scalars(
        select(Block)
        .where(Block.parent_id == course.id, Block.kind == "module")
        .order_by(Block.order)
    ).all()

    at = len(siblings)
    if after is not None:
        target = _get(db, after, kind="module")
        if target.parent_id != course.id:
            raise EditError(f"module {after} is not part of this curriculum")
        at = next(i for i, s in enumerate(siblings) if s.id == target.id) + 1

    module = Block(
        kind="module", title=title, body=objective or None, order=at,
        parent_id=course.id, language=course.language,
        meta={"tier": tier, "objective": objective, "coverage_note": "", "added_by_tutor": True},
    )
    siblings.insert(at, module)
    db.add(module)
    db.flush()
    for i, sib in enumerate(siblings):
        sib.order = i
    db.commit()
    db.refresh(module)
    return module


def add_lesson(db, module_id: uuid.UUID, *, title: str, objective: str = "",
               after: uuid.UUID | None = None) -> Block:
    """A new lesson under a module, `queued` — so the very next Resume drafts it.

    That is the whole reason this is worth having: the tutor reads module 3, sees a
    lesson missing, adds it, presses Resume, and it gets written with the same
    cached library prefix as the other nineteen. `draft_status="queued"` is not
    bookkeeping, it is the enqueue.
    """
    module = _get(db, module_id, kind="module")
    siblings = db.scalars(
        select(Block)
        .where(Block.parent_id == module.id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()

    at = len(siblings)
    if after is not None:
        target = _get(db, after, kind="lesson")
        if target.parent_id != module.id:
            raise EditError(f"lesson {after} is not part of this module")
        at = next(i for i, s in enumerate(siblings) if s.id == target.id) + 1

    course = db.get(Block, module.parent_id)
    minutes = ((course.meta or {}).get("shape") or {}).get("minutes_per_lesson") if course else None

    lesson = Block(
        kind="lesson", title=title, body=objective or None, order=at,
        parent_id=module.id, language=module.language, est_minutes=minutes,
        meta={"draft_status": "queued", "objective": objective, "added_by_tutor": True},
    )
    siblings.insert(at, lesson)
    db.add(lesson)
    db.flush()
    for i, sib in enumerate(siblings):
        sib.order = i
    db.commit()
    db.refresh(lesson)
    return lesson


def reorder_block(db, block_id: uuid.UUID, *, direction: str) -> Block:
    """Move a block one position up or down among its siblings.

    Up/down buttons, not drag-and-drop: two buttons and a swap, versus a drag
    library, touch targets, and an autoscroll. The tutor is reordering five
    modules, not a thousand rows.
    """
    if direction not in ("up", "down"):
        raise EditError(f"direction must be 'up' or 'down', got {direction!r}")
    block = _get(db, block_id)
    if block.parent_id is None:
        raise EditError("a root block has no siblings to move among")

    siblings = db.scalars(
        select(Block)
        .where(Block.parent_id == block.parent_id, Block.kind == block.kind)
        .order_by(Block.order)
    ).all()
    i = next((n for n, s in enumerate(siblings) if s.id == block.id), None)
    if i is None:
        raise EditError("block is not among its own siblings")

    j = i - 1 if direction == "up" else i + 1
    if not (0 <= j < len(siblings)):
        return block  # already at the end. Not an error — the button is just a no-op.

    siblings[i], siblings[j] = siblings[j], siblings[i]
    for n, sib in enumerate(siblings):
        sib.order = n
    db.commit()
    db.refresh(block)
    return block


def requeue_lesson(db, lesson_id: uuid.UUID, *, deepen: bool = False) -> uuid.UUID:
    """DEEPEN / REDRAFT one lesson: put it back to `queued` and return its course id.

    There is no separate deepen pipeline, and there should not be one. A lesson the
    tutor thinks is thin is, structurally, a lesson that has not been drafted yet —
    so it goes back to `queued`, the same state a brand-new lesson is born in, and
    the SAME fan-out (`jobs/curriculum_draft.py`) picks it up against the SAME
    cached library prefix. `deepen=True` only tells that fan-out to raise this one
    lesson's word target (`DEEPEN_TARGET_RATIO`); everything else about the path is
    the path every other lesson already takes.

    The flag is consumed at claim time, not here — a lesson that is claimed, drafted
    and persisted must not stay marked `deepen` forever, or the next unrelated
    Resume would silently redraft it long.
    """
    lesson = _get(db, lesson_id, kind="lesson")
    module = _get(db, lesson.parent_id, kind="module")
    if module.parent_id is None:
        raise EditError("this lesson's module is not attached to a curriculum")

    lesson.meta = {
        **(lesson.meta or {}),
        "draft_status": "queued",
        "deepen": bool(deepen),
        "error": None,
    }
    db.commit()
    return module.parent_id


def delete_block(db, block_id: uuid.UUID) -> uuid.UUID | None:
    """Delete a block and renormalise what is left. Returns the parent id.

    The ORM cascade (`all, delete-orphan`) plus the DB's `ON DELETE CASCADE` take
    the subtree. What neither of them does is close the hole in `order` — which is
    the bug that only becomes visible once you can also INSERT.
    """
    block = _get(db, block_id)
    parent_id = block.parent_id
    db.delete(block)
    db.flush()
    if parent_id is not None:
        _renormalise(db, parent_id)
    db.commit()
    return parent_id
