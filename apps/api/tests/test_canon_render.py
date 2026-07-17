"""Pass 3 — the canon block, and the divergences (Part B, Task C4).

THE DIVERGENCE TEST IN THIS FILE IS THE TEST THAT PROVES THE FEATURE EXISTS.

    "10 books all of them talking for guitar TONE, with much information
     repeated, but also some unique perspectives from each writer. So whats the
     plan there to create the ultimate curriculum, combining the knowledge of the
     10 books all together?"

Ten tone books are ~80% the same content. Keyed by concept that redundancy
collapses to nothing, and what SURVIVES is where the authors disagree. A canon
that silently averages Hunter and Gallagher into consensus mush has failed the
request even if every other test in this file passes — so
`test_two_books_that_disagree_render_a_divergence_naming_both_with_citations`
is not one test among many. It is the feature.

The other three properties, each of which has a way of failing silently:

  1. THE CANON GROWS WITH CONCEPTS, NOT PAGES. A second book covering the same
     ground must not double it, or this does not reach ten books.
  2. `page_index` / `ref_to_source_id` ARE `draft.py`'S CONTRACT. Asserted by
     handing a real `CanonContext` to `draft.py`'s REAL validator — not by
     comparing field names, which is a test that passes while the thing is broken.
  3. OUR DESCRIPTION OF A PHOTO IS NOT THE AUTHOR'S SENTENCE. A figure-grounded
     claim rendered as the author's words is a fabricated citation with a real
     page number on it.
"""
import uuid

import pytest

import app.canon.render as render_mod
from app.canon.render import CanonContext, build_canon_context
from app.curriculum.corpus import LibraryContext
from app.curriculum.depth import SECTIONS
from app.curriculum.draft import invalid_citations, strip_invalid_citations
from app.models.canon import Concept, ConceptAlias, ConceptClaim
from app.models.knowledge import KnowledgeSource, Page


@pytest.fixture(autouse=True)
def _no_live_count_tokens(monkeypatch):
    """`_count_tokens` reaches for the provider. Pin the estimate so a canon's
    size is a pure function of its text — every size assertion below is about
    the RENDER, not about a tokenizer."""
    monkeypatch.setattr(render_mod, "get_provider", _boom)


def _boom():
    raise RuntimeError("no provider in a unit test")


def _source(db, title, *, pages=(1, 2, 3)):
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
           grounding="author", alias=None):
    db.add(ConceptClaim(concept_id=concept.id, source_id=source.id, text=text,
                        pages=list(pages), stance=stance, depth=depth,
                        grounding=grounding))
    existing = db.query(ConceptAlias).filter_by(
        concept_id=concept.id, source_id=source.id).one_or_none()
    if existing is None:
        db.add(ConceptAlias(concept_id=concept.id, source_id=source.id,
                            alias=alias or concept.label_en))
    db.flush()


# ---------------------------------------------------------------------------
# THE TEST. Everything else in this file is in service of this one.
# ---------------------------------------------------------------------------

def test_two_books_that_disagree_render_a_divergence_naming_both_with_citations(db):
    """THE PRODUCT. Two authors contradict each other on one concept; the canon
    must put them side by side, name both, and cite both.

    This is the thing a 900K-token full-context prompt structurally cannot do —
    not because a model could not tell that Hunter and Gallagher disagree, but
    because nothing puts their two sentences next to each other and asks. Here
    they are 40 characters apart.
    """
    hunter = _source(db, "Tone Manual (Hunter)", pages=range(1, 200))
    gallagher = _source(db, "Guitar Tone (Gallagher)", pages=range(1, 300))
    concept = _concept(db, "pickup-height", "Pickup height", "Ύψος μαγνήτη")

    _claim(db, concept, hunter, "Lowering the pickup kills the string's sustain",
           pages=[113], stance="warns a low pickup kills sustain")
    _claim(db, concept, gallagher, "A lower treble-side pickup is the fix for harshness",
           pages=[201], stance="prefers a lower treble-side height")
    db.commit()

    canon = build_canon_context(db, None)

    block = _concept_block(canon.text, "pickup-height")
    assert "divergence:" in block, (
        "two authors contradicting each other rendered NO divergence — the canon "
        f"averaged them into consensus mush:\n{block}"
    )
    divergence = _line_group(block, "divergence:")

    # BOTH authors named. A divergence that names one side is not a divergence.
    assert "Tone Manual (Hunter)" in divergence
    assert "Guitar Tone (Gallagher)" in divergence
    # BOTH positions, in each author's own emphasis.
    assert "warns a low pickup kills sustain" in divergence
    assert "prefers a lower treble-side height" in divergence
    # BOTH citations, each resolving to that author's real page.
    hunter_ref = _ref_of(canon, hunter.id)
    gallagher_ref = _ref_of(canon, gallagher.id)
    assert f"({hunter_ref} p.113)" in divergence
    assert f"({gallagher_ref} p.201)" in divergence
    # ...and they are rendered AGAINST each other, not merely listed.
    assert " vs " in divergence or divergence.count("\n") >= 1

    # The disagreement must NOT have been laundered into a consensus line.
    assert "consensus:" not in block, (
        f"two contradicting authors produced a CONSENSUS line:\n{block}"
    )


