"""WHAT SITS ON EITHER SIDE OF ONE LESSON — computed in exactly ONE place.

Two callers need this and they arrive from opposite directions: the drafting
fan-out (`jobs/curriculum_draft._neighbours`) builds the map for a whole
curriculum in Phase A, and the lesson panel (`curriculum/lesson_ai.
neighbours_dict`) builds it for the single lesson the tutor is editing. Both
feed the SAME prompt block (`draft.LESSON_NEIGHBOURS_BLOCK`), so two
implementations would be two chances for the fan-out's «—» and the panel's «—»
to stop meaning the same thing — and nobody would ever see it, because the only
reader is a model.

Pure and duck-typed on purpose: it takes the ordered siblings (anything with
`.title` and a `.meta` dict) and an index, and does no database work of its own.
The queries stay with the callers, who already hold the session they are allowed
to hold.
"""
from __future__ import annotations

from typing import Sequence

# What a missing neighbour renders as. An em dash rather than an empty string:
# the first lesson of a module must SAY it is the first one, not leave the model
# with a blank it can fill in with an assumption.
NONE_MARKER = "—"


def _line(block) -> str:
    """«title — objective», or just the title when it has no objective."""
    objective = (getattr(block, "meta", None) or {}).get("objective") or ""
    return f"{block.title} — {objective}".rstrip(" —")


def neighbours_of(siblings: Sequence, index: int | None) -> dict[str, str]:
    """`{"prev": ..., "next": ..., "siblings": ...}` for the lesson at `index`.

    `siblings` is the module's lessons IN TEACHING ORDER; `index` is the position
    of the lesson being drafted within it (`None` — a lesson whose row is not in
    the list — yields an all-«—» dict rather than raising, because a prompt block
    is not the place to discover a stale id).
    """
    if index is None:
        return {"prev": NONE_MARKER, "next": NONE_MARKER, "siblings": NONE_MARKER}
    prev = siblings[index - 1] if index > 0 else None
    nxt = siblings[index + 1] if index + 1 < len(siblings) else None
    others = ", ".join(s.title for i, s in enumerate(siblings) if i != index)
    return {
        "prev": _line(prev) if prev is not None else NONE_MARKER,
        "next": _line(nxt) if nxt is not None else NONE_MARKER,
        "siblings": others or NONE_MARKER,
    }
