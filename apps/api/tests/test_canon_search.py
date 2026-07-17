"""C8 — the concept canon, made searchable.

Chris: *"those concepts might be nice to be searchable bro, by the search and
from the chat screen too!"*

THE DIVERGENCE TEST IN THIS FILE IS THE ONE THAT PROVES THE FEATURE. A concept
search that returns a topic but averages two disagreeing authors into one line
has failed the request the same way a consensus-only canon would — the whole
reason this search is worth more than `search_knowledge` is that it can put
Hunter next to Gallagher and say they disagree, with both citations.

BM25, not a new engine and not dense: per the retrieval memory the dense arm is
measured to MISS on this corpus, so the concept index reuses the exact PyStemmer
tokenizer + Okapi BM25 the chunk arm uses (`brain/lexical`). These tests seed the
canon directly via ORM rows (mirrors `test_canon_render.py`) — no live model.
"""
import uuid

import pytest

import app.canon.search as search_mod
from app.canon.search import reset_concept_index, search_concepts
from app.models.canon import Concept, ConceptClaim
from app.models.knowledge import KnowledgeSource, Page


@pytest.fixture(autouse=True)
def _identity_normalize(monkeypatch):
    """The concept search normalises a Greek query to the corpus language the
    same way chunk retrieval does — a live model call. Pin it to identity so
    these tests are hermetic (a Greek query then matches `label_el` directly,
    which is the fallback path when translation is unavailable anyway)."""
    monkeypatch.setattr(search_mod, "normalize_query", lambda db, q: q)


@pytest.fixture(autouse=True)
def _fresh_index():
    """The BM25 index is a process-global; wipe it around each test so a stale
    build from a prior test cannot leak (same hygiene `test_retrieve_hybrid.py`
    applies to the chunk index)."""
    reset_concept_index()
    yield
    reset_concept_index()


# ---------------------------------------------------------------------------
# Seeding helpers — the same shape `test_canon_render.py` uses
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
# It is a search, and it reuses BM25
# ---------------------------------------------------------------------------

def test_a_concept_is_found_by_its_english_label(db):
    book = _source(db, "Tone Manual (Hunter)")
    concept = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, concept, book, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    db.commit()

    hits = search_concepts(db, "pickup height", k=8)
    assert _hit_for(hits, "pickup-height") is not None


def test_a_concept_is_found_by_its_CLAIM_TEXT_not_only_its_label(db):
    """The claim text is indexed, not just the label — otherwise the search is a
    title lookup, not a search of what the books actually say."""
    book = _source(db, "Guitar Tone (Gallagher)")
    concept = _concept(db, "amp-bias", "Amplifier bias")
    _claim(db, concept, book,
           "A colder cathode-bias resistor tightens the low end considerably",
           pages=[57], stance="prefers a colder bias")
    db.commit()

    # "cathode" appears nowhere in the label — only in the claim text.
    hits = search_concepts(db, "cathode resistor", k=8)
    assert _hit_for(hits, "amp-bias") is not None


def test_a_greek_query_finds_a_concept_by_its_greek_label(db):
    """Greek is the product. A concept the tutor named in his own language must
    be findable in it — the Greek stemmer folds `Ύψος μαγνήτη`/`ύψους μαγνήτη`
    to a common root."""
    book = _source(db, "Tone Manual (Hunter)")
    concept = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, concept, book, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    db.commit()

    hits = search_concepts(db, "ύψους μαγνήτη", k=8)
    assert _hit_for(hits, "pickup-height") is not None


def test_a_term_absent_from_the_whole_canon_finds_nothing(db):
    book = _source(db, "Tone Manual (Hunter)")
    concept = _concept(db, "pickup-height", "Pickup height")
    _claim(db, concept, book, "Lowering the pickup kills sustain", pages=[113])
    db.commit()

    assert search_concepts(db, "harmonica reed", k=8) == []


# ---------------------------------------------------------------------------
# THE PRODUCT — divergence
# ---------------------------------------------------------------------------