def test_a_lone_position_against_a_consensus_is_still_a_divergence(db):
    """"7 of 9 agree" is only interesting because of the 2 who do not. The book
    that departs from the consensus is the divergence — it does not need a second
    dissenter to become one."""
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

    block = _concept_block(build_canon_context(db, None).text, "tone-source")
    assert "consensus:" in block          # A and B agree, verbatim
    assert "divergence:" in block         # ...and C does not
    assert "insists tone is in the wood" in _line_group(block, "divergence:")
    assert "Book C" in _line_group(block, "divergence:")


def test_the_one_book_that_covers_a_concept_is_not_called_a_divergence(db):
    """A unique take IS the product — "some unique perspectives from each writer"
    — but there is nobody to disagree with, and labelling it `divergence` would be
    a lie. It renders, honestly, as its own thing. What it must never do is
    vanish."""
    powers = _source(db, "Guitar Exercises (Powers)", pages=range(1, 10))
    concept = _concept(db, "daily-practice", "Philosophy of daily short practice")
    _claim(db, concept, powers, "Ten minutes daily beats two hours weekly",
           pages=[4], stance="short daily practice over long sessions")
    db.commit()

    block = _concept_block(build_canon_context(db, None).text, "daily-practice")
    assert "divergence:" not in block
    assert "consensus:" not in block
    assert "short daily practice over long sessions" in block   # never dropped
    assert "coverage:   1/1 books" in block


# ---------------------------------------------------------------------------
# `draft.py`'s contract — asserted against `draft.py`, not against a field list
# ---------------------------------------------------------------------------

def test_page_index_and_ref_to_source_id_satisfy_drafts_real_citation_validator(db):
    """The tutor CLICKS these chips. `draft.py` is the thing that decides whether
    a citation is real, so it is `draft.py` that must accept a `CanonContext` —
    and reject a page the canon never showed."""
    hunter = _source(db, "Tone Manual (Hunter)", pages=range(1, 200))
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, hunter, "Colder bias tightens the low end", pages=[57],
           stance="prefers a colder bias")
    db.commit()

    canon = build_canon_context(db, None)
    ref = _ref_of(canon, hunter.id)

    # A REAL section name, off `depth.SECTIONS` itself: `invalid_citations` only
    # walks sections it knows, so a made-up one makes every assertion here pass
    # vacuously — against an empty citation list, which is not the thing under
    # test.
    section = SECTIONS[0]

    # The real validator, on a real CanonContext. No adapter, no shim.
    good = {section: {"citations": [{"source_id": ref, "page": 57}]}}
    assert invalid_citations(good, canon) == []

    # p.58 is a real page of a real book and the canon never showed it. It is
    # therefore a fabrication HERE, exactly as `corpus.MIN_PAGE_CHARS`-dropped
    # pages are fabrications there.
    bad = {section: {"citations": [{"source_id": ref, "page": 58}]}}
    assert invalid_citations(bad, canon) == [(section, ref, 58)]
    stripped = strip_invalid_citations(bad, canon)
    assert stripped[section]["citations"] == []

    # A ref for a book that is not in this canon at all.
    ghost = {section: {"citations": [{"source_id": "S99", "page": 57}]}}
    assert invalid_citations(ghost, canon) == [(section, "S99", 57)]

    # `ref_to_source_id` is what turns a validated "S1" back into something the
    # Reader can deep-link to (`draft.py:534`).
    assert canon.ref_to_source_id[ref] == hunter.id


