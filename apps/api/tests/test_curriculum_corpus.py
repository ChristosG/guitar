"""The full-context library block (Plan 13, Stage 6.5) — the heart of the new
architecture, and the one part of it that can fail SILENTLY AND EXPENSIVELY.

Two properties are load-bearing and neither is visible at runtime:

  1. THE PREFIX IS STABLE. The library block must be byte-identical between the
     outline call and every one of the 20 lesson drafts, or the prompt cache never
     hits. Nothing breaks when it doesn't — the curriculum still generates. It just
     costs ten times as much, and the only symptom is an invoice a month later.

  2. THE TOKEN COUNT IS MEASURED, NOT GUESSED. Above the budget the app must
     degrade to retrieval WITH A BANNER. A silent downgrade is the bug the whole
     stage exists to remove.
"""
import uuid

import pytest

import app.curriculum.corpus as corpus_mod
from app.brain.ingest import IngestPayload, ingest_source
from app.curriculum.corpus import (
    LibraryContext,
    build_library_context,
    library_message,
)
from app.curriculum.draft import LessonContext, build_lesson_messages
from app.curriculum.outline import build_outline_messages
from app.curriculum.shape import plan_shape
from app.llm.anthropic_wire import to_anthropic
from app.models.knowledge import KnowledgeSource, Page


class _FakeTokenizer:
    """`count_tokens` as the provider seam exposes it. 1 token per 3 chars —
    pessimistic, like the real fallback, and exact enough to test a budget branch."""

    def __init__(self):
        self.calls = 0

    def count_tokens(self, text: str) -> int:
        self.calls += 1
        return len(text) // 3 + 1


@pytest.fixture(autouse=True)
def _fake_tokens(monkeypatch):
    monkeypatch.setattr(corpus_mod, "get_provider", lambda: _FakeTokenizer())


def _source_with_pages(db, title, pages: list[tuple[int, str]]) -> KnowledgeSource:
    source = KnowledgeSource(type="pdf", title=title, language="en", status="ready")
    db.add(source)
    db.flush()
    for page_no, text in pages:
        db.add(Page(source_id=source.id, page_no=page_no, text=text, status="ready"))
    db.commit()
    return source


_BOOK = [
    (19, "The Tube Screamer is the most copied overdrive pedal ever built. " * 3),
    (20, "A humbucker cancels hum by pairing two coils wound in opposition. " * 3),
    (21, "Gain staging: the preamp sets the character, the power amp sets the feel. " * 3),
]


# ---------------------------------------------------------------------------
# The block itself
# ---------------------------------------------------------------------------

def test_the_library_block_is_page_annotated_so_a_citation_can_be_checked(db):
    """`[p.N]` markers are not decoration. They are the only mechanism by which a
    model reading 90K tokens of prose can tell us WHICH PAGE a claim came from —
    and they are what `draft.py` validates every citation against."""
    source = _source_with_pages(db, "Getting Great Guitar Sounds", _BOOK)

    library = build_library_context(db, [source.id])

    assert '<source id="S1" title="Getting Great Guitar Sounds">' in library.text
    assert "[p.19]" in library.text
    assert "[p.20]" in library.text
    assert "Tube Screamer" in library.text
    assert library.page_index == {"S1": {19, 20, 21}}
    assert library.ref_to_source_id == {"S1": source.id}


def test_the_page_index_is_what_the_model_was_SHOWN_not_what_the_database_holds(db):
    """A page whose OCR produced 12 characters is in the DB and is NOT in the
    prompt. The model cannot have read it there, so a citation to it is a
    fabrication — even though the row exists."""
    source = _source_with_pages(db, "Book", [
        (1, "A real page of substantive text about guitar amplifiers. " * 3),
        (2, "blank"),          # under MIN_PAGE_CHARS — never reaches the prompt
    ])

    library = build_library_context(db, [source.id])

    assert library.page_index["S1"] == {1}
    assert "[p.2]" not in library.text


