"""Chunking: turn extracted Sections into embeddable ChunkDrafts.

Structure-aware: a heading boundary is never crossed — each Section is
windowed independently. Within a Section, a sliding window of ~target_chars
walks the text with overlap_chars of carryover between consecutive drafts,
preferring to cut on a sentence or whitespace boundary over a mid-word split.
"""
from dataclasses import dataclass

from app.brain.extract import Section

# Sentence-ending substrings the splitter looks for, checked longest-match-first
# semantics aren't needed since none is a prefix of another.
_SENTENCE_ENDERS = (". ", "! ", "? ", ".\n", "!\n", "?\n")

# Floor for the backward boundary-search width. Kept independent of overlap_chars
# so that overlap_chars=0 (a legitimate "no carryover" request) doesn't also
# disable the search for a clean sentence/whitespace boundary — see _split_point's
# caller in _chunk_one_section.
_MIN_BOUNDARY_LOOKBACK = 120


@dataclass
class ChunkDraft:
    text: str
    section_path: str | None
    page: int | None


def chunk_sections(
    sections: list[Section], *, target_chars: int = 1200, overlap_chars: int = 150
) -> list[ChunkDraft]:
    """Window every Section's text into ChunkDrafts.

    Never merges text across a Section boundary: each Section is windowed on
    its own, and its heading/page are carried through unchanged as every
    resulting draft's section_path/page. A Section shorter than target_chars
    yields exactly one draft; an empty/whitespace-only Section yields none.
    """
    if target_chars <= 0:
        raise ValueError("target_chars must be positive")
    if overlap_chars < 0:
        raise ValueError("overlap_chars must not be negative")

    drafts: list[ChunkDraft] = []
    for section in sections:
        drafts.extend(_chunk_one_section(section, target_chars, overlap_chars))
    return drafts


def _chunk_one_section(section: Section, target_chars: int, overlap_chars: int) -> list[ChunkDraft]:
    text = section.text
    if not text or not text.strip():
        return []

    drafts: list[ChunkDraft] = []
    n = len(text)
    start = 0
    while start < n:
        ideal_end = start + target_chars
        if ideal_end >= n:
            # Tail of the section (or the whole thing, on the first iteration,
            # when len(text) <= target_chars): take the rest as-is, no need to
            # hunt a boundary — this is also what makes a short Section yield
            # exactly one unmodified-text draft, with no separate case needed.
            end = n
        else:
            # The boundary-search width is deliberately independent of overlap_chars:
            # reusing overlap_chars here would mean overlap_chars=0 (a legitimate "no
            # carryover" request) also disables boundary preference, forcing a hard
            # mid-word cut on every split even when clean boundaries exist nearby.
            lookback = max(overlap_chars, target_chars // 5, _MIN_BOUNDARY_LOOKBACK)
            end = _split_point(text, start, ideal_end, lookback)

        drafts.append(ChunkDraft(text=text[start:end], section_path=section.heading, page=section.page))
        if end >= n:
            break

        next_start = end - overlap_chars
        start = next_start if next_start > start else end
    return drafts


def _split_point(text: str, start: int, ideal_end: int, lookback: int) -> int:
    """Choose a cut point in (start, ideal_end], preferring one that lands
    right after a sentence terminator or, failing that, a whitespace run —
    searched within `lookback` chars before ideal_end. Falls back to a hard
    cut at ideal_end (mid-word) only when no boundary is found in that range.
    """
    earliest = max(start, ideal_end - lookback)
    window = text[earliest:ideal_end]

    best_idx, best_len = -1, 0
    for terminator in _SENTENCE_ENDERS:
        idx = window.rfind(terminator)
        if idx > best_idx:
            best_idx, best_len = idx, len(terminator)
    if best_idx != -1:
        return earliest + best_idx + best_len  # split right after the terminator

    idx = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
    if idx != -1:
        return earliest + idx + 1  # split right after the whitespace

    return ideal_end
