"""Quote-based resolution — pure CPU, no LLM, no recompile. Every property here is
a fabrication guard or a folio-drift kill, pinned on synthetic pages so it costs
nothing to prove.
"""
from app.brain.ocr import FIGURE_MARKER, FIGURE_END
from app.canon.resolve import (
    RESOLVE_THRESHOLD, _PageText, _norm, build_page_index, resolve_anchor,
)


def _idx(pages: dict[int, str]) -> list[_PageText]:
    """A resolution index straight from {page_no: raw page text}, running the real
    figure-stripping + normalization the DB path uses."""
    from app.brain.ocr import book_text
    return [_PageText(page_no=n, text=_norm(book_text(t)))
            for n, t in sorted(pages.items()) if _norm(book_text(t))]


_P86 = ("Wiring Options. Though the coils of wire and magnets are the primary "
        "components of a pickup, the way they are wired together shapes the tone "
        "you hear more than any single part.")


def test_exact_match_resolves_the_physical_page():
    idx = _idx({85: "Some earlier prose about capacitors and tone caps.", 86: _P86})
    # the [p.86] marker is physical 86 even though the scan printed "62" on it
    page = resolve_anchor("the way they are wired together shapes the tone you hear", idx)
    assert page == 86


def test_whitespace_and_punctuation_noise_still_resolves():
    idx = _idx({86: _P86})
    assert resolve_anchor("the way they are   wired together, shapes the tone!!", idx) == 86


def test_case_noise_still_resolves():
    idx = _idx({86: _P86})
    assert resolve_anchor("THE WAY THEY ARE WIRED TOGETHER SHAPES THE TONE", idx) == 86


def test_anchor_spanning_a_page_boundary_resolves_to_its_start_page():
    # the quote begins on p.101 and finishes on p.102
    idx = _idx({
        101: "The neck relief you set at the first fret determines how the string",
        102: "clears the frets along its whole length, and a truss rod adjusts it.",
    })
    page = resolve_anchor(
        "determines how the string clears the frets along its whole length", idx)
    assert page == 101          # attributed to the page the quote STARTS on


def test_a_hallucinated_quote_matches_nothing_and_is_dropped():
    idx = _idx({86: _P86})
    assert resolve_anchor(
        "left-hand vibrato widens gradually as the phrase resolves upward", idx) is None


def test_a_quote_only_inside_a_figure_region_never_resolves():
    # the ONLY place these words appear is our own [FIGURE] description
    raw = (f"Fig 4. {FIGURE_MARKER}\nA wiring diagram: the tone capacitor bridges "
           f"the volume pot's third lug to ground.\n{FIGURE_END}\nSee above.")
    idx = _idx({40: raw})
    page = resolve_anchor(
        "the tone capacitor bridges the volume pot's third lug to ground", idx)
    assert page is None         # author-only index — a figure quote resolves to no page


def test_below_threshold_does_not_resolve():
    idx = _idx({86: _P86})
    # a partial, garbled overlap that scores under RESOLVE_THRESHOLD
    weak = resolve_anchor("wired shapes hear part single more than tone components", idx)
    assert weak is None
    assert RESOLVE_THRESHOLD == 88.0


# ---------------------------------------------------------------------------
# Re-resolve over STORED anchors — no LLM, no recompile (Task 5)
# ---------------------------------------------------------------------------

def test_reresolve_updates_pages_from_stored_anchors_without_any_model(db):
    from app.canon.resolve import reresolve_source
    from app.models.canon import Concept, ConceptClaim
    from app.models.knowledge import KnowledgeSource, Page

    src = KnowledgeSource(type="pdf", title="Reresolve Book", status="ready")
    db.add(src); db.flush()
    db.add(Page(source_id=src.id, page_no=52, status="ready",
                text=("The Lacey Act makes it unlawful to trade in wood harvested "
                      "in violation of another country's laws.")))
    concept = Concept(key="legal-wood", label_en="Legal wood")
    db.add(concept); db.flush()
    # a claim whose stored `pages` is wrong (folio offset) but whose anchor is right
    author = ConceptClaim(concept_id=concept.id, source_id=src.id,
                          text="Illegally sourced wood is unlawful to trade.",
                          pages=[28], grounding="author",
                          anchor="unlawful to trade in wood harvested in violation")
    # a figure claim with no anchor — must be LEFT ALONE
    figure = ConceptClaim(concept_id=concept.id, source_id=src.id,
                          text="See the map figure.", pages=[7],
                          grounding="figure", anchor=None)
    db.add_all([author, figure]); db.commit()

    summary = reresolve_source(db, src.id)
    db.refresh(author); db.refresh(figure)
    assert author.pages == [52]         # corrected from the anchor, no model call
    assert figure.pages == [7]          # untouched (no anchor)
    assert (summary.total, summary.resolved, summary.dropped) == (1, 1, 0)
