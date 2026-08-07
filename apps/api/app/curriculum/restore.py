"""Put a whole lesson back the way it was before the AI rewrote it.

WHAT THE EXISTING UNDO CANNOT REACH. `POST /blocks/{id}/undo` (`refine.py`)
restores ONE block's text from `meta.prev_body`, and it covers everything that
edits text in place: Extend-with-chat, and the surgical `edit_segment` op that
deliberately copies the same meta contract. `modify_lesson` is different in kind
— it queues the lesson and a worker rewrites every segment from scratch, so the
result is a different SET of segments, possibly a different count, with
different titles and sections. There is no single block whose `prev_body` would
help, which is why `revise.py` captures `meta.prev_segments` instead and why
this module exists.

IT IS A TOGGLE, NOT A DEMOLITION, AND THAT IS THE WHOLE DESIGN. Restoring writes
the segments it is about to replace into `prev_segments` before replacing them.
Flip once and you are back on your version; flip again and you are back on the
AI's. Neither is ever destroyed — so the confirm is honestly "did you mean to
swap" rather than "this cannot be undone", and the standing rule that this app
displaces rather than deletes is satisfied by construction instead of by being
careful. The first draft of the spec said no to a restore precisely because it
looked destructive; making it stash what it replaces is what removed the
objection.

ONE THING THIS DOES NOT SOLVE, recorded so the next person finds it before a
tutor does: ids are NOT preserved. The recreated segments are new rows, so an
`Artifact` pinned to a segment that a rewrite replaced has already had its
`block_id` set to NULL by its own `ondelete="SET NULL"` — the artifact survives
as a standalone, but it loses its attachment and a restore does not bring it
back. Preserving ids would mean diffing the snapshot against the live rows and
updating in place, which is materially more code for a case nobody has hit.

Caller-owned session, same as its siblings under `app/curriculum/`: this flushes
(the delete/recreate ordering needs it) but does NOT commit.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.block import Block


def snapshot_of(db: Session, lesson: Block) -> list[dict]:
    """The lesson's live segments, in the shape `meta.prev_segments` stores.

    Shared by this module and `revise.py`'s `modify_lesson` branch so the two
    can never disagree about the shape — a restore that read a key the capture
    never wrote would fail silently, with the tutor's previous lesson sitting
    right there in the database.
    """
    segs = db.scalars(
        select(Block)
        .where(Block.parent_id == lesson.id, Block.kind == "segment")
        .order_by(Block.order)
    ).all()
    return [
        {"title": s.title, "body": s.body, "section": (s.meta or {}).get("section")}
        for s in segs
    ]


def restore_lesson_segments(db: Session, lesson: Block) -> bool:
    """Swap the lesson's segments for the stashed set. Returns False when there
    is nothing stashed. Mutates; the caller commits."""
    meta = lesson.meta or {}
    stored = meta.get("prev_segments")
    if stored is None:
        return False

    # 1. Capture what we are about to replace — this is what makes it a toggle.
    outgoing = snapshot_of(db, lesson)

    # 2. Delete the current segments. They are leaves, so no re-parenting dance
    #    is needed (unlike `lessons/edit.py`'s split/merge, where items have to
    #    be moved BEFORE their old parent is deleted or `delete-orphan` takes
    #    them along). Flush so the recreated rows below cannot collide on
    #    `order` with rows the session still thinks exist.
    for seg in db.scalars(
        select(Block).where(Block.parent_id == lesson.id, Block.kind == "segment")
    ).all():
        db.delete(seg)
    db.flush()

    # 3. Recreate the stashed set, in order.
    for i, spec in enumerate(stored):
        seg_meta: dict = {"segment_status": "ready"}
        # `segment_status: "ready"` is NOT cosmetic. A restored segment left
        # `queued` would be picked up by the very next `drain_queued_segments`
        # and rewritten — the tutor restores his version and the AI immediately
        # undoes him, which is the worst possible outcome for this feature.
        if spec.get("section"):
            seg_meta["section"] = spec["section"]
        db.add(Block(
            kind="segment",
            title=spec.get("title") or "",
            body=spec.get("body"),
            order=i,
            parent_id=lesson.id,
            language=lesson.language,
            meta=seg_meta,
        ))
    db.flush()

    # 4. The version we just displaced becomes the next thing to flip back to.
    #    WHOLE-DICT REASSIGNMENT — `Block.meta` is plain sa.JSON with no
    #    MutableDict, so an in-place update here would appear to work in dev and
    #    silently lose the tutor's other version in production.
    lesson.meta = {**meta, "prev_segments": outgoing}

    # 5. The board reads `word_count` off the lesson; a restore changes it.
    #    Imported here rather than at module scope: `revise.py` is a heavy import
    #    and this module is pulled in by the router at startup.
    from app.curriculum.revise import _recompute_lesson_word_count

    _recompute_lesson_word_count(db, lesson)
    return True
