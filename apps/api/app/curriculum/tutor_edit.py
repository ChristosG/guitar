"""The tutor's hand edits, as data the rest of the engine can see.

`meta.tutor_edited = {"at": iso, "prev_body": baseline, "count": n}` on a
segment (or a lesson, for its summary). `prev_body` is the body AS THE AI LAST
WROTE IT — the first previous body since the last AI write — so «Τι άλλαξε;»
and the propagation planner always diff the tutor's version against the
model's, never against his own earlier save. Every AI write path
(`refine`, `segment_generate`, `persist_lesson`, `restore`) calls
`clear_tutor_edit` on the segments it rewrites.

Whole-dict reassignment throughout: `Block.meta` is plain sa.JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.curriculum.depth import count_words
from app.models.block import Block


def mark_tutor_edit(block: Block, previous_body: str | None, now: datetime | None = None) -> None:
    meta = dict(block.meta or {})
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    current = meta.get("tutor_edited")
    if isinstance(current, dict) and "prev_body" in current:
        entry = {**current, "at": stamp, "count": int(current.get("count") or 0) + 1}
    else:
        entry = {"at": stamp, "prev_body": previous_body or "", "count": 1}
    block.meta = {**meta, "tutor_edited": entry}


def clear_tutor_edit(block: Block) -> None:
    meta = block.meta or {}
    if "tutor_edited" not in meta:
        return
    block.meta = {k: v for k, v in meta.items() if k != "tutor_edited"}


def _segments(db, lesson: Block) -> list[Block]:
    return db.scalars(
        select(Block)
        .where(Block.parent_id == lesson.id, Block.kind == "segment")
        .order_by(Block.order)
    ).all()


def recompute_lesson_words(db, lesson: Block) -> int:
    """`word_count`/`meets_floor` from the segments AS THEY STAND — Greek-safe
    (`depth.count_words`, `\\w+`), the same counter `measure()` uses at draft
    time, so a hand edit and a redraft agree on what a word is."""
    total = count_words(lesson.body) + sum(count_words(s.body) for s in _segments(db, lesson))
    meta = {**(lesson.meta or {}), "word_count": total}
    floor = meta.get("floor_words")
    if floor is not None:
        meta["meets_floor"] = total >= int(floor)
    lesson.meta = meta
    return total


def tutor_edited_sections(db, lesson: Block) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for seg in _segments(db, lesson):
        te = (seg.meta or {}).get("tutor_edited")
        if not isinstance(te, dict):
            continue
        key = (seg.meta or {}).get("section") or seg.title
        out[key] = {"body": seg.body or "", "prev_body": te.get("prev_body") or "",
                    "at": te.get("at"), "title": seg.title}
    return out
