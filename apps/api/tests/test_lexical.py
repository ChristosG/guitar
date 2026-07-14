"""The BM25 arm: the gear-name tokenizer, the Greek stemmer, and the index.

Pure-function tests where possible (`tokenize` needs no DB), plus a small
in-memory corpus for the index itself. The claims pinned here are the ones the
tokenizer was BUILT for, and every one of them is a way retrieval silently breaks
if someone later "tidies" the tokenizer into a stock one.
"""
import pytest

from app.brain.lexical import LexicalIndex, _build, tokenize


# --- the gear-name tokenizer ------------------------------------------------

def test_hyphenated_model_number_emits_whole_form_and_segments():
    """`ts-808` must yield the exact form AND its parts, or a chunk printing
    `TS-808` shares no term with a tutor who typed `TS 808`."""
    assert set(tokenize("TS-808")) == {"ts-808", "ts", "808"}


def test_the_three_spellings_of_one_pedal_share_terms():
    """THE POINT OF THE WHOLE TOKENIZER. A tutor writes the same pedal three ways
    and his book prints a fourth; a stock tokenizer makes `ts808` and `ts-808` two
    unrelated tokens with nothing in common."""
    a, b, c = set(tokenize("TS-808")), set(tokenize("TS808")), set(tokenize("TS 808"))
    shared = a & b & c
    assert {"ts", "808"} <= shared, f"spellings share only {shared}"


def test_a_token_containing_a_digit_is_never_stemmed():
    """Snowball is a linguistic stemmer. Handed `5150` (an amp) it is being asked a
    question it was not built for, in an index whose job here is exact matching."""
    assert "5150" in tokenize("5150")
    assert tokenize("5150") == ["5150"]


def test_greek_is_folded_and_stemmed_to_a_common_root():
    """`κιθάρας` must match `κιθάρα`. Folding alone does not do it (the endings
    still differ); stemming alone does not either (Snowball's Greek algorithm is
    specified over unaccented lowercase input). Both steps, in order."""
    assert tokenize("κιθάρας") == tokenize("κιθάρα")
    assert tokenize("ΚΙΘΑΡΑΣ") == tokenize("κιθάρα")


def test_english_inflections_stem_together():
    assert tokenize("pickups") == tokenize("pickup")


def test_single_letters_are_dropped_but_lone_numbers_are_not():
    assert "a" not in tokenize("a pickup")
    assert "808" in tokenize("808")


# --- the index ---------------------------------------------------------------

_CORPUS = [
    ("c1", "The Ibanez TS-808 Tube Screamer is the archetypal overdrive pedal."),
    ("c2", "A humbucker pickup cancels mains hum by combining two coils."),
    ("c3", "Guitar tone starts in the fingers, then the guitar, then the amp."),
    ("c4", "The 5150 amplifier was built for high-gain guitar tone."),
]


def _index() -> LexicalIndex:
    return _build([cid for cid, _ in _CORPUS], [t for _, t in _CORPUS], fingerprint=(4, None))


@pytest.mark.parametrize("query", ["TS-808", "TS808", "TS 808", "tube screamer"])
def test_every_spelling_retrieves_the_ts808_chunk_first(query):
    """The claim `scripts/retrieval_baseline.py` checks against the real book,
    pinned here on a corpus small enough to reason about."""
    hits = _index().search(query, k=4)
    assert hits, f"{query!r} retrieved nothing"
    assert hits[0][0] == "c1"


def test_bm25_prefers_the_chunk_that_literally_says_the_word():
    hits = _index().search("5150", k=4)
    assert [cid for cid, _ in hits] == ["c4"]


def test_a_term_absent_from_the_corpus_scores_nothing():
    assert _index().search("tablature notation", k=4) == []


def test_distinctive_includes_terms_the_corpus_has_never_seen():
    """THE GAP-DETECTION MECHANISM (see `retrieve._passes_floor`). A term with
    df=0 is the strongest possible evidence the library does not cover a topic —
    dropping it (the obvious implementation) is what let `reading guitar tablature
    notation` pass on the strength of matching the word "read"."""
    index = _index()
    distinctive = index.distinctive("tablature notation")
    assert "tablatur" in distinctive
    assert "notat" in distinctive
    # ...and nothing in the corpus can ever match them, so no passage can clear
    # the lexical arm of the floor for this query.
    for cid, _ in _CORPUS:
        assert not (distinctive <= index.matched_terms("tablature notation", cid))


def test_distinctive_excludes_a_near_universal_term():
    """`guitar` is in 3 of these 4 chunks (278 of 408 in the real library).
    Matching it tells you nothing, so it must not be able to admit a passage."""
    assert "guitar" not in _index().distinctive("guitar tone")


def test_idf_is_never_negative():
    """The classic BM25 IDF goes negative above 50% document frequency, letting a
    common term SUBTRACT from a score — harmless in a web index, actively wrong in
    a 408-chunk corpus where `guitar` would push the guitar chunks down."""
    index = _index()
    assert index.idf("guitar") >= 0.0
    assert index.idf("5150") > index.idf("guitar")


def test_matched_terms_reports_only_terms_the_chunk_actually_contains():
    index = _index()
    assert "screamer" in index.matched_terms("tube screamer overdrive", "c1")
    assert index.matched_terms("tube screamer", "c2") == set()
