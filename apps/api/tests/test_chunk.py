import pytest

from app.brain.chunk import ChunkDraft, chunk_sections
from app.brain.extract import Section


def test_long_section_slides_with_overlap_and_prefers_sentence_boundaries():
    """A single ~5000-char Section must split into several overlapping
    drafts, none of them exceeding target+overlap chars, and the splitter
    must prefer a sentence boundary over a mid-word/mid-sentence hard cut.
    """
    sentence = "Tone shapes how a guitar feels under your fingers and pick attack. "
    text = sentence * (5000 // len(sentence) + 2)
    assert len(text) >= 5000  # this is the "~5000-char single section" case

    target, overlap = 1200, 150
    drafts = chunk_sections(
        [Section(heading="Tone", text=text, page=3)], target_chars=target, overlap_chars=overlap
    )

    assert len(drafts) > 1  # a single long section must produce multiple drafts
    for d in drafts:
        assert len(d.text) <= target + overlap
        assert d.section_path == "Tone"
        assert d.page == 3

    # Consecutive drafts carry overlap_chars of literal shared text.
    for a, b in zip(drafts, drafts[1:]):
        assert a.text[-overlap:] == b.text[:overlap]

    # Every draft but the last should end right at a sentence terminator —
    # proof the splitter preferred the natural boundary over a raw mid-word cut.
    for d in drafts[:-1]:
        assert d.text.rstrip().endswith("."), f"chunk did not end on a sentence boundary: {d.text!r}"

    # Concatenating start..end of drafts (minus overlaps) must reconstruct the
    # original text losslessly - the window walks forward, it never drops or
    # invents characters.
    rebuilt = drafts[0].text
    for d in drafts[1:]:
        rebuilt += d.text[overlap:]
    assert rebuilt == text


def test_long_unbroken_run_falls_back_to_hard_cut_without_hanging():
    """No whitespace or sentence terminator anywhere means there is no natural
    boundary to prefer; the splitter must still make forward progress (no
    hang), keep every draft within target_chars, and lose no characters.
    """
    text = "X" * 2000
    drafts = chunk_sections(
        [Section(heading="Wall of text", text=text, page=None)], target_chars=100, overlap_chars=10
    )

    assert len(drafts) > 1
    assert all(len(d.text) <= 100 for d in drafts)

    rebuilt = drafts[0].text
    for d in drafts[1:]:
        rebuilt += d.text[10:]
    assert rebuilt == text


def test_two_sections_never_mix_text_and_keep_own_heading_and_page():
    """Sections are chunked independently: text from one heading must never
    leak into a draft tagged with the other heading, and each draft carries
    its own section's page through untouched.
    """
    alpha = "Alpha discusses tone woods and body resonance in depth. " * 40
    beta = "Beta discusses pickup winding and magnet types in depth. " * 40

    drafts = chunk_sections(
        [
            Section(heading="Tone Woods", text=alpha, page=1),
            Section(heading="Pickups", text=beta, page=9),
        ]
    )

    alpha_drafts = [d for d in drafts if d.section_path == "Tone Woods"]
    beta_drafts = [d for d in drafts if d.section_path == "Pickups"]

    assert len(alpha_drafts) > 1  # long enough to itself split into multiple drafts
    assert len(beta_drafts) > 1
    assert len(alpha_drafts) + len(beta_drafts) == len(drafts)  # no unexpected section_path

    assert all("Beta" not in d.text for d in alpha_drafts)
    assert all("Alpha" not in d.text for d in beta_drafts)
    assert all(d.page == 1 for d in alpha_drafts)
    assert all(d.page == 9 for d in beta_drafts)


def test_short_section_yields_exactly_one_draft():
    # Lengthened past MIN_CHUNK_CHARS (Plan 13, Stage 4.6): the old 24-char fixture
    # ("Keep your thumb relaxed.") is now, by design, dropped as a junk fragment —
    # so it no longer exercises what this test is about, which is that a section
    # BELOW target_chars produces exactly one draft rather than zero or two.
    text = "Keep your thumb relaxed behind the neck, not hooked over the top of it."
    drafts = chunk_sections([Section(heading="Quick Tip", text=text, page=2)])
    assert drafts == [ChunkDraft(text=text, section_path="Quick Tip", page=2)]


def test_heading_none_and_page_none_pass_through():
    """text/url extraction always produce heading=None, page=None Sections
    (see extract.py) — this must round-trip cleanly, not just PDF's populated case.
    """
    # Past MIN_CHUNK_CHARS — see test_short_section_yields_exactly_one_draft.
    text = "Some untitled prose that runs on for a little while without a heading."
    drafts = chunk_sections([Section(heading=None, text=text, page=None)])
    assert drafts == [ChunkDraft(text=text, section_path=None, page=None)]


def test_empty_or_whitespace_section_yields_no_drafts():
    assert chunk_sections([Section(heading="Empty", text="", page=1)]) == []
    assert chunk_sections([Section(heading="Blank", text="   \n\t  ", page=1)]) == []


def test_no_sections_yields_no_drafts():
    assert chunk_sections([]) == []


def test_zero_overlap_still_prefers_sentence_boundaries():
    """overlap_chars=0 is a legitimate "no carryover" request — it must not also
    disable boundary preference. Regression test for a bug where the backward
    boundary-search width reused overlap_chars, so overlap_chars=0 collapsed the
    search window to empty and forced a raw mid-word hard cut on every split.
    """
    sentence = "Practice scales slowly before increasing tempo for control. "
    text = sentence * (4000 // len(sentence) + 3)

    drafts = chunk_sections(
        [Section(heading="Practice", text=text, page=1)], target_chars=305, overlap_chars=0
    )
    assert len(drafts) > 1

    def ends_on_boundary(t: str) -> bool:
        return bool(t) and (t[-1].isspace() or t.rstrip().endswith("."))

    boundary_count = sum(1 for d in drafts if ends_on_boundary(d.text))
    assert boundary_count / len(drafts) >= 0.8, (
        f"only {boundary_count}/{len(drafts)} drafts ended on a sentence/whitespace "
        "boundary with overlap_chars=0 — boundary preference must not depend on overlap"
    )


def test_non_positive_target_chars_raises():
    with pytest.raises(ValueError):
        chunk_sections([Section(heading="h", text="text", page=1)], target_chars=0)
    with pytest.raises(ValueError):
        chunk_sections([Section(heading="h", text="text", page=1)], target_chars=-10)


def test_negative_overlap_chars_raises():
    with pytest.raises(ValueError):
        chunk_sections([Section(heading="h", text="text", page=1)], overlap_chars=-1)


# --- MIN_CHUNK_CHARS: stop CREATING junk (Plan 13, Stage 4.6) ---------------

def test_a_fragment_below_min_chunk_chars_is_never_created():
    """The live index really contains a 19-character chunk whose entire text is
    "PART ONE The Guitar" — a heading OCR'd off an otherwise-blank page. It scores
    0.64 against real tone queries and has been handed to the model as grounding.

    `retrieve.py`'s floor refuses to RETRIEVE such a chunk. This is the other half:
    it must never enter the corpus at all, where it still distorts BM25's IDF and
    document-length statistics for every other query.
    """
    drafts = chunk_sections([Section(heading=None, text="PART ONE The Guitar", page=1)])
    assert drafts == []


def test_the_other_real_ocr_artifact_is_also_dropped():
    drafts = chunk_sections(
        [Section(heading=None, text="There is no visible text on this page.", page=7)]
    )
    assert drafts == []


def test_a_substantive_section_is_still_chunked():
    """The cut is at "carries no information whatsoever" (40 chars), NOT at the
    relevance floor (200) — the two answer different questions and are deliberately
    different numbers. A 60-char sentence is thin, but it is real text."""
    text = "A humbucker cancels hum by using two coils wound in opposition."
    drafts = chunk_sections([Section(heading=None, text=text, page=1)])
    assert [d.text for d in drafts] == [text]
