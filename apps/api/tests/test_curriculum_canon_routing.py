"""C5 — routing curriculum authoring through the canon above the threshold.

```
selected sources -> count_tokens
   <= canon_threshold (300K) -> full-context verbatim   (today's path, UNCHANGED)
   >  canon_threshold        -> the canon block
```

THE ASSERTIONS ARE AT THE PROMPT LAYER, NOT ON A FLAG. The `fits` bug (Task 7)
was precisely a flag computed and never read; this test suite inspects what
actually goes into the messages `prefix_messages` builds — below the threshold
the `<source ...>[p.N]` library block ships, above it the `<canon ...>` block
ships instead.

The uncompiled-book rule is the anti-silent-miss guarantee: a selected book
whose compile has not finished must never be silently dropped from a canon. Full
context is used whenever it still fits; when it does NOT fit and a book is
uncompiled, two graceful rungs replace the old refusal (which killed the tutor's
real run twice on 2026-07-20): a MIXED context (canon for the compiled books +
the uncompiled sources verbatim, disjoint refs), and per-lesson retrieval when
even that does not fit. Nothing is silently lost on either rung — the mixed
block carries every source on the page, and retrieval searches every chunk.
"""
import uuid

import pytest

import app.canon.render as render_mod
import app.curriculum.corpus as corpus_mod
from app.curriculum.corpus import (
    CurriculumContextError,
    build_curriculum_context,
    prefix_messages,
)
from app.models.canon import BookCompile, Concept, ConceptAlias, ConceptClaim
from app.models.knowledge import KnowledgeSource, Page


class _FakeProvider:
    """Serves `count_tokens` for both corpus and render, deterministically, so a
    context's size is a pure function of its text and the routing boundary is
    exact."""

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(render_mod, "get_provider", lambda: _FakeProvider())


def _book(db, title="Getting Great Guitar Sounds", *, pages=(19, 20),
          body="the page text is long enough to count"):
    source = KnowledgeSource(type="pdf", title=title, language="en", status="ready")
    db.add(source)
    db.flush()
    for page_no in pages:
        db.add(Page(source_id=source.id, page_no=page_no,
                    text=f"{title} p.{page_no}: {body} " * 3, status="ready"))
    db.flush()
    return source


def _compile(db, source, *, key, label_en, page):
    """Give a book a `ready` compile plus one concept/claim/alias, so
    `build_canon_context` renders a real `<canon>` block for it."""
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5",
                       concept_count=1, token_count=100))
    concept = Concept(key=key, label_en=label_en, label_el=None)
    db.add(concept)
    db.flush()
    db.add(ConceptClaim(concept_id=concept.id, source_id=source.id,
                        text=f"{label_en} claim", pages=[page], stance="a stance",
                        depth="primary", grounding="author"))
    db.add(ConceptAlias(concept_id=concept.id, source_id=source.id, alias=label_en))
    db.commit()


def _prefix_text(context) -> str:
    return "\n".join(m["content"] for m in prefix_messages(context))


# ---------------------------------------------------------------------------
# The boundary, asserted at the prompt layer
# ---------------------------------------------------------------------------

def test_at_or_below_the_threshold_the_LIBRARY_block_ships(db, monkeypatch):
    source = _book(db)
    _compile(db, source, key="tube-screamer", label_en="Tube Screamer", page=19)
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [source.id]).token_count
    # exactly at the threshold: <= means the library ships (the "299K" case)
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens)

    context = build_curriculum_context(db, [source.id])
    body = _prefix_text(context)

    assert "<source id=" in body, "the verbatim library block must ship at/below the threshold"
    assert "[p.19]" in body, "the page markers are what make a citation checkable"
    assert "<canon" not in body, "the canon must NOT ship below the threshold"


def test_above_the_threshold_the_CANON_block_ships(db, monkeypatch):
    source = _book(db)
    _compile(db, source, key="tube-screamer", label_en="Tube Screamer", page=19)
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [source.id]).token_count
    # one below the library's size: token_count > threshold routes to the canon
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens - 1)

    context = build_curriculum_context(db, [source.id])
    body = _prefix_text(context)

    assert "<canon" in body, "the canon block must ship above the threshold"
    assert "CONCEPT: tube-screamer" in body, "the canon's concepts must be in the prompt"
    assert "<source id=" not in body, "the verbatim library must NOT ship above the threshold"


# ---------------------------------------------------------------------------
# A selected book whose compile has not finished
# ---------------------------------------------------------------------------

def test_uncompiled_book_above_threshold_falls_back_to_full_context_when_it_fits(db, monkeypatch):
    """Above the threshold we prefer the canon — but a selected book that is not
    compiled would be silently missing from it. So we use full context, which
    reads everything, as long as it still fits. NOT a degrade: full context is
    the higher-fidelity path."""
    a = _book(db, "Compiled Book")
    b = _book(db, "Uncompiled Book")
    _compile(db, a, key="pickup-height", label_en="Pickup height", page=19)
    # b has NO BookCompile row — it is uncompiled.
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [a.id, b.id]).token_count
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens - 1)
    # it still fits whole
    monkeypatch.setattr(corpus_mod.settings, "full_context_budget", tokens + 1)

    context = build_curriculum_context(db, [a.id, b.id])
    body = _prefix_text(context)

    assert "<source id=" in body, "full context must ship when it fits and a book is uncompiled"
    assert "Uncompiled Book" in body, "the uncompiled book's OWN TEXT must be read, not dropped"
    assert "<canon" not in body, "a canon that omits a selected book must NOT ship"