def test_two_books_that_disagree_surface_BOTH_positions_with_citations(db):
    """THE FEATURE. `search_knowledge` structurally cannot do this — it returns
    chunks from one book and has no notion two books contradict each other. This
    search must name both authors, keep both positions, and cite both.
    """
    hunter = _source(db, "Tone Manual (Hunter)")
    gallagher = _source(db, "Guitar Tone (Gallagher)")
    concept = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, concept, hunter, "Lowering the pickup kills the string's sustain",
           pages=[113], stance="warns a low pickup kills sustain")
    _claim(db, concept, gallagher, "A lower treble-side pickup fixes harshness",
           pages=[201], stance="prefers a lower treble-side height")
    db.commit()

    hit = _hit_for(search_concepts(db, "pickup height", k=8), "pickup-height")
    assert hit is not None
    assert hit.divergence is True, "two contradicting authors were not flagged as a divergence"
    assert hit.coverage == 2

    positions = {p.position for p in hit.positions}
    assert "warns a low pickup kills sustain" in positions
    assert "prefers a lower treble-side height" in positions

    # Both positions are their own entry (not laundered into one consensus line),
    # each naming its own book and carrying its own real page.
    by_pos = {p.position: p for p in hit.positions}
    hunter_pos = by_pos["warns a low pickup kills sustain"]
    gallagher_pos = by_pos["prefers a lower treble-side height"]
    assert hunter_pos.kind == "divergence"
    assert gallagher_pos.kind == "divergence"
    assert hunter_pos.books == ["Tone Manual (Hunter)"]
    assert gallagher_pos.books == ["Guitar Tone (Gallagher)"]

    hunter_cite = hunter_pos.citations[0]
    assert hunter_cite.source_id == hunter.id
    assert hunter_cite.pages == [113]
    gallagher_cite = gallagher_pos.citations[0]
    assert gallagher_cite.source_id == gallagher.id
    assert gallagher_cite.pages == [201]


def test_a_lone_position_against_a_consensus_is_still_a_divergence(db):
    a = _source(db, "Book A")
    b = _source(db, "Book B")
    c = _source(db, "Book C")
    concept = _concept(db, "tone-source", "Where tone comes from")
    for src in (a, b):
        _claim(db, concept, src, "Tone is in the hands", pages=[2],
               stance="insists tone is in the hands")
    _claim(db, concept, c, "The wood decides the tone", pages=[3],
           stance="insists tone is in the wood")
    db.commit()

    hit = _hit_for(search_concepts(db, "where tone comes from", k=8), "tone-source")
    assert hit is not None
    assert hit.divergence is True
    kinds = {p.kind for p in hit.positions}
    assert "consensus" in kinds     # A and B agree, verbatim
    assert "divergence" in kinds    # ...and C does not
    consensus = next(p for p in hit.positions if p.kind == "consensus")
    assert sorted(consensus.books) == ["Book A", "Book B"]
    dissent = next(p for p in hit.positions if p.kind == "divergence")
    assert dissent.books == ["Book C"]


def test_two_books_agreeing_verbatim_are_one_consensus_not_a_divergence(db):
    a = _source(db, "Book A")
    b = _source(db, "Book B")
    concept = _concept(db, "string-gauge", "String gauge and tone")
    for src in (a, b):
        _claim(db, concept, src, "Heavier strings give a fuller tone", pages=[5],
               stance="heavier strings, fuller tone")
    db.commit()

    hit = _hit_for(search_concepts(db, "string gauge", k=8), "string-gauge")
    assert hit is not None
    assert hit.divergence is False
    assert len(hit.positions) == 1
    assert hit.positions[0].kind == "consensus"
    assert sorted(hit.positions[0].books) == ["Book A", "Book B"]


def test_a_single_book_concept_is_only_in_not_a_divergence(db):
    powers = _source(db, "Guitar Exercises (Powers)")
    concept = _concept(db, "daily-practice", "Short daily practice")
    _claim(db, concept, powers, "Ten minutes daily beats two hours weekly",
           pages=[4], stance="short daily practice over long sessions")
    db.commit()

    hit = _hit_for(search_concepts(db, "daily practice", k=8), "daily-practice")
    assert hit is not None
    assert hit.divergence is False
    assert len(hit.positions) == 1
    assert hit.positions[0].kind == "only_in"
    assert hit.coverage == 1


# ---------------------------------------------------------------------------
# Citations — the deep-link into the Reader
# ---------------------------------------------------------------------------