def test_selecting_no_sources_is_a_deliberate_answer_not_a_missing_one(db):
    """`[]` means "none of it". `None` means "all of it". They are different
    answers and both are legitimate — conflating them (the classic `if not
    source_ids:` bug) silently gives a tutor who chose nothing the whole library."""
    _source_with_pages(db, "Book", _BOOK)

    chose_none = build_library_context(db, [])
    chose_nothing_explicit = build_library_context(db, None)

    assert chose_none.is_empty
    assert chose_none.text == ""
    assert not chose_nothing_explicit.is_empty
    assert "Tube Screamer" in chose_nothing_explicit.text


def test_a_source_with_no_readable_text_is_skipped_not_emitted_as_an_empty_element(db):
    empty = KnowledgeSource(type="pdf", title="Unread Scan", language="en", status="ready")
    db.add(empty)
    db.commit()
    real = _source_with_pages(db, "Book", _BOOK)

    library = build_library_context(db, [empty.id, real.id])

    assert len(library.sources) == 1
    assert library.sources[0]["title"] == "Book"


def test_page_text_is_preferred_over_chunks_because_chunks_overlap(db):
    """`brain/chunk.py` carries 150 characters of OVERLAP between consecutive
    chunks. Concatenating them duplicates a sentence at every boundary — harmless
    for retrieval (they are never concatenated there), and here it would put ~12%
    of the book into the prompt twice and bill for it."""
    source = KnowledgeSource(type="text", title="Book", language="en")
    db.add(source)
    db.commit()
    ingest_source(db, source.id, IngestPayload(
        kind="text",
        text="Single-coil pickups sound bright and glassy. " * 60,
    ))
    db.commit()

    library = build_library_context(db, [source.id])

    page = db.query(Page).filter(Page.source_id == source.id).one()
    # The block is the PAGE's text, not a concatenation of overlapping chunks.
    assert len(library.text) < len(page.text) + 200


# ---------------------------------------------------------------------------
# The budget — measured, and honest when it doesn't fit
# ---------------------------------------------------------------------------

def test_a_library_that_fits_says_so_in_words_the_tutor_can_read(db):
    _source_with_pages(db, "Book", _BOOK)
    library = build_library_context(db, None)

    assert library.fits
    summary = library.summary()
    assert "1 source" in summary
    assert "tokens" in summary
    assert "fits whole" in summary


def test_an_oversized_library_does_not_fit_and_says_that_too(db, monkeypatch):
    """Above the budget we degrade to per-module retrieval WITH A BANNER. Never
    silently: a tutor whose library quietly stopped being read in full, and who was
    never told, is back to the bug we started with."""
    monkeypatch.setattr(corpus_mod.settings, "full_context_budget", 10)
    _source_with_pages(db, "Book", _BOOK)

    library = build_library_context(db, None)

    assert not library.fits
    assert "TOO LARGE" in library.summary()


def test_a_failing_count_tokens_never_takes_down_the_source_selection_screen(db, monkeypatch):
    """A count_tokens call that fails (no key yet, a rate limit) must not 500 the
    step the tutor is standing on. The local estimate is pessimistic, so the
    fallback errs toward "this might not fit" — which degrades to retrieval with a
    banner, rather than toward a context-length 400 ninety seconds into a call."""
    class _Broken:
        def count_tokens(self, text):
            raise RuntimeError("no key")

    monkeypatch.setattr(corpus_mod, "get_provider", lambda: _Broken())
    _source_with_pages(db, "Book", _BOOK)

    library = build_library_context(db, None)

    assert library.token_count > 0
    assert "Tube Screamer" in library.text


# ---------------------------------------------------------------------------
# THE CACHE. This is the one that saves ~$5 a curriculum and fails invisibly.
# ---------------------------------------------------------------------------