def test_uncompiled_book_that_does_NOT_fit_gets_the_MIXED_context(db, monkeypatch):
    """It does not fit whole AND a selected book is uncompiled: the canon carries
    the compiled book, the uncompiled one rides along VERBATIM after it, and the
    refs stay disjoint so citations still resolve. This replaced the refusal that
    failed the tutor's real run twice on 2026-07-20."""
    # A REALISTICALLY large compiled book: its canon render (one concept + the
    # legend) must be far smaller than its raw text, like the tutor's real books
    # (300K raw -> a few K of canon). A two-page fixture would invert that ratio
    # and the mixed rung could never fit.
    a = _book(db, "Compiled Book", pages=tuple(range(1, 40)),
              body="a long page of real prose about pickup height and tone " * 20)
    b = _book(db, "Uncompiled Book")
    _compile(db, a, key="pickup-height", label_en="Pickup height", page=19)
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [a.id, b.id]).token_count
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens - 1)
    # it does NOT fit whole — but canon(a) + verbatim(b) does
    monkeypatch.setattr(corpus_mod.settings, "full_context_budget", tokens - 1)

    context = build_curriculum_context(db, [a.id, b.id])
    body = _prefix_text(context)
    assert "<canon" in body, "the compiled book must arrive as canon"
    assert 'title="Uncompiled Book"' in body, "the uncompiled book must ride along verbatim"
    assert context.fits
    # Disjoint refs: the canon claimed S1, so the verbatim block starts at S2 —
    # a shared ref would corrupt every citation check downstream.
    assert set(context.ref_to_source_id.keys()) == {"S1", "S2"}
    assert context.ref_to_source_id["S2"] == b.id


def test_when_even_the_mixed_context_does_not_fit_it_degrades_to_retrieval(db, monkeypatch):
    """The last rung: nothing readable fits whole, so ground per-lesson via
    retrieval (fits=False) — never refuse, never fail the unattended run."""
    a = _book(db, "Compiled Book")
    b = _book(db, "Uncompiled Book")
    _compile(db, a, key="pickup-height", label_en="Pickup height", page=19)

    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", 1)
    monkeypatch.setattr(corpus_mod.settings, "full_context_budget", 1)

    context = build_curriculum_context(db, [a.id, b.id])
    assert not context.fits, "retrieval context grounds per-lesson"
    assert not context.is_empty


def test_below_threshold_an_uncompiled_book_is_full_context_as_usual(db, monkeypatch):
    """Below the threshold nothing about the canon applies — an uncompiled book is
    read whole exactly as today. This is the working 90K path and it must be
    untouched."""
    b = _book(db, "Uncompiled Book")     # no compile at all
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [b.id]).token_count
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens + 100)

    context = build_curriculum_context(db, [b.id])
    body = _prefix_text(context)
    assert "<source id=" in body
    assert "<canon" not in body


# ---------------------------------------------------------------------------
# The cache invariant on the canon path
# ---------------------------------------------------------------------------

def test_the_canon_prefix_is_byte_identical_across_two_calls_and_is_cached(db, monkeypatch):
    """corpus.py's entire economic model: the cached prefix must be byte-identical
    across calls, or every draft re-writes it at 1.25x. The canon path inherits
    this — the render is deterministic and the prefix carries `cache: True`, which
    is what makes the second call READ the cache (see the live integration test)."""
    source = _book(db)
    _compile(db, source, key="tube-screamer", label_en="Tube Screamer", page=19)
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [source.id]).token_count
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens - 1)

    first = prefix_messages(build_curriculum_context(db, [source.id]))
    second = prefix_messages(build_curriculum_context(db, [source.id]))

    cached_first = [m for m in first if m.get("cache")]
    cached_second = [m for m in second if m.get("cache")]
    assert cached_first, "the canon prefix must be marked cache: True, or nothing is cached"
    assert [m["content"] for m in cached_first] == [m["content"] for m in cached_second], (
        "the canon prefix is NOT byte-identical across two calls — every draft would "
        "re-write the block at 1.25x and the only symptom is the invoice"
    )
    assert "<canon" in cached_first[0]["content"]


def test_a_ready_compile_with_zero_claims_degrades_to_retrieval_not_general_knowledge(
    db, monkeypatch
):
    """Every contributor SAYS compiled, but the canon holds no claims (lost
    rows, purged ledger, bad migration). The empty canon used to ship anyway,
    `prefix_messages` rendered its NO_LIBRARY branch, and the tutor paid full
    price for a course drafted ENTIRELY from general knowledge — his library
    never sent, no error anywhere, the only tell the tier badges. Retrieval
    (`fits=False` -> per-lesson grounding over every chunk) is the honest
    floor."""
    source = _book(db)
    # A `ready` BookCompile with NO Concept/ConceptClaim behind it.
    db.add(BookCompile(source_id=source.id, status="ready", model="claude-sonnet-5",
                       concept_count=0, token_count=100))
    db.commit()
    from app.curriculum.corpus import build_library_context

    tokens = build_library_context(db, [source.id]).token_count
    monkeypatch.setattr(corpus_mod.settings, "canon_threshold", tokens - 1)

    context = build_curriculum_context(db, [source.id])

    assert not context.is_empty, "retrieval context still carries the sources"
    assert context.fits is False, "fits=False is what routes every draft to per-lesson retrieval"
    body = _prefix_text(context)
    assert "You have nothing of his to read" not in body, (
        "the NO_LIBRARY framing must never ship when the tutor DID select sources"
    )
