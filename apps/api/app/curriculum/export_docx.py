"""Clean DOCX export of a curriculum (spec 2026-07-20 §4).

Title page, then Heading 1 per module / Heading 2 per lesson / Heading 3 per
blueprint section with the section prose. Deliberately NO citation or source
data anywhere — the tutor asked for a clean teaching document; sources live in
the app's per-lesson Πηγές modal only. Built-in Word styles only ("Heading 1"
... "Title"), which is what keeps the file opening cleanly in both Word and
Apple Pages."""
from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.block import Block

# Rendered when a lesson has no drafted sections yet ("queued"/"drafting"/
# "failed"): the exported document must mirror the curriculum's real shape —
# a silently missing lesson reads as "covered everything" when it didn't.
_UNDRAFTED_NOTE = "— Το μάθημα δεν έχει συνταχθεί ακόμα. —"


def _children(db: Session, parent_id) -> list[Block]:
    return list(db.scalars(
        select(Block).where(Block.parent_id == parent_id).order_by(Block.order)
    ))


def filename_for(course: Block) -> str:
    """`<title>.docx`, filesystem-safe."""
    safe = re.sub(r"[^\w\s\-]", "", course.title, flags=re.UNICODE).strip() or "curriculum"
    return f"{safe}.docx"


def build_curriculum_docx(db: Session, course: Block) -> io.BytesIO:
    doc = Document()

    # --- title page -------------------------------------------------------
    title_p = doc.add_paragraph(style="Title")
    title_p.add_run(course.title)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    modules = _children(db, course.id)
    lesson_count = sum(len([b for b in _children(db, m.id) if b.kind == "lesson"]) for m in modules)
    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    level = (course.target_profile or {}).get("level")
    parts = [p for p in (
        level,
        f"{len(modules)} ενότητες",
        f"{lesson_count} μαθήματα",
    ) if p]
    subtitle.add_run(" · ".join(parts))
    if course.body:
        doc.add_paragraph(course.body)

    # --- modules → lessons → sections ------------------------------------
    for module in modules:
        doc.add_page_break()
        doc.add_heading(module.title, level=1)
        if module.body:
            doc.add_paragraph(module.body)
        for lesson in _children(db, module.id):
            if lesson.kind != "lesson":
                continue
            heading = lesson.title
            if lesson.est_minutes:
                heading = f"{heading} ({lesson.est_minutes}′)"
            doc.add_heading(heading, level=2)
            if lesson.body:
                doc.add_paragraph(lesson.body)
            segments = [b for b in _children(db, lesson.id) if b.kind == "segment"]
            if not segments:
                doc.add_paragraph(_UNDRAFTED_NOTE)
                continue
            for segment in segments:
                doc.add_heading(segment.title, level=3)
                for chunk in (segment.body or "").split("\n\n"):
                    if chunk.strip():
                        doc.add_paragraph(chunk.strip())
            # A REDRAFT-IN-PROGRESS OR FAILED REDRAFT still has its OLD segments —
            # `redraft_curriculum`/`deepen_lesson` requeue the lesson but never
            # delete its prior content, and a worker mid-write hasn't replaced it
            # yet either. Without this, the export above renders that stale prose
            # with no marker at all, which reads as "this is the finished lesson"
            # when it is really either not-yet-rewritten or a redraft the model
            # never finished. Appended AFTER the segments (not instead of them) —
            # the tutor still gets whatever prose exists, just honestly labelled.
            if (lesson.meta or {}).get("draft_status") in ("failed", "drafting"):
                doc.add_paragraph(_UNDRAFTED_NOTE)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf
