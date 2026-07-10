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
    synchronously, exactly as `POST /knowledge/sources` does — then, ONLY IF
    ingestion actually succeeded, flip `note.promoted_to_knowledge` and
    commit. Returns the created source.

    `kind="text"` unconditionally: the `kind="url"` SSRF guard inside
    `create_source` never applies here, and `MAX_TEXT_CHARS` (1,000,000
    chars) still guards an oversized note body for free — `create_source`
    raises its own `HTTPException(413, ...)` in that case, which propagates
    up through this call unmodified (FastAPI handles it the same as if
    raised directly in a router).

    INGEST-SUCCESS GATE (whole-plan review, Important 2): `ingest_source`
    SWALLOWS ordinary ingestion failures (embed model down, extraction
    error) and leaves the source at `status="failed"` rather than raising
    (see `app/brain/ingest.py`'s swallow-not-raise design). Since promotion
    is a documented ONE-WAY flip (there is no un-promote; a second attempt
    hard-errors on both the HTTP route's 409 and the tool's already-promoted
    guard), unconditionally flipping the flag on a failed ingest would
    PERMANENTLY and SILENTLY "succeed" an empty, non-retrievable source. So
    the flag is only flipped when `source.status == "ready"`; otherwise this
    raises `ValueError` (leaving the flag False so the note can be retried).
    Both callers surface it: `routers/notes.py`'s `promote_note_endpoint`
    maps it to a 502 (an upstream-dependency failure, same class as the
    guided-JSON generators' `GuidedJSONError`->502), and `app/agent/tools.
    py`'s `_promote_note_to_knowledge` catches it into a graceful `{"error"}`
    dict — one gate here fixes both paths. The failed source row stays behind
    as an inert (0-char, non-retrievable) audit record; a retry creates a
    fresh source rather than reusing it.
    """
    payload = SourceCreate(kind="text", title=note.title, text=note.body)
    source = create_source(payload, db)

    if source.status != "ready":
        raise ValueError(
            f"knowledge ingestion did not complete (source status={source.status!r}); "
            "the note was NOT promoted — please retry"
        )

    note.promoted_to_knowledge = True
    db.commit()
    return source