def test_canon_context_mirrors_library_context_field_for_field(db):
    """C5 SWAPS one for the other inside `corpus.prefix_messages`. A field that
    exists on `LibraryContext` and not here is a swap that fails at runtime, in a
    background job, with nobody watching."""
    for field in ("text", "token_count", "fits", "sources", "page_index",
                  "ref_to_source_id"):
        assert hasattr(CanonContext(text="", token_count=0, fits=True), field), field
    assert hasattr(LibraryContext(text="", token_count=0, fits=True), "is_empty")
    assert CanonContext(text="", token_count=0, fits=True).is_empty is True
    assert "summary" in dir(CanonContext)


def test_sources_carries_the_keys_draft_py_reads_off_it(db):
    """`draft.py:537` does `s["title"] for s in library.sources if s["ref"] == ...`
    to name the book on a citation chip. A missing key is a KeyError at draft
    time."""
    hunter = _source(db, "Tone Manual (Hunter)")
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, hunter, "Colder bias tightens the low end", pages=[1])
    db.commit()

    canon = build_canon_context(db, None)
    assert canon.sources
    for entry in canon.sources:
        for key in ("ref", "id", "title", "pages", "chars"):
            assert key in entry, key
    assert canon.sources[0]["title"] == "Tone Manual (Hunter)"


# ---------------------------------------------------------------------------
# The property that makes this reach ten books
# ---------------------------------------------------------------------------

def test_a_second_book_covering_the_same_concepts_does_not_double_the_canon(db):
    """THE SCALING PROPERTY. The library doubles when a book is added — 2 books
    are 2x the pages, verbatim. The canon must not: the concept scaffold is paid
    once, and a book that agrees collapses to a citation."""
    a = _source(db, "Book A")
    concepts = [_concept(db, f"concept-{i}", f"Concept number {i}") for i in range(10)]
    for concept in concepts:
        _claim(db, concept, a, f"What book A says about {concept.key}", pages=[1],
               stance="the standard position")
    db.commit()
    one_book = len(build_canon_context(db, None).text)

    b = _source(db, "Book B")
    for concept in concepts:
        # SAME concepts, same position, different book. This is the 80% overlap.
        _claim(db, concept, b, f"What book B says about {concept.key}", pages=[2],
               stance="the standard position")
    db.commit()
    two_books = len(build_canon_context(db, None).text)

    assert two_books < 2 * one_book, (
        f"a second book covering the SAME concepts nearly doubled the canon "
        f"({one_book} -> {two_books} chars) — this does not reach ten books"
    )
    # It agreed, verbatim, so it should have cost about a citation.
    assert two_books < one_book * 1.4, (
        f"a book that AGREES cost {two_books - one_book} chars — the redundancy "
        f"did not collapse; ten tone books are ~80% this case"
    )


def test_the_canon_does_not_grow_with_pages(db):
    """"Grows with CONCEPTS, not pages" — literally. A book with ten times the
    pages and the same ledger is the same canon. Pages enter only as citations."""
    small = _source(db, "Book", pages=range(1, 6))
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, small, "Colder bias tightens the low end", pages=[3])
    db.commit()
    before = len(build_canon_context(db, None).text)

    for page_no in range(6, 400):
        db.add(Page(source_id=small.id, page_no=page_no, text=f"page {page_no}",
                    status="ready"))
    db.commit()
    after = len(build_canon_context(db, None).text)

    assert after == before, (
        f"394 pages of book changed the canon by {after - before} chars — the "
        f"canon is reading pages, not concepts"
    )


# ---------------------------------------------------------------------------
# The [FIGURE] contract, at the far end of the pipeline
# ---------------------------------------------------------------------------

