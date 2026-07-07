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
    drafts = chunk_sections([Section(heading="Quick Tip", text="Keep your thumb relaxed.", page=2)])
    assert drafts == [ChunkDraft(text="Keep your thumb relaxed.", section_path="Quick Tip", page=2)]


def test_heading_none_and_page_none_pass_through():
    """text/url extraction always produce heading=None, page=None Sections
    (see extract.py) — this must round-trip cleanly, not just PDF's populated case.
    """
    drafts = chunk_sections([Section(heading=None, text="Some untitled prose.", page=None)])
    assert drafts == [ChunkDraft(text="Some untitled prose.", section_path=None, page=None)]


def test_empty_or_whitespace_section_yields_no_drafts():
    assert chunk_sections([Section(heading="Empty", text="", page=1)]) == []
    assert chunk_sections([Section(heading="Blank", text="   \n\t  ", page=1)]) == []


def test_no_sections_yields_no_drafts():
    assert chunk_sections([]) == []
