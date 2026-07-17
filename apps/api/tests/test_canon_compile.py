"""Pass 1 — compile one book into concept claims (Part B, Task C2).

THE THREE PROPERTIES THAT MATTER HERE, none of which is visible at runtime:

  1. A CITATION IS VALIDATED, NEVER TRUSTED. The tutor CLICKS these chips and
     lands on a page. A hallucinated page number that reaches `concept_claim` is
     worse than no citation at all, because it is one he will trust.

  2. OUR DESCRIPTION OF A PICTURE IS NOT THE AUTHOR'S SENTENCE. `brain/ocr.py`
     bought those descriptions with a vision call and they are real content —
     Powers is 40 pages of tab. They may be cited; they may never be quoted. Once
     both are prose in `page.text`, `grounding` is the only thing that still knows.

  3. MONEY IS NOT SPENT TWICE. Reading ten books costs ~$13 of real model time on
     the tutor's own subscription. An app update that silently re-read them is the
     top severity class in this plan.

The fake provider below is the whole point of the seam: none of this needs a real
call to be pinned.
"""
import uuid

import pytest

import app.canon.compile as compile_mod
from app.brain.ocr import FIGURE_END, FIGURE_MARKER
from app.canon.compile import build_book_context, compile_book
from app.llm.errors import LLMError
from app.models.canon import BookCompile, Concept, ConceptAlias, ConceptClaim
from app.models.knowledge import KnowledgeSource, Page


class _FakeProvider:
    """`guided_json` as the provider seam exposes it, scripted."""

    def __init__(self, payload=None, *, raises=None):
        self.payload = payload if payload is not None else {"concepts": []}
        self.raises = raises
        self.calls = 0
        self.messages = []      # every transcript it was asked with
        self.last_kw: dict = {}  # the kwargs of the last guided_json call
        self.last_usage = {"usage": {"input_tokens": 23_000}, "cost_usd": 0.42}

    def guided_json(self, messages, schema, **kw):
        self.calls += 1
        self.messages.append(messages)
        self.last_kw = kw
        if self.raises:
            raise self.raises
        return self.payload

    def count_tokens(self, text: str) -> int:
        return len(text) // 3 + 1

    @property
    def prompt(self) -> str:
        """Everything the model was actually shown, flattened."""
        return "\n".join(m["content"] for m in self.messages[-1])


@pytest.fixture(autouse=True)
def _model_name(monkeypatch):
    """`compile_book` stamps the model it used onto `book_compile`. Pin it so the
    assertions do not depend on the tutor's Settings row."""
    monkeypatch.setattr(compile_mod, "_model_name", lambda: "claude-sonnet-5")


def _book(db, pages: dict[int, str], title="Guitar Exercises Made Simple"):
    source = KnowledgeSource(type="pdf", title=title, status="ready")
    db.add(source)
    db.flush()
    for page_no, text in pages.items():
        db.add(Page(source_id=source.id, page_no=page_no, text=text, status="ready"))
    db.commit()
    return source


def _use(monkeypatch, provider):
    monkeypatch.setattr(compile_mod, "get_provider", lambda: provider)
    return provider


_PROSE = ("Alternate picking is the foundation of speed. Down, up, down, up — "
          "the pick never rests, and the wrist does the work, not the arm.")


def _concept(name="alternate picking", **kw):
    claim = {"text": "The wrist does the work, not the arm.", "pages": [12],
             "stance": "wrist, not arm", "depth": "primary",
             "grounding": "author"}
    claim.update(kw.pop("claim", {}))
    out = {"name": name, "name_el": "εναλλασσόμενη πενιά", "claims": [claim]}
    out.update(kw)
    return out


# ---------------------------------------------------------------------------
# 1. Citations are validated, never trusted
# ---------------------------------------------------------------------------