def test_a_figure_grounded_claim_is_never_rendered_as_the_authors_words(db):
    """`brain/ocr.py:38-52`: text inside a [FIGURE] region is OURS. C2 recorded
    that per claim. This is the LAST place it can be lost — and losing it here
    means the canon hands the drafting model our description of a photo as
    Hunter's sentence, which it then quotes, with a real page number on it."""
    hunter = _source(db, "Tone Manual (Hunter)", pages=range(1, 100))
    concept = _concept(db, "amp-controls", "Amp control layout")
    _claim(db, concept, hunter, "The photo shows the bias test points behind the tubes",
           pages=[57], stance="bias points are behind the tubes", grounding="figure")
    db.commit()

    canon = build_canon_context(db, None)
    block = _concept_block(canon.text, "amp-controls")
    ref = _ref_of(canon, hunter.id)

    assert f"({ref} p.57 FIGURE)" in block, (
        "a figure-grounded claim rendered as an ordinary citation — nothing "
        f"downstream can now tell it is OUR description of a photo:\n{block}"
    )
    # The legend must be present, or the marker means nothing to the reader.
    assert "FIGURE" in canon.text.split("CONCEPT:")[0], (
        "the FIGURE marker is rendered but never explained — the model has no "
        "way to know it must not quote it"
    )


def test_an_author_grounded_claim_is_not_marked_as_ours(db):
    """The converse, and it is not symmetric decoration: marking the author's own
    prose as ours would quietly demote every real quotation in the canon to
    unquotable, and the lessons would stop quoting his books."""
    hunter = _source(db, "Tone Manual (Hunter)")
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, hunter, "Colder bias tightens the low end", pages=[1],
           grounding="author")
    db.commit()

    canon = build_canon_context(db, None)
    ref = _ref_of(canon, hunter.id)
    assert f"({ref} p.1)" in canon.text
    assert "FIGURE" not in _concept_block(canon.text, "bias")


# ---------------------------------------------------------------------------
# Shape, stability, honesty
# ---------------------------------------------------------------------------

def test_the_canon_is_byte_identical_across_two_builds(db):
    """It goes in the CACHED prefix. `corpus.py`: get this wrong and nothing
    breaks — the curriculum still generates, it just costs ten times as much, and
    the only symptom is an invoice a month later."""
    a = _source(db, "Book A")
    b = _source(db, "Book B")
    for i in range(6):
        concept = _concept(db, f"c-{i}", f"Concept {i}")
        _claim(db, concept, a, "A's take", pages=[1], stance=f"a-stance-{i}")
        _claim(db, concept, b, "B's take", pages=[2], stance=f"b-stance-{i}")
    db.commit()

    assert build_canon_context(db, None).text == build_canon_context(db, None).text


def test_coverage_counts_the_books_in_this_canon(db):
    a = _source(db, "Book A")
    b = _source(db, "Book B")
    c = _source(db, "Book C")
    covered = _concept(db, "shared", "A shared concept")
    for src in (a, b):
        _claim(db, covered, src, "take", pages=[1], stance=f"stance-{src.title}")
    lonely = _concept(db, "lonely", "Only C knows this")
    _claim(db, lonely, c, "take", pages=[1], stance="c-only")
    db.commit()

    text = build_canon_context(db, None).text
    assert "coverage:   2/3 books" in _concept_block(text, "shared")
    assert "coverage:   1/3 books" in _concept_block(text, "lonely")


def test_depth_names_the_book_that_goes_deepest_and_its_real_pages(db):
    """"S5 pp.110-118 goes deepest" — this is what sends a lesson to the book that
    actually teaches the concept rather than the one that name-checks it."""
    deep = _source(db, "The Deep Book", pages=range(1, 200))
    shallow = _source(db, "The Shallow Book", pages=range(1, 200))
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, deep, "chapter on bias", pages=[110, 111, 112],
           stance="deep-take", depth="primary")
    _claim(db, concept, deep, "more on bias", pages=[118], stance="deep-take-2",
           depth="primary")
    _claim(db, concept, shallow, "mentions bias", pages=[7], stance="shallow-take",
           depth="mention")
    db.commit()

    canon = build_canon_context(db, None)
    block = _concept_block(canon.text, "bias")
    deep_ref = _ref_of(canon, deep.id)
    assert f"depth:      {deep_ref} pp.110-112, 118 goes deepest" in block, block


