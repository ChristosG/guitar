import pytest
from sqlalchemy import text

from app.db import Base, engine, SessionLocal
from app.models.student import Student
from app.models.block import Block
from app.models.knowledge import KnowledgeSource, Chunk

# Skip cleanly (not error) when no DB is reachable — e.g. a bare `pytest` on a fresh
# checkout without DATABASE_URL pointing at a running Postgres.
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

def test_recursive_block_tree_and_vector_roundtrip():
    # Write + commit in one session.
    db = SessionLocal()
    try:
        course = Block(kind="course", title="Guitar Tone & Gear", order=0, language="en")
        db.add(course); db.flush()
        module = Block(kind="module", title="The Guitar", order=0, parent_id=course.id, language="en")
        db.add(module); db.flush()

        src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds",
                              status="ready", language="en")
        db.add(src); db.flush()
        chunk = Chunk(source_id=src.id, text="A humbucker cancels hum.",
                      section_path="Ch1", embedding=[0.1] * 2560)
        db.add(chunk); db.commit()
        course_id, module_id, src_id, chunk_id = course.id, module.id, src.id, chunk.id
    finally:
        db.close()

    # Read back in a FRESH session so it's a genuine DB round-trip (not the
    # identity-map object reused under expire_on_commit=False) — this actually
    # exercises pgvector's read-path deserialization and the persisted self-FK.
    db2 = SessionLocal()
    try:
        got_module = db2.get(Block, module_id)
        assert got_module is not None
        assert got_module.parent_id == course_id           # recursive tree persisted

        got_chunk = db2.get(Chunk, chunk_id)
        assert got_chunk is not None
        assert got_chunk.source_id == src_id
        assert len(got_chunk.embedding) == 2560             # 2560-dim vector read from pgvector
        assert abs(float(got_chunk.embedding[0]) - 0.1) < 1e-4
    finally:
        db2.close()