def test_a_citation_to_a_page_that_does_not_exist_is_dropped_and_logged(
    db, monkeypatch, caplog
):
    """THE BRIEF'S TEST. Two concepts, one citing a page the book does not have.
    Only the valid claim is stored — and the drop is LOGGED, because a citation
    silently vanishing is how you find out months later that a book compiled to
    half a ledger."""
    source = _book(db, {12: _PROSE})
    fake = _use(monkeypatch, _FakeProvider({"concepts": [
        _concept("alternate picking"),
        _concept("string skipping", claim={"pages": [999], "text": "Skip a string."}),
    ]}))

    with caplog.at_level("WARNING"):
        compile_book(db, source.id)

    claims = db.query(ConceptClaim).all()
    assert len(claims) == 1, "a page the book does not have entered the canon"
    assert claims[0].pages == [12]
    assert fake.calls == 1
    assert "999" in caplog.text, "an invalid citation was dropped without a word"


def test_only_the_bad_page_is_dropped_when_a_claim_cites_good_and_bad_together(
    db, monkeypatch
):
    """A claim citing pp.12 AND 999 keeps p.12. The prose is not the problem —
    the fabricated page number is, and it is the only part worth destroying."""
    source = _book(db, {12: _PROSE, 13: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept(claim={"pages": [12, 999, 13]}),
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).one().pages == [12, 13]


def test_a_claim_whose_every_citation_is_invalid_is_dropped_entirely(db, monkeypatch):
    """An uncitable claim is not a claim. Keeping it with `pages=[]` would put a
    sentence in the canon that no page supports — which is what the canon is FOR
    stopping."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept(claim={"pages": [998, 999]}),
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).count() == 0
    assert db.query(Concept).count() == 0, "a concept with no surviving claim was kept"


def test_a_page_below_the_char_floor_is_not_citable(db, monkeypatch):
    """`page_index` is the set of pages the model was ACTUALLY SHOWN, not the set
    of rows in the DB. A page whose OCR produced 12 characters is in the database
    and is NOT in the prompt — so the model cannot have read it, so a cite to it is
    a fabrication even though the row exists. Same contract as
    `corpus.LibraryContext.page_index`."""
    source = _book(db, {12: _PROSE, 13: "p. 13"})   # 13 is a running head, nothing more
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept(claim={"pages": [13]}),
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).count() == 0


# ---------------------------------------------------------------------------
# 2. The [FIGURE] contract
# ---------------------------------------------------------------------------

_TAB_PAGE = (f"{FIGURE_MARKER}\nA tablature exercise in 4/4. Measure 1: under "
             f"\"Am\", columns of 1/2/2 repeated twice on the D and G strings.\n"
             f"{FIGURE_END}")


def test_a_claim_from_a_figure_region_is_marked_as_ours_not_the_books_words(
    db, monkeypatch
):
    """THE BRIEF'S TEST. p.31 is nothing but our description of a tab staff. A
    claim citing it is TRUE and CITABLE — "the tab on p.31 shows..." — and it is
    not Maxwell Powers' sentence. `grounding` is the only thing that still knows
    that once both are prose in the same column."""
    source = _book(db, {12: _PROSE, 31: _TAB_PAGE})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept("am arpeggio exercise", claim={
            "pages": [31], "text": "An Am exercise runs 1/2/2 on the D and G strings.",
            "grounding": "figure",
        }),
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).one().grounding == "figure"


def test_grounding_is_FORCED_to_figure_when_the_cited_page_has_no_prose_at_all(
    db, monkeypatch
):
    """THE DECLARATION IS NOT TRUSTED EITHER. p.31 has no words of its own, so a
    claim citing only p.31 CANNOT be the author's — whatever the model said. This
    is the same discipline as the page check: verify where verification is
    possible, and never take the model's word for the one thing that fabricates a
    quotation."""
    source = _book(db, {12: _PROSE, 31: _TAB_PAGE})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept("am arpeggio", claim={
            "pages": [31], "grounding": "author",     # <- the model is wrong
            "text": "Powers writes that the Am exercise runs 1/2/2.",
        }),
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).one().grounding == "figure"


def test_grounding_is_FORCED_to_author_when_the_cited_page_has_no_figure_at_all(
    db, monkeypatch
):
    """The complement, and it is safe for the same structural reason: p.12 carries
    no figure region, so by the contract's TOTALITY every character of it is the
    page's own words. A claim citing only p.12 is the author's, whatever the model
    said — and marking it "figure" would quietly demote a real quotation to
    unquotable."""
    source = _book(db, {12: _PROSE, 31: _TAB_PAGE})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept(claim={"pages": [12], "grounding": "figure"}),   # <- wrong
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).one().grounding == "author"


def test_an_unknown_grounding_value_fails_safe_to_figure(db, monkeypatch):
    """On a MIXED page the declaration is all we have, so a value we do not
    recognise must land on the side that cannot fabricate: "figure" costs a
    paragraph of attribution, "author" costs a quotation the author never wrote."""
    # Both halves must clear `MIN_PAGE_CHARS`, or the page is not MIXED and the
    # structural rules above would settle it without consulting the declaration.
    mixed = (f"{_PROSE}\n{FIGURE_MARKER}\nA photo of a Telecaster's bridge plate, "
             f"three brass saddles and the ashtray removed.\n{FIGURE_END}")
    source = _book(db, {12: mixed})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept(claim={"pages": [12], "grounding": "hearsay"}),
    ]}))

    compile_book(db, source.id)

    assert db.query(ConceptClaim).one().grounding == "figure"


def test_the_model_is_shown_the_authors_words_and_our_descriptions_LABELLED_APART(
    db, monkeypatch
):
    """The compile does NOT strip figures — that would throw away the content the
    OCR run was paid for (Powers is 40 pages of tab). It shows both, labelled, so
    the model can tell them apart and declare which it used."""
    source = _book(db, {12: _PROSE, 31: _TAB_PAGE})
    fake = _use(monkeypatch, _FakeProvider())

    compile_book(db, source.id)

    prompt = fake.prompt
    assert "[p.12]" in prompt
    assert "the wrist does the work" in prompt.lower(), "the author's words were not shown"
    assert "tablature exercise in 4/4" in prompt, "our figure description was stripped"
    assert "[p.31 FIGURE]" in prompt, "our description was shown as the author's words"


def test_the_raw_figure_markers_never_reach_the_model(db, monkeypatch):
    """The prompt carries OUR labelling, not `ocr.py`'s storage markers. Leaving
    both in means two conventions describing the same thing, and the model gets to
    pick which one it believes."""
    source = _book(db, {31: _TAB_PAGE})
    fake = _use(monkeypatch, _FakeProvider())

    compile_book(db, source.id)

    assert FIGURE_MARKER not in fake.prompt
    assert FIGURE_END not in fake.prompt


def test_the_compile_reads_the_book_under_the_compile_role_not_the_default(
    db, monkeypatch
):
    """The whole-book read is the LONGEST call in the app and must not run under the
    default 600s `guided_json` timeout — Gallagher (366K tokens) overran it and the
    compile failed with a `ReadTimeout`. `compile_book` tags the call `role="compile"`,
    which both providers map to a book-length budget: a far larger timeout on the
    `claude -p` bridge, and a streamed, large-output request on the real API. Without
    the role the largest and most important book cannot be compiled at all."""
    source = _book(db, {12: _PROSE})
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))

    compile_book(db, source.id)

    assert fake.last_kw.get("role") == "compile"


# ---------------------------------------------------------------------------
# 3. Money is not spent twice
# ---------------------------------------------------------------------------

def test_a_book_already_compiled_is_NOT_recompiled_and_costs_nothing(db, monkeypatch):
    """THE MONEY GUARD, and it is the top severity class in this plan. Chris:
    "when i send him an update of the app, the data of the user must be the same"
    — an update that silently re-read ten books would spend his subscription to
    produce what it already had."""
    source = _book(db, {12: _PROSE})
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    compile_book(db, source.id)
    assert fake.calls == 1

    again = compile_book(db, source.id)

    assert fake.calls == 1, "a compiled book was re-read — that is real money"
    assert again.status == "ready"
    assert db.query(ConceptClaim).count() == 1


def test_force_recompiles_and_REPLACES_the_previous_ledger(db, monkeypatch):
    """A recompile is a replacement, not an append: two readings of one book
    interleaved is a ledger nobody can reason about, and it would double every
    claim the model happened to repeat."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [_concept("alternate picking")]}))
    compile_book(db, source.id)

    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept("string skipping")]}))
    compile_book(db, source.id, force=True)

    assert fake.calls == 1
    claims = db.query(ConceptClaim).all()
    assert len(claims) == 1, "the old ledger survived alongside the new one"
    keys = {c.key for c in db.query(Concept).all()}
    assert "string-skipping" in keys


