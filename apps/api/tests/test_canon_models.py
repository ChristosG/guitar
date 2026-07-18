"""The concept canon's four tables (Part B, Task C1).

Mirrors `test_chat_models.py`'s skip-guard + `setup_module` convention.

The two tests that matter here are the DDL ones, and they exist because both
facts are invisible from Python until the day they are not:

  * `pages` must be a REAL `int[]`. A JSON column round-trips `[47, 48]` through
    SQLAlchemy just as happily and looks identical in every Python assertion —
    and then `47 = ANY(pages)`, the query C5 hydrates a lesson with, is a syntax
    error against it.
  * `ON DELETE CASCADE` is enforced by POSTGRES, not by the ORM. There is no
    `relationship()` on these models, so a `db.delete(source)` that leaves claims
    behind fails nowhere in Python — it just leaves the canon quoting a book that
    is gone.
"""
import uuid

import pytest
from sqlalchemy import text

from app.db import Base, SessionLocal, engine

try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )

import app.models  # noqa: F401  register every model's table on Base.metadata
from app.models.canon import BookCompile, Concept, ConceptAlias, ConceptClaim
from app.models.knowledge import KnowledgeSource


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


def _source(db, title="Guitar Exercises Made Simple") -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title=title, status="ready")
    db.add(source)
    db.flush()
    return source


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_concept_and_claim_round_trip():
    db = SessionLocal()
    try:
        source = _source(db)
        concept = Concept(
            key="pickup-height-and-its-effect-on-attack",
            label_en="Pickup height and its effect on attack",
            label_el="Ύψος μαγνήτη και η επίδρασή του στην επίθεση",
        )
        db.add(concept)
        db.flush()
        db.add(ConceptClaim(
            concept_id=concept.id,
            source_id=source.id,
            text="Raising the treble side sharpens pick attack.",
            pages=[47, 48],
            stance="recommends raising the treble side",
            depth="primary",
            grounding="author",
        ))
        db.commit()

        got = db.query(ConceptClaim).one()
        assert got.pages == [47, 48]
        assert got.concept_id == concept.id
        assert got.source_id == source.id
        assert got.depth == "primary"
        assert got.grounding == "author"
        assert got.created_at is not None

        again = db.query(Concept).one()
        assert again.label_el == "Ύψος μαγνήτη και η επίδρασή του στην επίθεση"
        assert again.created_at is not None and again.updated_at is not None
    finally:
        db.close()


def test_concept_alias_records_what_each_book_called_it():
    """The whole reason `concept_alias` exists: a bad C3 merge must be reversible
    WITHOUT recompiling (i.e. without re-spending). The alias keeps the book's own
    name for the concept, per source."""
    db = SessionLocal()
    try:
        hunter = _source(db, "Tone Manual")
        gallagher = _source(db, "Guitar Tone")
        concept = Concept(key="pickup-height", label_en="Pickup height")
        db.add(concept)
        db.flush()
        db.add_all([
            ConceptAlias(concept_id=concept.id, source_id=hunter.id,
                         alias="pickup height"),
            ConceptAlias(concept_id=concept.id, source_id=gallagher.id,
                         alias="adjusting pickup height"),
        ])
        db.commit()

        aliases = {a.source_id: a.alias for a in db.query(ConceptAlias).all()}
        assert aliases == {hunter.id: "pickup height",
                           gallagher.id: "adjusting pickup height"}
    finally:
        db.close()


def test_book_compile_round_trip_keyed_on_source():
    """`source_id` IS the primary key — one compile record per book, so
    "was this compiled by the good model?" has exactly one answer."""
    db = SessionLocal()
    try:
        source = _source(db)
        db.add(BookCompile(
            source_id=source.id, status="ready", model="claude-sonnet-5",
            token_count=23_000, concept_count=31,
        ))
        db.commit()

        got = db.query(BookCompile).one()
        assert got.source_id == source.id
        assert got.model == "claude-sonnet-5"
        assert got.token_count == 23_000
        assert got.concept_count == 31
        assert got.error is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The DDL facts
# ---------------------------------------------------------------------------

def test_pages_is_a_real_int_array_not_json():
    """`_int4` — Postgres' name for `integer[]`. See the module docstring."""
    db = SessionLocal()
    try:
        udt = db.execute(text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'concept_claim' AND column_name = 'pages'"
        )).scalar_one()
        assert udt == "_int4"
    finally:
        db.close()


