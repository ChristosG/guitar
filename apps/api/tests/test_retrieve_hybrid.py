"""Hybrid retrieval: the floor, the two arms, and the units bug the floor exists
to avoid (Plan 13, Stage 4.4).

These use a FAKE embedder — a deterministic hash-based vector — because what is
under test is the FUSION, the FLOOR and the SCORE PLUMBING, not e5's semantics.
The real-model, real-library numbers are `scripts/retrieval_baseline.py`'s job,
and it asserts the three gate conditions against the tutor's actual 408 chunks.
"""
import hashlib

import pytest

from app.brain import lexical
from app.brain.retrieve import (
    MIN_PASSAGE_CHARS,
    STRONG_COSINE,
    WEAK_COSINE_FLOOR,
    Hit,
    _passes_floor,
    search,
)
from app.models.knowledge import EMBED_DIM, Chunk, KnowledgeSource

# The real fragment, from the live index. 19 characters of OCR scraped off an
# otherwise-blank page; it really scores 0.64 against tone queries and really has
# been handed to the model as grounding. See `test_the_live_junk_fragment_...`.
_LIVE_JUNK = "PART ONE The Guitar"

_REAL_PASSAGE = (
    "A humbucker pickup cancels 60-cycle mains hum by combining two coils wound in "
    "opposite magnetic and electrical polarity: hum picked up equally by both coils "
    "cancels out, while the guitar string signal itself still adds constructively. "
    "This is why humbucker-equipped guitars are prized in high-gain settings where "
    "single coils would buzz audibly, and why a Stratocaster sounds thinner."
)


class _StubEmbedder:
    """A hashed bag-of-words, L2-normalised — not a random vector.

    It has to produce a MEANINGFUL cosine: these tests assert on the difference
    between `Hit.score` (RRF, ~0.03) and `Hit.vector_score` (a real cosine), and a
    stub whose vectors are near-orthogonal for every pair of texts would make every
    cosine ~0.0 and the distinction untestable. Word overlap -> high cosine is
    crude, deterministic, and enough — the SEMANTICS are e5's job and are measured
    against the real library by `scripts/retrieval_baseline.py`, not here.
    """

    def embed(self, texts, *, is_query=False):
        out = []
        for text in texts:
            v = [0.0] * EMBED_DIM
            for word in text.lower().split():
                slot = int(hashlib.sha256(word.strip(".,:;").encode()).hexdigest(), 16)
                v[slot % EMBED_DIM] += 1.0
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch):
    monkeypatch.setattr("app.brain.retrieve.get_embedder", lambda: _StubEmbedder())
    # No LLM in these tests: query normalisation must degrade to the raw query, not
    # raise. (That it does so is itself asserted below.)
    lexical.reset_index()
    yield
    lexical.reset_index()


def _seed(db, *texts) -> KnowledgeSource:
    """Chunks embedded with the SAME stub the queries use — a zero vector would
    give a NaN cosine (which is exactly what an un-re-embedded chunk does in
    production, and is asserted separately)."""
    source = KnowledgeSource(type="text", title="Book", language="en")
    db.add(source)
    db.commit()
    vectors = _StubEmbedder().embed(list(texts))
    for text, vector in zip(texts, vectors):
        db.add(Chunk(source_id=source.id, text=text, embedding=vector))
    db.commit()
    return source


# --- the floor ---------------------------------------------------------------

def _hit(text: str, *, cos: float) -> Hit:
    return Hit(
        chunk_id="c", source_id="s", source_title="t", text=text,
        section_path=None, page=None, score=0.03, vector_score=cos,
    )


def test_a_short_passage_never_passes_however_well_it_scores():
    """The length arm. The live 19-char fragment scores 0.64 — and higher-scoring
    junk exists. Score alone has never been able to see this."""
    assert not _passes_floor(_hit(_LIVE_JUNK, cos=0.99), set(), set())


