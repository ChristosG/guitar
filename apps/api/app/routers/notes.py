"""`/notes` routes: CRUD for free-form teaching notes + promote-to-Brain.

No-auth PoC posture, same as `routers/artifacts.py`/`routers/students.py` —
no authentication/authorization here either; this deploys origin-locked
behind Cloudflare for a single user.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.note import Note
from app.models.student import Student
from app.notes.promote import promote_note
from app.schemas.notes import NoteCreate, NoteOut, NotePromoteOut, NoteUpdate

router = APIRouter(tags=["notes"])


def _to_note_out(note: Note) -> NoteOut:
    return NoteOut.model_validate(note, from_attributes=True)


def _get_note_or_404(db: Session, note_id: UUID) -> Note:
    note = db.get(Note, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    return note


def _get_student_or_404(db: Session, student_id: UUID) -> Student:
    # Same lookup routers/students.py's own `_get_student_or_404` performs
    # (not imported from there — small deliberate duplication over a
    # cross-router import, mirroring routers/artifacts.py's own `_get_
    # block_or_404` precedent for the identical situation).
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status_code=404, detail="student not found")
    return student


@router.post("/notes", response_model=NoteOut)
def create_note(payload: NoteCreate, db: Session = Depends(get_db)) -> NoteOut:
    if payload.title == "":
        # Satisfies the NOT NULL constraint but is still a useless title —
        # mirrors routers.artifacts.create_artifact's / routers.curriculum.
        # update_block's identical empty-title guard.
        raise HTTPException(status_code=422, detail="title cannot be empty")
    if payload.student_id is not None:
        _get_student_or_404(db, payload.student_id)

    note = Note(
        title=payload.title, body=payload.body, tags=payload.tags,
        student_id=payload.student_id,
    )
    db.add(note)
    db.commit()  # Postgres RETURNING populates id/created_at/updated_at eagerly — no refresh needed
    return _to_note_out(note)


@router.get("/notes", response_model=list[NoteOut])
def list_notes(student_id: UUID | None = None, db: Session = Depends(get_db)) -> list[NoteOut]:
    stmt = select(Note).order_by(Note.created_at.desc())
    if student_id is not None:
        stmt = stmt.where(Note.student_id == student_id)
    notes = db.scalars(stmt).all()
    return [_to_note_out(n) for n in notes]


@router.get("/notes/{note_id}", response_model=NoteOut)
def get_note(note_id: UUID, db: Session = Depends(get_db)) -> NoteOut:
    return _to_note_out(_get_note_or_404(db, note_id))


@router.patch("/notes/{note_id}", response_model=NoteOut)
def update_note(note_id: UUID, payload: NoteUpdate, db: Session = Depends(get_db)) -> NoteOut:
    """`exclude_unset=True, exclude_none=True` — same "null is always a
    no-op" rule `routers.curriculum.update_block` established for the
    identical shape (a NOT NULL `title`/`body` sitting alongside a nullable
    `student_id`): dropping every null-valued field uniformly, rather than
    only the NOT NULL ones, means one rule works for all of them, at the
    accepted cost that `student_id` also can't be explicitly cleared back
    to `null` via PATCH this way (not exercised by this task's brief; a
    fresh unlinked note is the workaround if ever needed).
    """
    note = _get_note_or_404(db, note_id)
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if updates.get("title") == "":
        raise HTTPException(status_code=422, detail="title cannot be empty")
    if "student_id" in updates:
        _get_student_or_404(db, updates["student_id"])
    for field, value in updates.items():
        setattr(note, field, value)
    db.commit()
    return _to_note_out(note)


@router.delete("/notes/{note_id}", status_code=204, response_model=None)
def delete_note(note_id: UUID, db: Session = Depends(get_db)) -> None:
    note = _get_note_or_404(db, note_id)
    db.delete(note)
    db.commit()


@router.post("/notes/{note_id}/promote", response_model=NotePromoteOut)
def promote_note_endpoint(note_id: UUID, db: Session = Depends(get_db)) -> NotePromoteOut:
    """Turn a note into a ready Brain `KnowledgeSource` (`app.notes.promote.
    promote_note`). Idempotent-as-409, not idempotent-as-no-op: a note
    already promoted refuses a second promote outright rather than quietly
    doing nothing, same "acting again on an already-resolved transition"
    409 vocabulary as `routers/chat.py`'s resolve-twice / open-approval
    guards — a silent 200 no-op here would hide the fact that calling this
    twice would otherwise create a second, duplicate `KnowledgeSource` for
    the same note, which is never what a caller wants.
    """
    note = _get_note_or_404(db, note_id)
    if note.promoted_to_knowledge:
        raise HTTPException(status_code=409, detail="note already promoted to knowledge")
    if not note.body or not note.body.strip():
        raise HTTPException(status_code=422, detail="cannot promote a note with an empty body")

    try:
        source = promote_note(db, note)
    except ValueError as e:
        # promote_note raises when ingestion didn't complete (source status
        # != "ready"): `ingest_source` SWALLOWS ordinary failures, so this is
        # an upstream-dependency failure (embed/extract) -> 502, same class as
        # the guided-JSON generators' GuidedJSONError->502. The note's
        # `promoted_to_knowledge` flag stays False (promote_note didn't flip
        # it), so the tutor can retry rather than being permanently stuck with
        # a "promoted" but empty, non-retrievable source. (An oversized-body
        # HTTPException(413) from create_source is NOT a ValueError and still
        # propagates unmodified, exactly as before.)
        raise HTTPException(status_code=502, detail=str(e)) from e
    return NotePromoteOut(**_to_note_out(note).model_dump(), source_id=source.id)
