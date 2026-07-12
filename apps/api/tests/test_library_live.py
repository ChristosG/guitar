"""THE ACCEPTANCE TEST for the Library (Plan 9 Task 10).

This is the point of the whole plan. Green unit tests prove the code *runs*;
this proves the tutor's actual book is IN THE APP and that a grounded answer
cites a page we can SHOW him.

Runs READ-ONLY against the REAL APP DB (`guitar`) and the REAL local model —
deliberately, not against a fresh `guitar_test` ingest:

  * The criterion is "the book is in HIS app", not "the pipeline can ingest a
    book if you ask it nicely". Re-ingesting a throwaway second copy into
    `guitar_test` would prove the pipeline, then throw the result away. The
    pipeline itself is already pinned by test_ocr.py / test_paginate.py /
    test_ingest.py (418 unit tests).
  * conftest.py force-pins DATABASE_URL to `guitar_test` and TRUNCATEs every
    table after each test, so this module opens its OWN engine/session
    against `guitar` and never writes — the autouse truncate fixture cannot
    reach it, and this test can never destroy his library.

Run:
  cd apps/api && LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1 \
    ./.venv/bin/python -m pytest -m integration -v -s tests/test_library_live.py
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.brain.retrieve import search
from app.models.knowledge import Collection, KnowledgeSource, Page

APP_DATABASE_URL = os.environ.get(
    "APP_DATABASE_URL", "postgresql+psycopg://guitar:guitar@localhost:5434/guitar"
)

# A question whose answer lives in THIS BOOK's own prose and nowhere else in
# the library: the Course Spine (the only other content-bearing source) is a
# module outline that never discusses pick gauge, and a generic LLM answering
# from parametric memory would not reproduce the book's specific wording.
BOOK_ONLY_QUESTION = "what does the thickness of a pick do to the tone?"


@pytest.fixture(scope="module")
def app_db():
    """Session on the REAL app DB. Read-only by contract — see module docstring."""
    engine = create_engine(APP_DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.mark.integration
def test_the_book_is_in_the_app_ready_and_filed(app_db):
    """The tutor's 77-page scanned book is really in his library, honestly
    green (D6: `ready` iff it actually carries text), and filed."""
    book = (
        app_db.query(KnowledgeSource)
        .filter_by(title="Getting Great Guitar Sounds")
        .one_or_none()
    )
    assert book is not None, "the book is not in the app DB at all"

    pages = app_db.query(Page).filter_by(source_id=book.id).order_by(Page.page_no).all()
    ready = [p for p in pages if p.status == "ready"]
    empty = [p for p in pages if p.status == "empty"]
    failed = [p for p in pages if p.status == "failed"]
    print(
        f"\nBOOK: status={book.status} char_count={book.char_count} "
        f"pages={len(pages)} ready={len(ready)} empty(blank)={len(empty)} failed={len(failed)}"
    )
    if failed:
        print(f"FAILED PAGES: {[(p.page_no, p.ocr_error) for p in failed]}")
    if empty:
        print(f"EMPTY (blank) PAGES: {[p.page_no for p in empty]}")

    assert len(pages) == 77, "every physical page must be paginated"
    assert len(ready) >= 70, "the vast majority of the book must be transcribed"
    assert not failed, f"pages failed OCR: {[(p.page_no, p.ocr_error) for p in failed]}"

    # D6: 'ready' must MEAN something. This is the exact lie the redesign exists to kill.
    assert book.status == "ready"
    assert book.char_count and book.char_count > 20_000, (
        "the book transcribed to suspiciously little text"
    )

    # Filed, not Unfiled (D7).
    assert book.collection_id is not None, "the book is Unfiled"
    collection = app_db.get(Collection, book.collection_id)
    assert collection is not None
    print(f"FILED UNDER: {collection.name}")


@pytest.mark.integration
def test_a_book_only_question_returns_a_citation_we_can_actually_show(app_db):
    """THE ACCEPTANCE CRITERION.

    Ask something only the book can answer. Get back a real chunk, attributed
    to a real page, whose SCAN EXISTS — so the citation is VERIFIABLE, not
    merely claimed.
    """
    hits = search(app_db, BOOK_ONLY_QUESTION, k=5)
    assert hits, "nothing retrieved — the library answered a guitar question with silence"

    top = hits[0]
    print(f"\nQUESTION: {BOOK_ONLY_QUESTION}")
    print(f"TOP HIT: source={top.source_title!r} page={top.page} score={top.score:.4f}")
    print(f"RETRIEVED TEXT:\n{top.text}\n")

    # It must come from the book, not the Course Spine — this question is
    # book-only by construction (see BOOK_ONLY_QUESTION's comment).
    assert top.source_title == "Getting Great Guitar Sounds", (
        f"a book-only question retrieved from {top.source_title!r} instead"
    )

    # The citation must be ATTRIBUTABLE: chunk -> page.
    assert top.page_id is not None, "the cited chunk has no page — we cannot show him anything"
    assert top.page is not None, "the hit carries no page NUMBER to display"

    page = app_db.get(Page, top.page_id)
    assert page is not None
    assert page.page_no == top.page

    # ...and the page must have a SCAN. This is what makes the citation
    # verifiable rather than a claim: one click shows him the actual page.
    assert page.image_path, "the cited page has no scan to show — citation is unverifiable"
    print(f"CITED PAGE: p.{page.page_no}  scan={page.image_path}  (GET /media/pages/{page.id}.jpg)")

    # The page's own transcript must actually contain the chunk we retrieved —
    # i.e. the citation points at the page the text really came from, rather
    # than at some arbitrary page id that merely happens to exist.
    assert page.text and top.text[:80] in page.text, (
        "the retrieved chunk is not on the page it cites — the citation is WRONG"
    )