def test_a_confident_dense_hit_passes_without_any_lexical_match():
    """A covered topic's best passage often shares only half the query's words —
    a paraphrase. The dense arm has to be able to admit it alone."""
    assert _passes_floor(_hit(_REAL_PASSAGE, cos=STRONG_COSINE), {"absent"}, set())


def test_complete_lexical_coverage_passes_a_dense_hit_the_model_is_unsure_of():
    """The gear case. `5150` tops out at 0.787 — the dense arm genuinely cannot
    recognise a bare model number, and the lexical arm is what rescues it."""
    assert _passes_floor(_hit(_REAL_PASSAGE, cos=0.78), {"5150"}, {"5150"})


def test_partial_lexical_coverage_is_not_enough():
    """THE UNCOVERED CASE, and the reason coverage must be COMPLETE. `vibrato`
    really does appear in the tutor's library (11 chunks — in an effects list), so
    a "matches any distinctive term" rule reports `vibrato and legato technique` as
    covered. Demanding every term separates "mentions the word" from "is about it".
    """
    assert not _passes_floor(
        _hit(_REAL_PASSAGE, cos=0.86), {"vibrato", "legato", "techniqu"}, {"vibrato"}
    )


def test_an_unmatchable_term_can_never_be_covered():
    """`tablature` is in ZERO chunks, so no passage can ever satisfy the lexical
    arm for a query that used it — which is exactly what "your library doesn't
    cover this" means."""
    assert not _passes_floor(_hit(_REAL_PASSAGE, cos=0.86), {"tablatur"}, set())


def test_a_query_with_no_distinctive_terms_abstains_rather_than_rejecting():
    """"how do I get a good sound?" — every word near-universal. The lexical arm
    must abstain, not return an empty search box."""
    assert _passes_floor(_hit(_REAL_PASSAGE, cos=0.75), set(), set())


def test_a_nan_cosine_fails_the_floor():
    """An un-re-embedded chunk (mid-migration, zero vector) gives a NaN distance.
    Every `>=` against NaN is False, so it drops out — a half-finished re-embed
    yields FEWER results, never wrong ones."""
    assert not _passes_floor(_hit(_REAL_PASSAGE, cos=float("nan")), set(), set())


def test_the_floor_constants_are_ordered_sanely():
    assert 0.0 < WEAK_COSINE_FLOOR < STRONG_COSINE < 1.0
    assert MIN_PASSAGE_CHARS > len(_LIVE_JUNK)


# --- the plumbing (DB-backed) ------------------------------------------------

def test_search_never_lets_the_rrf_score_become_the_cosine(db):
    """THE UNITS BUG THIS SEPARATION EXISTS TO PREVENT. `curriculum/ground.py` used
    to threshold `Hit.score >= 0.60`. RRF tops out near 2/61 ~= 0.033 — had `score`
    silently become the fusion score, EVERY curriculum module would have been
    reported as an uncovered gap, and it would have looked like a content problem.
    """
    _seed(db, _REAL_PASSAGE)
    [hit] = search(db, "humbucker pickup coils", k=5, apply_floor=False)

    # The RRF ceiling is a hard fact, not a measurement: a chunk ranked #1 by BOTH
    # arms scores 2/(60+1). Nothing fused can ever exceed it — which is why the old
    # `score >= 0.60` test in ground.py would have called every module a gap.
    rrf_ceiling = 2 / (60 + 1)
    assert hit.score <= rrf_ceiling

    # ...and the cosine lives on a completely different scale. That it is ABOVE the
    # RRF ceiling for a genuinely matching passage is the whole point: the two
    # numbers are not interchangeable and a threshold written for one is nonsense
    # against the other.
    assert hit.vector_score > rrf_ceiling
    assert hit.vector_score != hit.score


def test_vector_score_is_never_none_even_for_a_lexical_only_hit(db):
    """`vector_score` is documented as always populated — the floor depends on it,
    and a None would make every floor comparison a TypeError at retrieval time."""
    _seed(db, _REAL_PASSAGE)
    hits = search(db, "humbucker", k=5, apply_floor=False)
    assert hits
    assert all(isinstance(h.vector_score, float) for h in hits)


