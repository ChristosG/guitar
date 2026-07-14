from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

# pool_size/max_overflow are NOT defaults — see `Settings.db_pool_size`. The
# Stage-6 draft fan-out holds N worker sessions while the board polls progress
# from the request path; at SQLAlchemy's default 5/10 the poll is what starves,
# and a starved poll is indistinguishable from a broken feature.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
