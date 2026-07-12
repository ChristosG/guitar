"""Lesson editing: split / merge / add a `session` under a `lesson`
Block (Plan 10 Task 2 — "an agent that actually checks his lectures... and
does stuff for them, e.g. change/add something, or split/segment the
sessions").

BINDING DECISION B1 — NO NEW TABLE. Everything here operates on the existing
`Block` tree (`app.models.block`): a lesson is `Block(kind="lesson")`, its
sessions are `Block(kind="session")` children, and each session's items are
`Block(kind="item")` children. Rename/delete of any single Block already
exist (`PATCH`/`DELETE /blocks/{id}`, `app.routers.curriculum`) and are
deliberately NOT duplicated here.

BINDING DECISION B6 — SPLIT IS DETERMINISTIC. `split_session` reuses
`app.curriculum.segment.partition_by_minutes` (the same greedy, pure,
unit-tested bin-packer `segment_block` uses to cut a course into sessions in
the first place) to decide where the cuts fall. No LLM call: "how many
minutes of already-written items fit under a session" is arithmetic, not a
judgment call a model should be re-deciding, and re-cutting via a guided-JSON
call would risk both a slow round-trip and a hallucinated duration for
content that already has one.

THE INVARIANT THAT MATTERS MOST (split/merge alike): every item that went in
must come out, in the same order — this is the tutor's work. Concretely:

- `split_session` RE-PARENTS the existing item Blocks (via `Block.parent_id`
  assignment + `flush()`) onto newly-created session Blocks BEFORE deleting
  the original session. `Block.children` is declared with
  `cascade="all, delete-orphan"` (`app.models.block`) — deleting a Block
  cascades onto whatever is STILL parented to it at that moment. Deleting the
  old session first (or re-parenting only in Python objects without a
  `flush()` before the delete) would let the ORM's unit-of-work see the items
  as still-children-of-the-doomed-session and delete-orphan them right along
  with it. Re-parent-then-flush-then-delete is the only safe order; this is
  exercised directly by `test_lesson_edit.py`'s "keeps every item" assertion,
  and by construction (every original item id is looked up again under its
  NEW parent, never re-created) rather than merely re-created with the same
  title.
- `merge_sessions` re-parents every item from every session but the first
  onto the first (survivor) session, keeping each source session's own item
  order and concatenating source sessions in the order given, THEN deletes
  the now-childless donor sessions — same re-parent-before-delete rule.

Both operations are transactional per their own docstring: on any exception
before the final `db.commit()`, `db.rollback()` undoes every `add`/`delete`/
re-parent so a partial split/merge can never leave the tree half-migrated.
"""
import uuid

from sqlalchemy import select

from app.curriculum.segment import Leaf, partition_by_minutes
from app.models.block import Block


def _get_block_or_raise(db, block_id: uuid.UUID, *, kind: str | None = None) -> Block:
    block = db.get(Block, block_id)
    if block is None:
        raise ValueError(f"block not found: {block_id}")
    if kind is not None and block.kind != kind:
        raise ValueError(f"block {block_id} is not a {kind!r} (got {block.kind!r})")
    return block


def _renormalise_order(db, parent_id: uuid.UUID) -> None:
    """Reset `order` to a contiguous 0..n-1 run for `parent_id`'s children,
    in their current relative order. Called after every structural change in
    this module so siblings never end up with gaps or duplicate `order`
    values (e.g. after a session is removed from the middle of a lesson).
    """
    siblings = db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ).all()
    for i, sib in enumerate(siblings):
        sib.order = i


