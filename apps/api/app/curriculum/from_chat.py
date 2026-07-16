"""Turning a chat answer into a curriculum lesson — the chat→curriculum bridge.

The tutor asks something in chat, gets an answer worth teaching, and presses
"add to curriculum" under the bubble. What lands is a REAL lesson row under the
module he picked (picked from a SELECT over the database — the model is nowhere
near this path, so there is nothing to hallucinate): the lesson carries one
segment holding the answer verbatim, plus whatever library citations the chat
turn was grounded on, mapped into the same `meta.citations` shape the board's
provenance chips already render.

DELIBERATELY NOT AN LLM CALL. The content the tutor approved is the content
that lands — restructuring it through another model call would cost money to
produce something he did not read. The upgrade path to a full 2,200-word lesson
already exists and is one click away: DEEPEN re-queues the lesson through the
ordinary draft fan-out, which rewrites it from the library at full length.
`meta.origin = "chat"` (and the session id) keep the provenance visible.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.block import Block

# Block.title is String(300); a chat-derived title is clamped, never 500'd.
MAX_TITLE = 300

# The one segment's heading, in the course's language. Mirrors
# `curriculum/draft.py`'s SECTION_LABELS pattern (labels land on Block.title so
# the row is legible in the DB and any export; the frontend renders body text).
FROM_CHAT_LABELS = {"el": "Από τη συνομιλία", "en": "From chat"}


class FromChatError(ValueError):
    """A bridge request the tree cannot accept (wrong kind, empty content).
    The router turns it into a 4xx — never a 500."""


def _map_citations(citations: list | None) -> list[dict]:
    """Chat citations ({source_id, source_title, page_no, ...}) → the board's
    provenance shape ({source_id, source_title, page}). Only page-addressable
    ones survive: a chip with nowhere real to deep-link would break the
    'click it to verify' promise the chips exist for."""
    out: list[dict] = []
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        page = c.get("page_no") if c.get("page_no") is not None else c.get("page")
        if not isinstance(page, int):
            continue
        out.append({
            "source_id": str(c.get("source_id")),
            "source_title": c.get("source_title"),
            "page": page,
        })
    return out


def add_lesson_from_chat(
    db,
    module_id: uuid.UUID,
    *,
    title: str,
    content: str,
    citations: list | None = None,
    chat_session_id: uuid.UUID | None = None,
) -> Block:
    """One lesson (+ its single segment) under `module_id`, appended, `ready`.

    `ready`, not `queued`: it HAS content — the chat answer — and must not be
    picked up and overwritten by the next Resume. Deepen is the explicit "now
    write it out properly" action, and that path re-queues it deliberately.
    Commits.
    """
    module = db.get(Block, module_id)
    if module is None:
        raise FromChatError(f"module not found: {module_id}")
    if module.kind != "module":
        raise FromChatError(f"block {module_id} is a {module.kind!r}, not a module")

    clean_title = (title or "").strip()[:MAX_TITLE]
    clean_content = (content or "").strip()
    if not clean_title:
        raise FromChatError("the lesson needs a title")
    if not clean_content:
        raise FromChatError("the chat answer is empty — nothing to add")

    siblings = db.scalars(
        select(Block)
        .where(Block.parent_id == module.id, Block.kind == "lesson")
        .order_by(Block.order)
    ).all()

    lesson = Block(
        kind="lesson",
        title=clean_title,
        body=None,
        order=len(siblings),
        parent_id=module.id,
        language=module.language,
        meta={
            "draft_status": "ready",
            "objective": "",
            "origin": "chat",
            "chat_session_id": str(chat_session_id) if chat_session_id else None,
            "word_count": len(clean_content.split()),
        },
    )
    db.add(lesson)
    db.flush()

    label = FROM_CHAT_LABELS.get(module.language, FROM_CHAT_LABELS["el"])
    db.add(Block(
        kind="segment",
        title=label,
        body=clean_content,
        order=0,
        parent_id=lesson.id,
        language=module.language,
        meta={
            "section": "chat",
            "citations": _map_citations(citations),
        },
    ))

    db.commit()
    db.refresh(lesson)
    return lesson
