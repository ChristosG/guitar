"""C7 — the concept canon, BROWSABLE. The tutor can finally SEE it.

Chris, three times: *"that would also be nice to see somewhere in the library, i
mean the canon generations"* / *"i dont see any canon component, or text
anywhere."* C8 made concepts searchable; C7 makes them browsable and puts a
compile status on every Library row.

THE HEADLINE TEST HERE is that DIVERGENCE sorts first and renders as its own
positions — a browse view that buried the disagreements under a wall of consensus
rows would have missed the entire point of owning ten books (see
`canon/render.py`). These tests seed the canon directly via ORM rows (same shape
as `test_canon_search.py`/`test_canon_render.py`) — no live model.
"""
import uuid

from app.canon.browse import list_concepts
from app.models.canon import BookCompile, Concept, ConceptClaim
from app.models.knowledge import KnowledgeSource, Page


# ---------------------------------------------------------------------------
# Seeding helpers — the same shape test_canon_search.py uses
# ---------------------------------------------------------------------------

def _source(db, title, *, pages=range(1, 300)):
    source = KnowledgeSource(type="pdf", title=title, status="ready")
    db.add(source)
    db.flush()
    for page_no in pages:
        db.add(Page(source_id=source.id, page_no=page_no,
                    text=f"page {page_no} of {title}", status="ready"))
    db.flush()
    return source


def _concept(db, key, label_en, label_el=None):
    concept = Concept(key=key, label_en=label_en, label_el=label_el)
    db.add(concept)
    db.flush()
    return concept


def _claim(db, concept, source, text, *, pages, stance=None, depth="primary",
           grounding="author"):
    db.add(ConceptClaim(concept_id=concept.id, source_id=source.id, text=text,
                        pages=list(pages), stance=stance, depth=depth,
                        grounding=grounding))
    db.flush()


def _hit_for(hits, key):
    return next((h for h in hits if h.key == key), None)


# ---------------------------------------------------------------------------
# It is a render of the whole ledger
# ---------------------------------------------------------------------------

def test_browse_lists_every_compiled_concept(db):
    book = _source(db, "Tone Manual (Hunter)")
    a = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, a, book, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    b = _concept(db, "amp-bias", "Amplifier bias")
    _claim(db, b, book, "A colder bias tightens the low end", pages=[57],
           stance="prefers a colder bias")
    db.commit()

    hits = list_concepts(db)
    assert {h.key for h in hits} == {"pickup-height", "amp-bias"}


def test_browse_drops_a_concept_with_no_claims(db):
    """A concept with no claim is a NAME with no book behind it (a reconciliation
    artefact). The browse view renders what the books SAY, so it is dropped."""
    book = _source(db, "Book A")
    real = _concept(db, "vibrato", "Vibrato")
    _claim(db, real, book, "A controlled pitch wobble", pages=[10])
    _concept(db, "orphan", "Orphaned name")  # no claim
    db.commit()

    keys = {h.key for h in list_concepts(db)}
    assert "vibrato" in keys
    assert "orphan" not in keys


# ---------------------------------------------------------------------------
# THE PRODUCT — divergence is the headline, at the list level too
# ---------------------------------------------------------------------------

