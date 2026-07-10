import uuid

from sqlalchemy import JSON, Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PkMixin, TimestampMixin


class Note(Base, PkMixin, TimestampMixin):
    """A free-form teaching note (Plan 6 Task 1), optionally promotable into
    a Brain `KnowledgeSource` (`POST /notes/{id}/promote` ->
    `app.notes.promote.promote_note`) once it's ready to ground future
    generation/retrieval — see that module for the promote flow itself.

    `student_id` is nullable + `SET NULL` on delete, the same choice and
    reasoning as `Artifact.block_id`: a note that loses its linked student
    (the Student row is deleted) is still a valid standalone note — e.g. a
    general teaching note not tied to any one student — so deleting the
    Student should detach it, not destroy it. This uses a real `ForeignKey`
    (not a bare, unconstrained UUID column) so the reference stays valid for
    as long as it exists, consistent with every other LIVE cross-entity
    reference in this codebase (`Artifact.block_id`, `Progress.student_id`,
    `Chunk.source_id`) — unlike e.g. `GenerationJob.result_root_id`/
    `ChatSession.student_id`, which are deliberately unconstrained because
    those rows must stay readable with a now-dangling id even after the row
    they reference is gone. A Note has no such requirement, so there's no
    reason to give up referential integrity here.

    `promoted_to_knowledge` starts `False` and is a one-way flip (Task 1's
    promote endpoint refuses a second promote rather than clearing/re-
    flipping it) — there is no un-promote action.
    """

    __tablename__ = "note"
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    # `default=list` (a callable), NOT `default=[]` — mirrors Artifact.tags's
    # own precedent/reasoning against the classic mutable-default gotcha.
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    student_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("student.id", ondelete="SET NULL"), nullable=True, index=True)
    promoted_to_knowledge: Mapped[bool] = mapped_column(Boolean, default=False)
