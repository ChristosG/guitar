"""`find_lesson` must match a Greek title typed WITHOUT accents (Plan 13, 4.7).

A LIVE BUG, not a hypothetical. `_find_lesson` used `Block.title.ilike('%q%')`,
and Postgres's ILIKE is accent-SENSITIVE — it lowercases, it does not fold. The
tutor's lessons are titled in Greek (`Τονικότητα και Κλίμακες`); people type Greek
without accents (`τονικοτητα`); ILIKE matched none of it. The model then went back
to GUESSING session uuids, which is the exact failure `find_lesson` was built to
end.
"""
from app.agent.tools import TOOLS
from app.models.block import Block


def _lesson(db, title: str) -> Block:
    block = Block(kind="lesson", title=title, order=0)
    db.add(block)
    db.commit()
    return block


def _find(db, query: str) -> list[dict]:
    return TOOLS["find_lesson"].fn(db, title_query=query)


def test_unaccented_greek_query_finds_the_accented_lesson(db):
    """THE BUG. `ILIKE '%τονικοτητα%'` never matched `Τονικότητα` — zero hits."""
    lesson = _lesson(db, "Τονικότητα και Κλίμακες")
    assert [r["id"] for r in _find(db, "τονικοτητα")] == [lesson.id]


def test_accented_query_still_finds_it(db):
    lesson = _lesson(db, "Τονικότητα και Κλίμακες")
    assert [r["id"] for r in _find(db, "Τονικότητα")] == [lesson.id]


def test_final_sigma_does_not_break_the_match(db):
    """`ς` and `σ` are the same letter in different positions, and `.casefold()`
    alone does not unify them — so every Greek word ending in -s would fail to
    match its own stem. `fold()` handles it; this pins that it is wired in here."""
    lesson = _lesson(db, "Κλίμακες και Αρμονία")
    assert [r["id"] for r in _find(db, "κλιμακες")] == [lesson.id]


def test_case_insensitive_english_still_works(db):
    """The old behaviour must not regress: the fix is fold(), which is a SUPERSET
    of ILIKE's case-insensitivity, not a replacement for it."""
    lesson = _lesson(db, "Power Chords and Palm Muting")
    assert [r["id"] for r in _find(db, "power chords")] == [lesson.id]


def test_a_non_matching_query_returns_nothing(db):
    """Folding must not turn the filter into a no-op that returns every lesson."""
    _lesson(db, "Τονικότητα και Κλίμακες")
    assert _find(db, "μπλουζ") == []


def test_search_knowledge_no_longer_accepts_a_language_filter():
    """The model chose this parameter's value. Under the Greek default locale it
    passed `language="el"` and filtered the tutor's ENGLISH library to zero hits,
    then answered from pretrained memory. The schema is the only thing that stops
    it — a model cannot pass a parameter it was never shown.
    """
    params = TOOLS["search_knowledge"].schema["function"]["parameters"]["properties"]
    assert "language" not in params
    assert "domain" not in params
    assert set(params) == {"query", "k"}