def test_the_live_junk_fragment_can_no_longer_be_cited(db):
    """REGRESSION, against a real thing that really happened. The 19-char
    "PART ONE The Guitar" chunk is in the live index and has been retrieved as
    grounding for real tone questions. It must now be unreachable through
    `search()` — which is where the chat pre-hop (`agent/loop.py`) gets its
    citations from, and the pre-hop no longer filters anything itself.
    """
    _seed(db, _LIVE_JUNK, _REAL_PASSAGE)

    for query in ("guitar tone", "pickups and tone", "PART ONE The Guitar"):
        hits = search(db, query, k=10)
        assert all(h.text != _LIVE_JUNK for h in hits), (
            f"the 19-char OCR fragment was retrievable for {query!r} — it can be "
            f"cited to the tutor as evidence again"
        )


def test_search_returns_chunks_with_and_without_pages(db):
    """outerjoin, not join: chunks whose source predates the Page model have
    page_id=NULL and must still be searchable, with page=None."""
    from app.models.knowledge import Page

    source = _seed(db, _REAL_PASSAGE)
    page = Page(source_id=source.id, page_no=42)
    db.add(page)
    db.commit()
    paged_text = _REAL_PASSAGE.replace("humbucker", "single-coil")
    db.add(
        Chunk(
            source_id=source.id, page_id=page.id, text=paged_text,
            embedding=_StubEmbedder().embed([paged_text])[0],
        )
    )
    db.commit()

    hits = search(db, "pickup coils hum", k=10, apply_floor=False)
    assert len(hits) == 2
    assert {h.page for h in hits} == {None, 42}


def test_query_normalisation_falls_back_to_the_raw_query_when_the_llm_is_down(db, monkeypatch):
    """A retrieval path that can 500 because a translation hiccuped would be a
    strictly worse trade than the one `normalize_query` exists to make."""
    from app.brain.retrieve import (
        _translate_call,
        normalize_query,
        reset_translation_breaker,
    )

    _seed(db, _REAL_PASSAGE)

    def _boom():
        raise RuntimeError("no API key")

    monkeypatch.setattr("app.brain.retrieve.get_provider", _boom)
    _translate_call.cache_clear()
    reset_translation_breaker()

    greek = "τι είναι το humbucker;"
    assert normalize_query(db, greek) == greek          # unchanged, not raised
    assert search(db, greek, k=5, apply_floor=False)    # and search still works

    # The breaker is TIME-based now, not a permanent latch: one failure must
    # not disable translation for the process lifetime (that silently
    # collapsed Greek retrieval quality until a restart). A settings change
    # re-arms it immediately.
    import app.brain.retrieve as retrieve_mod

    assert retrieve_mod._translation_blocked_until > 0
    reset_translation_breaker()
    assert retrieve_mod._translation_blocked_until == 0.0
    # And the failed lookup was NOT memoized: with the provider healthy again,
    # the same query gets a real translation attempt (lru_cache only stores
    # successes — a cached failure would pin the raw query forever).
    class _OkProvider:
        def chat(self, messages, **kw):
            return "what is a humbucker"

    monkeypatch.setattr("app.brain.retrieve.get_provider", lambda: _OkProvider())
    assert normalize_query(db, greek) == "what is a humbucker"


def test_an_english_query_is_never_sent_to_the_translator(db, monkeypatch):
    """The translation is ~150ms and costs money. It fires only when the query's
    script does not match the corpus's."""
    _seed(db, _REAL_PASSAGE)

    def _must_not_be_called():
        raise AssertionError("an English query was sent to the translator")

    monkeypatch.setattr("app.brain.retrieve.get_provider", _must_not_be_called)
    assert search(db, "humbucker pickup", k=5, apply_floor=False)