def test_a_page_the_canon_never_rendered_is_not_in_the_page_index(db):
    """`page_index` is "what the model was SHOWN" — the same contract as
    `corpus.LibraryContext.page_index`, and deliberately not "the pages in the
    database"."""
    hunter = _source(db, "Tone Manual (Hunter)", pages=range(1, 200))
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, hunter, "Colder bias tightens the low end", pages=[57])
    db.commit()

    canon = build_canon_context(db, None)
    assert canon.page_index[_ref_of(canon, hunter.id)] == {57}


def test_an_empty_canon_is_empty_rather_than_a_block_saying_nothing(db):
    """`prefix_messages` branches on `is_empty` to tell the model "you read
    nothing of his — tier honestly". A canon that renders an empty shell instead
    would defeat that."""
    _source(db, "A book nobody compiled")
    db.commit()
    canon = build_canon_context(db, None)
    assert canon.is_empty is True
    assert canon.text == ""
    assert canon.sources == []


def test_selecting_no_sources_is_a_deliberate_answer_and_is_respected(db):
    a = _source(db, "Book A")
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, a, "take", pages=[1])
    db.commit()
    assert build_canon_context(db, []).is_empty is True
    assert build_canon_context(db, [a.id]).is_empty is False


def test_a_book_outside_the_selection_is_not_in_the_canon(db):
    a = _source(db, "Selected")
    b = _source(db, "Not selected")
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, a, "A's take", pages=[1], stance="a-stance")
    _claim(db, concept, b, "B's take", pages=[2], stance="b-stance")
    db.commit()

    canon = build_canon_context(db, [a.id])
    assert "b-stance" not in canon.text
    assert "Not selected" not in canon.text
    assert "coverage:   1/1 books" in _concept_block(canon.text, "bias")


def test_the_greek_label_is_rendered_because_the_tutor_reads_the_canon(db):
    a = _source(db, "Book A")
    concept = _concept(db, "bias", "Tube amp bias", "Πόλωση λυχνιών")
    _claim(db, concept, a, "take", pages=[1])
    db.commit()
    assert "Πόλωση λυχνιών" in build_canon_context(db, None).text


def test_fits_is_measured_against_the_same_budget_the_library_uses(db, monkeypatch):
    a = _source(db, "Book A")
    concept = _concept(db, "bias", "Tube amp bias")
    _claim(db, concept, a, "take", pages=[1])
    db.commit()
    monkeypatch.setattr(render_mod.settings, "full_context_budget", 1)
    assert build_canon_context(db, None).fits is False
    monkeypatch.setattr(render_mod.settings, "full_context_budget", 10_000_000)
    assert build_canon_context(db, None).fits is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _concept_block(text: str, key: str) -> str:
    """The rendered block for one concept, so an assertion cannot accidentally
    match a line belonging to a different concept."""
    blocks = text.split("CONCEPT: ")
    for block in blocks[1:]:
        if block.split("\n", 1)[0].strip() == key:
            return "CONCEPT: " + block.split("\nCONCEPT: ")[0]
    raise AssertionError(f"no CONCEPT block for {key!r} in:\n{text}")


def _line_group(block: str, label: str) -> str:
    """A labelled line plus its `vs`/continuation lines."""
    out, collecting = [], False
    for line in block.split("\n"):
        if line.strip().startswith(label):
            collecting = True
            out.append(line)
            continue
        if collecting:
            if line.startswith(" ") and not _is_label(line):
                out.append(line)
            else:
                break
    assert out, f"no {label!r} line in:\n{block}"
    return "\n".join(out)


def _is_label(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(f"{k}:") for k in
               ("consensus", "divergence", "coverage", "depth", "name", "only in"))


def _ref_of(canon: CanonContext, source_id: uuid.UUID) -> str:
    for ref, sid in canon.ref_to_source_id.items():
        if sid == source_id:
            return ref
    raise AssertionError(f"{source_id} has no ref in {canon.ref_to_source_id}")
