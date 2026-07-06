import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.knowledge import Chunk, KnowledgeSource

# Skip cleanly (not error) when no DB is reachable — mirrors test_models_roundtrip.py.
try:
    with engine.connect() as _c:
        _c.execute(text("SELECT 1"))
except Exception:
    pytest.skip(
        "database not reachable — set DATABASE_URL to a running Postgres",
        allow_module_level=True,
    )


def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)


def test_domain_and_cosine_order():
    db = SessionLocal()
    try:
        src = KnowledgeSource(
            type="text", title="t", status="ready", language="en", domain="tone"
        )
        db.add(src)
        db.flush()
        assert src.domain == "tone"

        # Distinct-axis unit vectors: cosine distance depends only on direction, so
        # giving each chunk its own axis makes the nearest neighbour unambiguous for
        # a query that leans toward axis 0. (The brief's original c0=[0]*n/c1=[1]*n/
        # c2=[2]*n vectors are all *parallel* — same direction, different magnitude —
        # so they are equidistant from any query under cosine distance and "nearest
        # == c1" is not well-defined. This version tests the same capability, cosine
        # NN ordering, without that flaw.)
        dim = settings.embed_dim
        axis_vectors = {
            "c_a": [1.0, 0.0, 0.0] + [0.0] * (dim - 3),
            "c_b": [0.0, 1.0, 0.0] + [0.0] * (dim - 3),
            "c_c": [0.0, 0.0, 1.0] + [0.0] * (dim - 3),
        }
        for label, vec in axis_vectors.items():
            db.add(Chunk(source_id=src.id, text=label, embedding=vec))
        db.commit()

        query = [0.9, 0.1, 0.0] + [0.0] * (dim - 3)
        rows = db.scalars(
            select(Chunk).order_by(Chunk.embedding.cosine_distance(query)).limit(1)
        ).all()
        assert rows and rows[0].text == "c_a"  # closest by angle to the query axis
    finally:
        db.close()
