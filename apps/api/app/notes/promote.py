"""Promote a Note into a Brain `KnowledgeSource` (Task 1's `POST /notes/{id}/
promote`): turns free-form teaching-note text into ready-to-retrieve Brain
material, reusing the EXACT same create+ingest path `POST /knowledge/sources`
itself uses (`app.routers.knowledge.create_source`) rather than
reimplementing its row-create + ingest logic — same reuse precedent as
`app.seed`'s own `_ensure_source` (which calls the identical function the
identical way, from a non-router module).

Precondition checks (already-promoted -> 409, empty body -> 422) are the
router's job (`routers/notes.py`), not this module's — same "guards live at
the HTTP boundary, this module does the mechanical work" split `create_
source`/`ingest_source` already establish for the Knowledge pipeline itself
(e.g. `create_source` checks `MAX_TEXT_CHARS` before calling `ingest_source`,
`ingest_source` doesn't re-check it). `promote_note` assumes both
preconditions already hold.
"""
from sqlalchemy.orm import Session

from app.models.note import Note
from app.routers.knowledge import create_source
from app.schemas.knowledge import SourceCreate, SourceOut


def promote_note(db: Session, note: Note) -> SourceOut:
    """Create a `kind="text"` `KnowledgeSource` from `note` (title/body
    verbatim) via `create_source` — which creates the row AND ingests it
    synchronously, exactly as `POST /knowledge/sources` does — then flip
    `note.promoted_to_knowledge` and commit. Returns the created source.

    `kind="text"` unconditionally: the `kind="url"` SSRF guard inside
    `create_source` never applies here, and `MAX_TEXT_CHARS` (1,000,000
    chars) still guards an oversized note body for free — `create_source`
    raises its own `HTTPException(413, ...)` in that case, which propagates
    up through this call unmodified (FastAPI handles it the same as if
    raised directly in a router).
    """
    payload = SourceCreate(kind="text", title=note.title, text=note.body)
    source = create_source(payload, db)

    note.promoted_to_knowledge = True
    db.commit()
    return source