def test_browse_puts_a_divergence_ahead_of_a_consensus(db):
    """Divergence sorts first. The one thing a shelf of ten books buys is the
    disagreements; a browse view that buried them below the consensus rows would
    hide the payoff."""
    a = _source(db, "Book A")
    b = _source(db, "Book B")

    # A consensus concept (both books say the same words) — heavily covered.
    agree = _concept(db, "string-gauge", "String gauge and tone")
    for src in (a, b):
        _claim(db, agree, src, "Heavier strings give a fuller tone", pages=[5],
               stance="heavier strings, fuller tone")

    # A divergence (the books disagree) — same coverage, MUST still sort first.
    disagree = _concept(db, "pickup-height", "Pickup height")
    _claim(db, disagree, a, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    _claim(db, disagree, b, "A lower treble-side pickup fixes harshness",
           pages=[201], stance="prefers a lower treble-side height")
    db.commit()

    hits = list_concepts(db)
    assert hits[0].key == "pickup-height", "the divergence did not sort first"
    assert hits[0].divergence is True
    assert _hit_for(hits, "string-gauge").divergence is False


def test_browse_divergence_keeps_both_positions_and_reader_deeplink_citations(db):
    """THE FEATURE. Two authors who disagree render as TWO positions, each naming
    its book and carrying its own real page — the data a Reader deep-link needs
    (`source_id` + `pages`), never averaged into one line."""
    hunter = _source(db, "Tone Manual (Hunter)")
    gallagher = _source(db, "Guitar Tone (Gallagher)")
    concept = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, concept, hunter, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    _claim(db, concept, gallagher, "A lower treble-side pickup fixes harshness",
           pages=[201, 202], stance="prefers a lower treble-side height")
    db.commit()

    hit = _hit_for(list_concepts(db), "pickup-height")
    assert hit.divergence is True
    assert hit.coverage == 2
    by_pos = {p.position: p for p in hit.positions}
    hunter_pos = by_pos["warns a low pickup kills sustain"]
    gallagher_pos = by_pos["prefers a lower treble-side height"]
    assert hunter_pos.kind == "divergence"
    assert gallagher_pos.kind == "divergence"

    hcite = hunter_pos.citations[0]
    assert hcite.source_id == hunter.id
    assert hcite.pages == [113]
    assert hcite.pages_label == "p.113"
    gcite = gallagher_pos.citations[0]
    assert gcite.source_id == gallagher.id
    assert gcite.pages == [201, 202]
    assert gcite.pages_label == "pp.201-202"


def test_browse_preserves_figure_grounding(db):
    """The [FIGURE] contract survives to the browse surface: a claim grounded in
    OUR description of a picture is citable, never quotable, so its marker must
    reach the UI."""
    book = _source(db, "Fretboard Workbook")
    concept = _concept(db, "caged", "The CAGED system")
    _claim(db, concept, book, "A neck diagram outlines the C shape", pages=[15],
           stance="five interlocking shapes", grounding="figure")
    db.commit()

    hit = _hit_for(list_concepts(db), "caged")
    assert hit.positions[0].citations[0].grounding == "figure"


# ---------------------------------------------------------------------------
# The browse route — the envelope + counts the landing page shows
# ---------------------------------------------------------------------------

def test_browse_route_returns_concepts_and_counts(client, db):
    hunter = _source(db, "Tone Manual (Hunter)")
    gallagher = _source(db, "Guitar Tone (Gallagher)")
    diverge = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, diverge, hunter, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    _claim(db, diverge, gallagher, "A lower treble-side pickup fixes harshness",
           pages=[201], stance="prefers a lower treble-side height")
    solo = _concept(db, "daily-practice", "Short daily practice")
    _claim(db, solo, hunter, "Ten minutes daily beats two hours weekly", pages=[4],
           stance="short daily practice")
    # Compile ledger rows: one ready, one running.
    db.add(BookCompile(source_id=hunter.id, status="ready", model="claude-sonnet-5",
                       concept_count=2))
    db.add(BookCompile(source_id=gallagher.id, status="running", model="claude-sonnet-5"))
    db.commit()

    r = client.get("/canon/concepts")
    assert r.status_code == 200
    body = r.json()
    assert body["total_concepts"] == 2
    assert body["divergence_count"] == 1
    assert body["books_compiled"] == 1
    assert body["books_compiling"] == 1
    # Divergence first.
    assert body["concepts"][0]["key"] == "pickup-height"
    assert body["concepts"][0]["divergence"] is True
    assert body["concepts"][0]["label_el"] == "Ύψος μαγνήτη"


def test_browse_route_empty_canon_is_200_not_error(client, db):
    """An uncompiled library has an empty canon — a true state, not a failure."""
    r = client.get("/canon/concepts")
    assert r.status_code == 200
    body = r.json()
    assert body["concepts"] == []
    assert body["total_concepts"] == 0
    assert body["divergence_count"] == 0


# ---------------------------------------------------------------------------
# Compile status on the Library row (GET /knowledge/sources)
# ---------------------------------------------------------------------------

def test_source_list_carries_compile_status_when_ready(client, db):
    book = _source(db, "Modern Guitar Rigs (Kahn)", pages=range(1, 5))
    db.add(BookCompile(source_id=book.id, status="ready", model="claude-sonnet-5",
                       concept_count=34))
    db.commit()

    r = client.get("/knowledge/sources")
    assert r.status_code == 200
    row = next(s for s in r.json() if s["id"] == str(book.id))
    assert row["compile"] is not None
    assert row["compile"]["status"] == "ready"
    assert row["compile"]["concept_count"] == 34


def test_source_list_compile_is_null_when_never_compiled(client, db):
    book = _source(db, "A book nobody compiled", pages=range(1, 5))
    db.commit()

    r = client.get("/knowledge/sources")
    row = next(s for s in r.json() if s["id"] == str(book.id))
    assert row["compile"] is None


def test_source_list_carries_running_compile(client, db):
    book = _source(db, "A book being read into the canon now", pages=range(1, 5))
    db.add(BookCompile(source_id=book.id, status="running", model="claude-sonnet-5"))
    db.commit()

    r = client.get("/knowledge/sources")
    row = next(s for s in r.json() if s["id"] == str(book.id))
    assert row["compile"]["status"] == "running"
    assert row["compile"]["concept_count"] is None


def test_source_detail_also_carries_compile_status(client, db):
    book = _source(db, "Detail view book", pages=range(1, 5))
    db.add(BookCompile(source_id=book.id, status="ready", model="claude-sonnet-5",
                       concept_count=16))
    db.commit()

    r = client.get(f"/knowledge/sources/{book.id}")
    assert r.status_code == 200
    assert r.json()["compile"]["concept_count"] == 16
