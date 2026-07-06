import uuid
from app.db import Base, engine, SessionLocal
from app.models.student import Student
from app.models.block import Block
from app.models.knowledge import KnowledgeSource, Chunk

def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)

def test_recursive_block_tree_and_vector_roundtrip():
    db = SessionLocal()
    try:
        course = Block(kind="course", title="Guitar Tone & Gear", order=0, language="en")
        db.add(course); db.flush()
        module = Block(kind="module", title="The Guitar", order=0, parent_id=course.id, language="en")
        db.add(module); db.flush()
        assert module.parent_id == course.id

        src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds",
                              status="ready", language="en")
        db.add(src); db.flush()
        chunk = Chunk(source_id=src.id, text="A humbucker cancels hum.",
                      section_path="Ch1", page=25, embedding=[0.1] * 2560)
        db.add(chunk); db.commit()

        got = db.query(Chunk).filter_by(id=chunk.id).one()
        assert len(got.embedding) == 2560 and got.source_id == src.id
    finally:
        db.close()