def test_pages_supports_the_array_containment_query_c5_will_hydrate_with():
    """Not a tautology of the test above: this is the actual query shape — "which
    claims cite page 47?" — that a JSON column cannot answer."""
    db = SessionLocal()
    try:
        source = _source(db)
        concept = Concept(key="alternate-picking", label_en="Alternate picking")
        db.add(concept)
        db.flush()
        db.add_all([
            ConceptClaim(concept_id=concept.id, source_id=source.id,
                         text="on p.47", pages=[47, 48], grounding="author"),
            ConceptClaim(concept_id=concept.id, source_id=source.id,
                         text="elsewhere", pages=[112], grounding="author"),
        ])
        db.commit()

        hits = db.execute(text(
            "SELECT text FROM concept_claim WHERE 47 = ANY(pages)"
        )).scalars().all()
        assert hits == ["on p.47"]
    finally:
        db.close()


def test_deleting_a_source_cascades_its_claims_and_aliases():
    """Enforced by Postgres, not by the ORM — there is no `relationship()` here.
    A canon that cites a book that is gone is a citation the tutor cannot open."""
    db = SessionLocal()
    try:
        source = _source(db)
        concept = Concept(key="tone-woods", label_en="Tone woods")
        db.add(concept)
        db.flush()
        db.add_all([
            ConceptClaim(concept_id=concept.id, source_id=source.id,
                         text="mahogany is warm", pages=[12], grounding="author"),
            ConceptAlias(concept_id=concept.id, source_id=source.id,
                         alias="woods"),
            BookCompile(source_id=source.id, status="ready",
                        model="claude-sonnet-5"),
        ])
        db.commit()

        db.delete(source)
        db.commit()

        assert db.query(ConceptClaim).count() == 0
        assert db.query(ConceptAlias).count() == 0
        assert db.query(BookCompile).count() == 0
        # The concept itself SURVIVES: it is canon-level, not book-level. C3 may
        # later find it unsupported and prune it — that is a decision, not a
        # side effect of deleting one book of ten.
        assert db.query(Concept).count() == 1
    finally:
        db.close()


def test_deleting_a_concept_cascades_its_claims_and_aliases():
    db = SessionLocal()
    try:
        source = _source(db)
        concept = Concept(key="string-gauge", label_en="String gauge")
        db.add(concept)
        db.flush()
        db.add_all([
            ConceptClaim(concept_id=concept.id, source_id=source.id,
                         text="heavier strings, fatter tone", pages=[3],
                         grounding="author"),
            ConceptAlias(concept_id=concept.id, source_id=source.id,
                         alias="gauge"),
        ])
        db.commit()

        db.delete(concept)
        db.commit()

        assert db.query(ConceptClaim).count() == 0
        assert db.query(ConceptAlias).count() == 0
        assert db.query(KnowledgeSource).count() == 1  # the book is not the concept
    finally:
        db.close()


def test_concept_key_is_unique():
    """C2 compiles book after book. The second book to name a concept the same
    thing must REUSE the row (that is a free reconciliation), and the constraint
    is what makes `_get_or_create` a real upsert rather than a race."""
    from sqlalchemy.exc import IntegrityError

    db = SessionLocal()
    try:
        db.add(Concept(key="pickup-height", label_en="Pickup height"))
        db.commit()
        db.add(Concept(key="pickup-height", label_en="Pickup Height (again)"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_claim_requires_a_real_source_and_concept():
    """A claim's `source_id` is what makes it citable. A dangling one is a
    citation to nothing."""
    from sqlalchemy.exc import IntegrityError

    db = SessionLocal()
    try:
        concept = Concept(key="bridge-saddles", label_en="Bridge saddles")
        db.add(concept)
        db.flush()
        db.add(ConceptClaim(concept_id=concept.id, source_id=uuid.uuid4(),
                            text="ghost", pages=[1], grounding="author"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_concept_claim_anchor_is_nullable_text(db):
    """The verbatim quote quote-based citations resolve from (Unit B). Nullable:
    the four books compiled before Unit B carry no anchor, and a figure-grounded
    claim stores NULL on purpose so `reresolve_source` skips it."""
    from app.models.canon import Concept, ConceptClaim
    from app.models.knowledge import KnowledgeSource

    src = KnowledgeSource(type="pdf", title="Anchor Book", status="ready")
    db.add(src); db.flush()
    concept = Concept(key="tonewoods", label_en="Tonewoods")
    db.add(concept); db.flush()

    # anchor present
    c1 = ConceptClaim(concept_id=concept.id, source_id=src.id,
                      text="mahogany is warm", pages=[12], grounding="author",
                      anchor="mahogany bodies read warm and thick through the mids")
    # anchor absent (pre-Unit-B / figure claim) — must be allowed to be NULL
    c2 = ConceptClaim(concept_id=concept.id, source_id=src.id,
                      text="see the diagram", pages=[31], grounding="figure",
                      anchor=None)
    db.add_all([c1, c2]); db.commit()
    db.refresh(c1); db.refresh(c2)
    assert c1.anchor.startswith("mahogany bodies read warm")
    assert c2.anchor is None