def split_session(db, session_id: uuid.UUID, *, session_minutes: int) -> list[Block]:
    """Cut one over-long `session` Block into several, packed to
    ~`session_minutes` each by the same deterministic bin-packer
    `app.curriculum.segment.partition_by_minutes` uses. The session's own
    ITEMS become the partition's leaves (not a re-walk of the content plane —
    a session's items are always childless, so each is exactly one `Leaf`
    with its own `est_minutes`, no `module_title` grouping applies at this
    level).

    All-or-nothing: every new session + every re-parented item is `add`ed/
    `flush`ed inside this call; if anything raises before the final
    `db.commit()`, `db.rollback()` undoes the whole attempt and the original
    session is left exactly as it was — never half-split.

    Raises `ValueError` if `session_id` doesn't name a real `session` Block,
    or if it has no items to split (nothing to partition).
    """
    session = _get_block_or_raise(db, session_id, kind="session")
    lesson_id = session.parent_id
    original_order = session.order

    items = db.scalars(
        select(Block).where(Block.parent_id == session_id).order_by(Block.order)
    ).all()
    if not items:
        raise ValueError(f"session {session_id} has no items to split")

    leaves = [
        Leaf(block_id=item.id, minutes=item.est_minutes or 1, title=item.title, module_title=None)
        for item in items
    ]
    items_by_id = {item.id: item for item in items}

    try:
        partitions = partition_by_minutes(leaves, session_minutes)

        # Make room for the new parts BEFORE assigning them `order` values:
        # shift every LATER sibling (order > original_order — the split
        # session itself is excluded and about to be deleted) down by
        # `len(partitions) - 1`. Without this, new sessions land at
        # `original_order + i` and collide (tie) with whatever sibling used
        # to sit right after the split session — e.g. splitting the middle
        # of [A(0),B(1),C(2)] into B1,B2 previously assigned B1=1, B2=2,
        # DIRECTLY COLLIDING with C's order=2. `_renormalise_order`'s
        # `ORDER BY Block.order` tiebreak on that collision is not guaranteed
        # to keep C last, so C could silently move before the new parts —
        # reordering untouched sibling work the tutor never touched.
        shift = len(partitions) - 1
        if shift:
            later_siblings = db.scalars(
                select(Block).where(Block.parent_id == lesson_id, Block.order > original_order)
            ).all()
            for sib in later_siblings:
                sib.order = sib.order + shift
            db.flush()

        new_sessions: list[Block] = []
        for i, partition in enumerate(partitions):
            new_session = Block(
                kind="session",
                title=f"{session.title} ({i + 1}/{len(partitions)})",
                parent_id=lesson_id,
                order=original_order + i,
                est_minutes=sum(leaf.minutes for leaf in partition),
                language=session.language,
                student_id=session.student_id,
                plane=session.plane,
            )
            db.add(new_session)
            db.flush()  # new_session.id available for re-parenting below

            for j, leaf in enumerate(partition):
                item = items_by_id[leaf.block_id]
                item.parent_id = new_session.id  # RE-PARENT FIRST
                item.order = j
            db.flush()  # persist the re-parent BEFORE the old session is deleted,
            # so cascade="all, delete-orphan" sees no children left to orphan

            new_sessions.append(new_session)

        db.delete(session)  # safe now: every item has already been re-parented away
        db.flush()

        _renormalise_order(db, lesson_id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    for s in new_sessions:
        db.refresh(s)
    return new_sessions


def merge_sessions(db, session_ids: list[uuid.UUID]) -> Block:
    """Fold >=2 adjacent sessions of the SAME lesson into the first
    (survivor) session, concatenating items in the given session order and
    each session's own item order, summing `est_minutes`.

    Raises `ValueError` if fewer than 2 session ids are given, any id doesn't
    name a real `session` Block, the sessions don't all share the same
    `parent_id` (lesson), or the sessions are not contiguous (adjacent) in
    `order` — merging non-adjacent sessions would cause silent reordering of
    in-between sessions and is rejected.

    All-or-nothing, same rollback contract as `split_session`.
    """
    if len(session_ids) < 2:
        raise ValueError("merge_sessions needs at least 2 session ids")

    sessions = [_get_block_or_raise(db, sid, kind="session") for sid in session_ids]
    lesson_id = sessions[0].parent_id
    if any(s.parent_id != lesson_id for s in sessions):
        raise ValueError("cannot merge sessions from different lessons")

    # Check that sessions are contiguous (adjacent) in order to prevent silent
    # reordering of in-between sessions.
    sorted_sessions = sorted(sessions, key=lambda s: s.order)
    for i in range(len(sorted_sessions) - 1):
        if sorted_sessions[i].order + 1 != sorted_sessions[i + 1].order:
            raise ValueError(
                f"cannot merge non-adjacent sessions: order {sorted_sessions[i].order} "
                f"and order {sorted_sessions[i + 1].order} are not contiguous"
            )

    survivor, donors = sessions[0], sessions[1:]

    try:
        last_survivor_child = db.scalar(
            select(Block).where(Block.parent_id == survivor.id).order_by(Block.order.desc())
        )
        next_order = (last_survivor_child.order + 1) if last_survivor_child is not None else 0

        total_minutes = survivor.est_minutes or 0
        for donor in donors:
            donor_items = db.scalars(
                select(Block).where(Block.parent_id == donor.id).order_by(Block.order)
            ).all()
            for item in donor_items:
                item.parent_id = survivor.id  # RE-PARENT FIRST
                item.order = next_order
                next_order += 1
            db.flush()  # persist re-parent BEFORE deleting the donor session

            total_minutes += donor.est_minutes or 0
            db.delete(donor)  # safe: donor has no children left to orphan

        survivor.est_minutes = total_minutes
        db.flush()

        _renormalise_order(db, lesson_id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(survivor)
    return survivor


def add_session(
    db, lesson_id: uuid.UUID, *, title: str, est_minutes: int | None = None,
    after: uuid.UUID | None = None,
) -> Block:
    """Create a new, empty `session` Block under `lesson_id`. Appended at the
    end by default; if `after` names an existing sibling session, the new
    session is inserted immediately after it instead. Either way, sibling
    `order` is re-normalised to 0..n-1 afterward so the insertion point is
    exactly one position past `after` with no gaps.

    Raises `ValueError` if `lesson_id` doesn't name a real `lesson` Block, or
    `after` is given but doesn't name a session that is actually a child of
    `lesson_id`.
    """
    lesson = _get_block_or_raise(db, lesson_id, kind="lesson")

    siblings = db.scalars(
        select(Block).where(Block.parent_id == lesson.id).order_by(Block.order)
    ).all()

    if after is None:
        insert_at = len(siblings)
    else:
        after_block = _get_block_or_raise(db, after, kind="session")
        if after_block.parent_id != lesson.id:
            raise ValueError(f"session {after} is not a session of lesson {lesson_id}")
        insert_at = next(i for i, s in enumerate(siblings) if s.id == after_block.id) + 1

    new_session = Block(
        kind="session", title=title, parent_id=lesson.id, order=insert_at,
        est_minutes=est_minutes, language=lesson.language, student_id=lesson.student_id,
        plane=lesson.plane,
    )
    siblings.insert(insert_at, new_session)
    db.add(new_session)
    db.flush()

    for i, sib in enumerate(siblings):
        sib.order = i

    db.commit()
    db.refresh(new_session)
    return new_session