def test_a_recompile_does_not_stack_duplicate_aliases(db, monkeypatch):
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    compile_book(db, source.id)
    _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    compile_book(db, source.id, force=True)

    assert db.query(ConceptAlias).count() == 1


# ---------------------------------------------------------------------------
# 4. The ledger itself
# ---------------------------------------------------------------------------

def test_book_compile_records_the_model_and_the_token_count(db, monkeypatch):
    """"Was this compiled by the good model?" must be a question the DATA answers.
    A canon half-built by a cheap model looks identical and there would be no way
    to tell which of his books got the careless read."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))

    record = compile_book(db, source.id)

    assert record.status == "ready"
    assert record.model == "claude-sonnet-5"
    assert record.token_count > 0
    assert record.concept_count == 1
    assert record.compiled_at is not None
    assert record.error is None


def test_token_count_counts_the_CACHED_input_too(db, monkeypatch):
    """CAUGHT BY THE LIVE COMPILE OF POWERS, NOT BY THIS SUITE.

    The `claude` CLI applies its own prompt caching, so the real 40,206-token book
    came back as `cache_creation_input_tokens: 40206` and `input_tokens: 2` — and
    reading `input_tokens` alone stamped **`token_count = 2`** on a 57-page book.
    Nothing fails, nothing looks wrong, and the one column that exists to answer
    "what did reading this book actually cost?" quietly answers "nothing" — which
    also makes the plan's "~$1.26/book" estimate uncheckable against reality,
    forever, on every book compiled from here on.

    The payload below is the REAL one from that run, pasted verbatim.
    """
    source = _book(db, {12: _PROSE})
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    fake.last_usage = {
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 40206,
                  "cache_read_input_tokens": 0, "output_tokens": 4630},
        "cost_usd": 0.341349, "duration_ms": 46977,
    }

    record = compile_book(db, source.id)

    assert record.token_count == 40_208, "the cached input was not counted as input"


def test_token_count_reads_the_flat_shape_claude_py_reports(db, monkeypatch):
    """`ClaudeProvider.last_usage` is flat; `ClaudeCLIProvider`'s nests under
    `usage`. The canon must not care which provider the tutor picked."""
    source = _book(db, {12: _PROSE})
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    fake.last_usage = {"input_tokens": 1_000, "cache_creation_input_tokens": 30_000,
                       "cache_read_input_tokens": 500, "output_tokens": 4_000}

    record = compile_book(db, source.id)

    assert record.token_count == 31_500


def test_token_count_falls_back_to_the_estimate_when_usage_says_nothing(db, monkeypatch):
    """A provider that reports no usage must leave an honest estimate on the row,
    not a zero that reads as "this book was free"."""
    source = _book(db, {12: _PROSE})
    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    fake.last_usage = {}

    record = compile_book(db, source.id)

    assert record.token_count > 0


def test_two_books_naming_a_concept_identically_share_ONE_concept_row(db, monkeypatch):
    """A free reconciliation, before C3 runs at all — and the reason `concept.key`
    is UNIQUE. Two claims on one concept from two sources is exactly the shape
    divergence is read from."""
    powers = _book(db, {12: _PROSE}, title="Powers")
    kahn = _book(db, {12: _PROSE}, title="Kahn")
    _use(monkeypatch, _FakeProvider({"concepts": [_concept("alternate picking")]}))
    compile_book(db, powers.id)
    _use(monkeypatch, _FakeProvider({"concepts": [_concept("Alternate Picking")]}))
    compile_book(db, kahn.id)

    assert db.query(Concept).count() == 1
    claims = db.query(ConceptClaim).all()
    assert len(claims) == 2
    assert {c.source_id for c in claims} == {powers.id, kahn.id}


def test_each_book_records_what_IT_called_the_concept(db, monkeypatch):
    """`concept_alias` exists so a bad C3 merge is reversible WITHOUT recompiling.
    The book's own name for the idea is kept, per source."""
    powers = _book(db, {12: _PROSE}, title="Powers")
    kahn = _book(db, {12: _PROSE}, title="Kahn")
    _use(monkeypatch, _FakeProvider({"concepts": [_concept("alternate picking")]}))
    compile_book(db, powers.id)
    _use(monkeypatch, _FakeProvider({"concepts": [_concept("Alternate Picking")]}))
    compile_book(db, kahn.id)

    aliases = {a.source_id: a.alias for a in db.query(ConceptAlias).all()}
    assert aliases == {powers.id: "alternate picking", kahn.id: "Alternate Picking"}