def test_a_citation_carries_source_id_and_pages_for_the_reader_deeplink(db):
    """A concept hit opens the concept; its citations deep-link into the Reader.
    The data for that link is `ConceptClaim.pages` + `source_id`."""
    book = _source(db, "Tone Manual (Hunter)")
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, book, "Colder bias tightens the low end",
           pages=[57, 58], stance="prefers a colder bias")
    db.commit()

    hit = _hit_for(search_concepts(db, "tube amp bias", k=8), "bias")
    cite = hit.positions[0].citations[0]
    assert cite.source_id == book.id
    assert cite.pages == [57, 58]
    assert cite.pages_label == "pp.57-58"


def test_a_figure_grounded_citation_is_marked_never_quotable(db):
    """The [FIGURE] contract, carried through to search: a claim grounded in our
    description of a picture is citable, never quotable, so the marker must
    survive."""
    book = _source(db, "Guitar Fretboard Workbook")
    concept = _concept(db, "caged", "The CAGED system")
    _claim(db, concept, book, "A neck diagram outlines the C shape in grey",
           pages=[15], stance="five interlocking shapes", grounding="figure")
    db.commit()

    hit = _hit_for(search_concepts(db, "CAGED system", k=8), "caged")
    assert hit.positions[0].citations[0].grounding == "figure"


# ---------------------------------------------------------------------------
# Staleness, not invalidation — the index rebuilds when the canon moves
# ---------------------------------------------------------------------------

def test_a_newly_compiled_concept_becomes_searchable_without_a_manual_reset(db):
    """The index self-invalidates on the canon's fingerprint (concept/claim
    counts + newest timestamps), exactly like the chunk index — so a book
    compiled after the first search is still found, with no invalidate() call
    wired into the compile path."""
    book = _source(db, "Book A")
    first = _concept(db, "vibrato", "Vibrato")
    _claim(db, concept=first, source=book, text="Vibrato is a pitch wobble",
           pages=[10], stance="a controlled pitch wobble")
    db.commit()

    # Prime the index on the first search.
    assert _hit_for(search_concepts(db, "vibrato", k=8), "vibrato") is not None
    assert search_concepts(db, "sustain", k=8) == []

    # A second concept enters the canon after the index was already built.
    second = _concept(db, "sustain", "Sustain")
    _claim(db, concept=second, source=book, text="Sustain is how long a note rings",
           pages=[20], stance="length of ring")
    db.commit()

    assert _hit_for(search_concepts(db, "sustain", k=8), "sustain") is not None


# ---------------------------------------------------------------------------
# The library-search route — the API shape C7 wires a UI onto
# ---------------------------------------------------------------------------

def test_concept_search_route_returns_hits_with_divergence_and_citations(client, db):
    """`POST /knowledge/concepts/search`. An English query needs no translation,
    so this drives the real route end to end (no model)."""
    hunter = _source(db, "Tone Manual (Hunter)")
    gallagher = _source(db, "Guitar Tone (Gallagher)")
    concept = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")
    _claim(db, concept, hunter, "Lowering the pickup kills sustain", pages=[113],
           stance="warns a low pickup kills sustain")
    _claim(db, concept, gallagher, "A lower treble-side pickup fixes harshness",
           pages=[201], stance="prefers a lower treble-side height")
    db.commit()

    r = client.post("/knowledge/concepts/search", json={"query": "pickup height"})
    assert r.status_code == 200
    hits = r.json()["hits"]
    hit = next(h for h in hits if h["key"] == "pickup-height")
    assert hit["divergence"] is True
    assert hit["label_el"] == "Ύψος μαγνήτη"
    positions = {p["position"] for p in hit["positions"]}
    assert "warns a low pickup kills sustain" in positions
    assert "prefers a lower treble-side height" in positions
    # A citation deep-links into the Reader: source_id + page number.
    a_cite = hit["positions"][0]["citations"][0]
    assert a_cite["source_id"] in {str(hunter.id), str(gallagher.id)}
    assert a_cite["pages"]


def test_concept_search_route_on_an_empty_canon_is_an_empty_list_not_an_error(client):
    r = client.post("/knowledge/concepts/search", json={"query": "pickup height"})
    assert r.status_code == 200
    assert r.json() == {"hits": []}