def test_the_library_block_carries_an_ephemeral_cache_breakpoint(db):
    _source_with_pages(db, "Book", _BOOK)
    library = build_library_context(db, None)

    _system, msgs = to_anthropic([library_message(library)])

    blocks = msgs[0]["content"]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_the_outline_and_every_lesson_draft_share_a_BYTE_IDENTICAL_cached_prefix(db):
    """THE assertion that protects the cost model.

    The outline call WRITES the 90K-token library block (1.25x). The 20 lesson
    drafts READ it (0.1x). That only holds if the cached prefix is byte-identical
    across all 21 calls — so everything volatile (module title, lesson objective,
    student brief, course brief) must sit strictly AFTER the cache breakpoint.

    Get it wrong and nothing looks different. The curriculum still generates. It
    just costs ~$7 instead of ~$2.72, and nobody finds out until the invoice.
    """
    _source_with_pages(db, "Book", _BOOK)
    library = build_library_context(db, None)
    shape = plan_shape(20, 1, 50)

    outline_msgs = build_outline_messages(
        title="Blues", brief="a brief", language="el", shape=shape,
        library=library, student_brief="THE STUDENT: Nikos", gap_policy="general_knowledge",
    )
    lesson_a = build_lesson_messages(
        ctx=LessonContext(
            lesson_title="Power chords", lesson_objective="o", module_title="Tone",
            module_objective="mo", course_title="Blues", tier="library",
            position="lesson 1 of 4 in module 1 of 5", minutes=50, teaching_minutes=40,
            target_words=2200, floor_words=1760,
        ),
        library=library, language="el", student_brief="THE STUDENT: Nikos",
        course_brief="a brief",
    )
    lesson_b = build_lesson_messages(
        ctx=LessonContext(
            lesson_title="Barre chords", lesson_objective="different", module_title="Rhythm",
            module_objective="different", course_title="Blues", tier="general_knowledge",
            position="lesson 3 of 4 in module 5 of 5", minutes=50, teaching_minutes=40,
            target_words=2200, floor_words=1760,
        ),
        library=library, language="el", student_brief="THE STUDENT: Nikos",
        course_brief="a brief",
    )

    # The library message is index 1 in all three, and it is the SAME BYTES.
    assert outline_msgs[1] == lesson_a[1] == lesson_b[1]
    assert outline_msgs[1]["cache"] is True

    # And the volatile parts really are different — otherwise this test would pass
    # for the trivial reason that nothing varies at all.
    assert lesson_a[-1]["content"] != lesson_b[-1]["content"]
    assert "Power chords" in lesson_a[-1]["content"]
    assert "Power chords" not in lesson_b[-1]["content"]


def test_nothing_volatile_leaks_into_the_cached_block(db):
    """The failure mode this catches: someone helpfully interpolates the module
    title into the library header ("here is the library, for the module on Tone").
    It reads better and it invalidates the cache on every single call."""
    _source_with_pages(db, "Book", _BOOK)
    library = build_library_context(db, None)

    block = library_message(library)["content"]

    for volatile in ("Power chords", "Nikos", "Blues", "lesson 1 of 4"):
        assert volatile not in block


def test_a_cached_message_only_marks_its_LAST_block(db):
    """`cache_control` marks a breakpoint: everything up to and INCLUDING that block
    is cached. Stamping it on every block would be several breakpoints, which
    Anthropic caps — and the cap is a 400, not a warning."""
    _system, msgs = to_anthropic([
        {"role": "user", "cache": True, "content": "the library"},
        {"role": "user", "content": "the volatile tail"},
    ])
    blocks = msgs[0]["content"]

    assert blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_an_uncached_message_is_untouched_by_the_wire():
    """Every other caller in the app must be byte-for-byte unaffected by the cache
    plumbing — `cache` is opt-in and only `corpus.py` sets it."""
    _system, msgs = to_anthropic([{"role": "user", "content": "hello"}])
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_an_empty_library_still_produces_a_usable_outline_prompt():
    """No sources selected: the model must be told it has nothing of his to read,
    and told to tier honestly — NOT left to infer 'library' from silence."""
    msgs = build_outline_messages(
        title="Blues", brief=None, language="el", shape=plan_shape(20, 1, 50),
        library=LibraryContext(text="", token_count=0, fits=True),
        student_brief=None, gap_policy="general_knowledge",
    )
    body = " ".join(m["content"] for m in msgs)
    assert "no library sources" in body.lower() or "nothing of his" in body.lower()
    assert not any(m.get("cache") for m in msgs)