def test_the_concepts_greek_label_is_kept(db, monkeypatch):
    """`el` is the default locale and the tutor is Greek. A canon whose index he
    cannot read is a canon he cannot steer."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))

    compile_book(db, source.id)

    assert db.query(Concept).one().label_el == "εναλλασσόμενη πενιά"


def test_a_stance_longer_than_the_column_is_truncated_not_dropped(db, monkeypatch):
    """`stance` is varchar(40) and `llm/schema.py` STRIPS `maxLength` before the
    model ever sees it — so nothing upstream enforces this and an over-long stance
    would otherwise be a DataError that loses the whole book's compile. Truncating
    a label cannot invert its meaning; losing 57 pages of work can."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider({"concepts": [
        _concept(claim={"stance": "x" * 200}),
    ]}))

    compile_book(db, source.id)

    assert len(db.query(ConceptClaim).one().stance) == 40


# ---------------------------------------------------------------------------
# 5. Failing honestly
# ---------------------------------------------------------------------------

def test_a_provider_failure_leaves_book_compile_failed_with_the_error(db, monkeypatch):
    """A failed compile must be a FACT ON THE ROW, not an exception in a log. The
    caller is a background job; by the time this runs there is nobody to tell."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider(raises=LLMError("rate_limit", "5-hour cap")))

    with pytest.raises(LLMError):
        compile_book(db, source.id)

    record = db.query(BookCompile).one()
    assert record.status == "failed"
    assert "5-hour cap" in record.error
    assert db.query(ConceptClaim).count() == 0


def test_a_failed_compile_is_retryable_without_force(db, monkeypatch):
    """Only "ready" is the money guard. A book that failed was never read, so
    nothing is re-spent by trying again — and requiring `force=True` here would
    make the honest retry look like the dangerous one."""
    source = _book(db, {12: _PROSE})
    _use(monkeypatch, _FakeProvider(raises=LLMError("rate_limit", "5-hour cap")))
    with pytest.raises(LLMError):
        compile_book(db, source.id)

    fake = _use(monkeypatch, _FakeProvider({"concepts": [_concept()]}))
    record = compile_book(db, source.id)

    assert fake.calls == 1
    assert record.status == "ready"
    assert record.error is None, "the old failure survived onto a healthy row"


def test_a_book_with_no_readable_text_fails_honestly_and_never_calls_the_model(
    db, monkeypatch
):
    """Gallagher is 388 pages of `text IS NULL` right now, mid-OCR. Compiling it
    would buy a call that reads an empty book and returns confident nothing."""
    source = _book(db, {1: "", 2: None})
    fake = _use(monkeypatch, _FakeProvider())

    record = compile_book(db, source.id)

    assert fake.calls == 0, "a call was paid for to read an empty book"
    assert record.status == "failed"
    assert "no readable text" in record.error.lower()


def test_an_unknown_source_is_an_error_not_an_empty_compile(db, monkeypatch):
    _use(monkeypatch, _FakeProvider())
    with pytest.raises(LookupError):
        compile_book(db, uuid.uuid4())


# ---------------------------------------------------------------------------
# 6. The context
# ---------------------------------------------------------------------------

def test_build_book_context_indexes_the_pages_it_actually_showed(db):
    source = _book(db, {12: _PROSE, 13: "p. 13", 31: _TAB_PAGE})

    ctx = build_book_context(db, source.id)

    assert ctx.page_index == {12, 31}, "the running head on p.13 is not citable"
    assert ctx.author_pages == {12}
    assert ctx.figure_pages == {31}
    assert ctx.token_count > 0
