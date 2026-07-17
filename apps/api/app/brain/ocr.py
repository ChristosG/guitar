"""Transcribe pending Page scans, then chunk + embed each page's text.

Per-page commit is the whole point (spec D5): a `vision()` timeout on page 60
must not cost us pages 1-59. Each page gets ONE automatic retry, then is
marked `failed` and the job moves on. The tutor is never asked to fix OCR by
hand — Chris was explicit that he should not have to care about this.

`chunk_sections` and `retrieve.py` are untouched: this bolts onto the tested
pipeline rather than replacing it. The one addition is `Chunk.page_id`, which
is what turns a retrieved chunk into a citation you can actually open.
"""
import logging
import os
import re
import unicodedata
from dataclasses import dataclass

import fitz
from sqlalchemy import func, or_

from app.brain.chunk import chunk_sections
from app.brain.extract import Section
from app.brain.paginate import RENDER_DPI, source_pdf_path
from app.brain.textlayer import looks_like_ocr_garbage
from app.config import settings
from app.llm.embed_factory import get_embedder
from app.llm.errors import LLMError
from app.llm.factory import get_ocr_provider
from app.models.generation_job import GenerationJob
from app.models.knowledge import Chunk, KnowledgeSource, Page
from app.prompts import overrides
from app.prompts.overrides import resolve
from app.settings_store import resolve_llm_config

log = logging.getLogger(__name__)

# --- what we ask the model, and why ---------------------------------------
#
# THE FIGURE REGION. A description of a picture is NOT a sentence from the book,
# and once both are in `page.text` nothing downstream can tell them apart by
# reading them — the canon compile (Part B) and `retrieve.py` both quote this
# text back to the tutor as the book's own words. Quoting our description of a
# photo as if the author wrote it is a fabricated citation with a real page
# number on it, which is worse than having no description at all.
#
# THE CONTRACT, and it is TOTAL — every character of a page is classified, and
# no reader ever has to guess:
#
#     text INSIDE a [FIGURE]...[/FIGURE] region is OURS;
#     everything outside one is the PAGE'S OWN WORDS;
#     an unterminated [FIGURE] runs to the end of the text.
#
# `book_text()` IS that rule, executable. Call it; do not re-derive it.
#
# WHY IT IS DELIMITED AND NOT POSITIONAL. This contract used to read "everything
# from the first marker on is ours". That is true in `describe` mode — the
# publisher's text comes first and every marker follows it — and FALSE on the
# transcribe path, which is 845 of the tutor's 888 pages: `OCR_PROMPT` asks for
# markers INTERLEAVED in reading order, because that is where the pictures
# actually are, so the author's prose RESUMES after a marker. With no terminator
# both readings were wrong, and the second one is the failure this whole module
# exists to prevent:
#
#     split at the first marker  -> the book's real prose is discarded as ours;
#     take the marker's line only -> a multi-line description's continuation is
#                                    read as the book's words, and gets cited as
#                                    a verbatim quotation with a real page number.
#
# A region has an END, so a description is bounded WHEREVER it appears — which
# makes the same rule true in both modes rather than in the one mode that had
# tests. The last clause ("an unterminated [FIGURE] runs to the end") is what
# keeps the rule total for the 43 pages of Powers already in his library, which
# were written in the old format: there, "from the marker on is ours" was true,
# and this resolves them to exactly that — the only direction that cannot
# fabricate a citation.
#
# ASCII and greppable on purpose: a human reading the Reader sees what it means,
# and it survives a model that decides to reformat the markdown around it.
FIGURE_MARKER = "[FIGURE]"
FIGURE_END = "[/FIGURE]"

# Non-greedy, DOTALL: a region runs from an opener to the FIRST closer after it,
# across the newlines a multi-line description is full of. A stray opener nested
# inside a well-formed region is harmless and deliberately not special-cased —
# it sits in text this rule already calls ours.
_FIGURE_REGION = re.compile(
    re.escape(FIGURE_MARKER) + r".*?" + re.escape(FIGURE_END), re.DOTALL
)
_ANY_FIGURE_MARKER = re.compile(
    f"{re.escape(FIGURE_MARKER)}|{re.escape(FIGURE_END)}"
)


def book_text(text: str) -> str:
    """The page's OWN words: everything outside a [FIGURE]...[/FIGURE] region.

    THE CONTRACT'S ONE READER — for `_publisher_text` here, and for Part B's
    canon compile, which is the caller this was written for. It quotes this text
    back to the tutor as the author's sentences, so anything this function gets
    wrong becomes a citation he will click on and trust.

    Substitutes a newline rather than "" for a lifted region: a figure marked up
    inline would otherwise weld the words either side of it into one word that
    appears in neither the book nor our description.
    """
    outside = _FIGURE_REGION.sub("\n", text or "")
    # The totality clause. An opener with no closer left standing after the
    # well-formed regions are gone owns the rest of the text — the fail-safe
    # direction: mistaking the book's prose for our description costs a
    # paragraph, mistaking our description for the book's prose is a fabricated
    # quotation. Only legacy `describe` text can reach this — a transcription
    # whose markup does not close is refused outright (`_UnclosedFigureRegion`).
    head, marker, _ours = outside.partition(FIGURE_MARKER)
    return (head if marker else outside).strip()


def _figure_markup_is_closed(text: str) -> bool:
    """True when every marker in `text` belongs to a well-formed region — i.e.
    when the contract can be applied to it without the totality clause having to
    rescue anything.

    Removes the well-formed regions and asks whether any marker survived: one
    check that catches an unterminated opener, an orphaned closer, and a crossed
    pair alike.
    """
    return not _ANY_FIGURE_MARKER.search(_FIGURE_REGION.sub("", text))

# THE TRANSCRIBE PROMPT — for a page with no trustworthy text (`inherited_ocr`,
# `no_text_layer`).
#
# The old wording ("Transcribe ALL text ... do not invent any text that is not
# visibly present") inherited TESSERACT'S STRUCTURAL BLIND SPOT: an OCR layer
# cannot describe a PICTURE, so it doesn't, and neither did we. But these are
# books about guitar TONE. The photo of a dialed-in amp face IS the lesson;
# Hunter p.57 (knobs labelled VOLUME, MASTER) produced literally nothing from
# Tesseract, and under the old prompt a vision model would have agreed with it —
# correctly, because there is no *text* on that page to speak of. A page that
# reads as blank is a page the curriculum cannot teach from.
#
# Point 3 is the answer to `claude -p`, which returns chatty markdown around a
# transcription: it narrates that it zoomed in, it adds its own headers. The
# bridge deliberately scrubs none of it (Task 4) — and it should not, because a
# regex aimed at a model's prose eventually eats a line of a real page with it.
# Instructing the shape is the only fix that cannot corrupt the content.
OCR_PROMPT = (
    "You are reading one page of a printed guitar instruction book, for a "
    "library the book's own words will be quoted from. Reply with the page's "
    "content and nothing else.\n"
    "\n"
    "1. TEXT. Transcribe all text on the page verbatim, in reading order — "
    "headings, body, captions, sidebars, table text, page numbers. Do not "
    "summarize, do not translate, do not correct the author, and do not invent "
    "any text that is not visibly present. Small glyphs are content, not noise: "
    "fractions (¼ ½ ¾ ⅓ ⅔ ⅛), sharps and flats (♯ ♭), primes (′ ″) and "
    "superscripts must be transcribed as they are printed, never flattened to "
    "the nearest plain digit or letter.\n"
    "2. PICTURES. A photograph, diagram, illustration, chord box, tab staff or "
    "table is content too, and no transcription describes one. Where each one "
    f"appears in reading order, open a {FIGURE_MARKER} line, describe plainly "
    "what it shows and reproduce any text, numbers or labels printed inside it, "
    f"then close it with a {FIGURE_END} line before the page's text resumes. "
    "Describe only what is visible — if you cannot make it out, say that "
    "instead of guessing.\n"
    f"3. CLOSE EVERY FIGURE. {FIGURE_MARKER} and {FIGURE_END} are how a reader "
    "downstream tells your words from the author's: what you write BETWEEN them "
    "is a description of a picture, and everything OUTSIDE them is quoted back "
    "to a teacher as the author's own sentences, with this page's number on it. "
    f"So a {FIGURE_MARKER} you never close turns the rest of the page into a "
    "quotation the author never wrote. Every one gets its closing line, even "
    "the last figure on the page.\n"
    "4. SHAPE. No preamble, no sign-off, no commentary about how you read the "
    "page, no markdown fences, no headings of your own. Begin with the page's "
    f"first word, or with {FIGURE_MARKER} if the page is all picture. If the page "
    "is genuinely blank, reply with nothing at all."
)

# THE DESCRIBE PROMPT — for an `image_region` page: a DIGITAL page whose text
# layer is the publisher's own real font (correct, and already ours for free),
# carrying raster art that text layer cannot describe. Powers has 43 of them.
#
# It must never ask for a transcription. Asking would buy a vision call to
# re-derive text we already hold — this plan's top severity class — and the
# answer would then be appended to the very text it duplicates.
FIGURE_SLICE_ID = "ocr.figure"
OCR_SLICE_ID = "ocr.transcribe"

FIGURE_PROMPT = (
    "You are looking at one page of a printed guitar instruction book. Its text "
    "has already been captured perfectly and is NOT your job — do not transcribe "
    "the page.\n"
    "\n"
    "Describe the pictures on it: each photograph, diagram, illustration, chord "
    "box, tab staff or table. Say plainly what each one shows, and reproduce any "
    "text, numbers or labels printed inside it (a knob's setting, a fret number, "
    "a string name) — those are the parts a reader cannot get from the words "
    "around them. For a tab or chord diagram, give the notation itself: strings, "
    "frets, fingerings, and the order they are played in.\n"
    "\n"
    "Describe only what is visible. Do not invent detail you cannot make out, "
    "and do not explain, teach, or comment on the music.\n"
    "\n"
    f"Open each picture with {FIGURE_MARKER} on its own line and close it with "
    f"{FIGURE_END} on its own line. Those lines are how a reader downstream tells "
    "your description of a picture from the sentences the author actually wrote "
    "on this page. No preamble, no sign-off, no commentary about how you read "
    "the page, no markdown fences. If the page has no picture on it at all, "
    "reply with nothing at all."
)

_MAX_ATTEMPTS = 2       # initial + one retry

# vision()'s max_tokens=4000 (app/llm/qwen.py) bounds output length, but
# vision() does not surface finish_reason (see its docstring) — so a response
# cut off mid-generation is, at this seam, indistinguishable from a complete
# one that just happens to be long. Left unguarded, a truncated page would be
# committed as `ready` and silently corrupt the citation store (a citation
# that looks verbatim but stops mid-sentence is worse than one flagged
# `failed`, since nothing downstream has a way to know to distrust it).
#
# Heuristic: ~4 chars/token is a reasonable average for English prose, so a
# genuinely complete transcription of a single printed page essentially never
# reaches 4000 tokens' worth of characters (~1127 prompt tokens/page at
# 110dpi per qwen.py's own measurement, and output text is rendered from the
# same page, not generated de novo). 12,000 chars (~3000 tokens, 75% of the
# ceiling) is comfortably past any normal page while still catching a
# response that ran into the wall. False positives cost one extra retry;
# false negatives are the status quo this guard exists to reduce, not
# eliminate — it is cheap insurance, not a proof.
_SUSPECTED_TRUNCATION_CHARS = 12_000

# Page-level pickup cap (Stage 7.4). Counts PICKUPS (`Page.ocr_attempts`), not
# vision() calls — each pickup already gets `_MAX_ATTEMPTS` tries of its own.
# Three exists to make re-picking-up `empty` pages affordable: an OCR'd-to-empty
# page used to be dead forever (the old pickup filter was pending/failed/
# ocr_running only), so a one-off "" from the model permanently lost a page of
# the book; retrying it on every run instead would re-bill the book's genuinely
# blank pages — a real 77-page scan has several — every single time the tutor
# pressed Retry. After three honest attempts a page is left alone.
MAX_PAGE_ATTEMPTS = 3

# Which page statuses `ocr_source` re-picks-up. "ocr_running" is here so a page
# orphaned by a hard process kill mid-vision() (which never got to write `failed`)
# resumes on the next run instead of being stuck forever; "empty" is here for the
# reason above. Safe to re-pick-up any of them: re-OCRing a page is idempotent
# (`_embed_page` deletes that page's existing chunks before re-adding them).
PICKUP_STATUSES = ("pending", "failed", "ocr_running", "empty")

# The quality gate (Stage 7.4). A vision model asked to transcribe a page it
# cannot read does not fail — it NARRATES. These are the shapes that answer,
# lifted from what actually turned up in the real library (retrieve.py's floor
# quotes the same 38-char "There is no visible text on this page." junk chunk,
# which was embedded, indexed, and cited to the tutor before anything screened
# for it). Matched only against a SHORT response — see `_looks_like_no_text`.
_NO_TEXT_PATTERNS = re.compile(
    r"(no (visible|readable|legible|discernible)?\s*text"
    r"|nothing (is )?(visible|readable|written)"
    r"|(this |the )?page (is|appears) (blank|empty)"
    r"|blank page"
    r"|δεν υπάρχει (ορατό )?κείμενο"
    r"|κενή σελίδα)",
    re.IGNORECASE,
)

# A page's response is screened for a "no visible text" sentinel only if it is at
# most this long. THE POINT OF THE BOUND: a genuinely short page with real content
# ("Chapter 3", a part title, a photo caption) must never be screened out — that
# would turn a healthy book amber, which is the exact failure mode Stage 7.4 was
# warned about. A model's refusal narration is one sentence; a real page that
# happens to also contain the phrase "no visible text" (a book about OCR, say)
# will be far longer than this.
_NO_TEXT_MAX_CHARS = 200

# Unicode-garbage screen. Mojibake and a mis-decoded scan produce long runs of
# symbol/private-use/replacement codepoints; real prose in any language this app
# serves (English, Greek) is overwhelmingly letters, digits, whitespace and
# punctuation. Only applied above a length where the ratio means anything at all:
# a 12-char page of pure musical symbols is not evidence of a broken read.
_GARBAGE_RATIO = 0.30
_GARBAGE_MIN_CHARS = 120


class _SuspectedTruncation(RuntimeError):
    """Raised when a vision() response is long enough to plausibly have hit
    the max_tokens ceiling. Routed through the same one-retry-then-`failed`
    path as any other vision() error (see module docstring): at
    temperature=0.0 a retry may reproduce the same output, but failing closed
    beats silently trusting text that might stop mid-page. A `failed` page is
    picked up again by a future `ocr_source()` run, same as any other
    failure — there is still no manual-edit path."""


class _UnclosedFigureRegion(RuntimeError):
    """A TRANSCRIPTION whose figure markup does not close — an opener with no
    `[/FIGURE]` after it, an orphaned closer, a crossed pair.

    THE ONE SHAPE THE CONTRACT CANNOT RESOLVE WITHOUT GUESSING, on the one path
    where guessing costs something. The totality clause says an unterminated
    marker runs to the end of the text; on a transcribed page that is very
    possibly the author's prose, silently discarded. The other reading — the
    description ends at its line — cites our photo caption as his sentence.
    Nothing in the response distinguishes them.

    So it is routed like any other unreadable page: one retry, then `failed` —
    amber, retryable, and visible to the tutor. A page we could not parse is
    cheap; a page we parsed wrongly is permanent, silent, and green.

    `describe` mode never raises this, and that asymmetry is a decision rather
    than an oversight: there, `FIGURE_PROMPT` forbids transcription outright, so
    every word of the response IS ours and "the region ends where the response
    ends" is a fact rather than a guess. `_marked` closes it and the page stays
    `ready` — see `_VisionTask.screens_figure_markup`."""


class _GarbageTranscription(RuntimeError):
    """The response was long enough to judge and is not a transcription of a
    page. TWO detectors raise this, because there are two ways to get here and
    they are the same verdict:

      `_looks_like_garbage`      — mostly not LANGUAGE (mojibake, a mis-decoded
                                   image): symbol/private-use codepoints by
                                   unicode category.
      `looks_like_ocr_garbage`   — mostly not PROSE (`brain/textlayer.py`): a
                                   wall of 1-2 character stubs, which is what a
                                   page damaged in the PDF itself produces. Kahn
                                   p.40 renders blank in BOTH MuPDF and poppler
                                   and Tesseract wrote 544 chars of "7 ipgges x
                                   a Rar ek en ee" for it — 9 of 617 pages across
                                   the three scanned books are like this, and
                                   today's pipeline ingests every one of them as
                                   page content.

    Routed through the same one-retry-then-`failed` path as any other vision()
    error: a page we could not read is a FAILED page (amber, with a retry), not
    an `empty` one (which would silently roll up green), and above all not a
    `ready` one — which is what it is today."""


def _looks_like_no_text(text: str) -> bool:
    """True for a model NARRATING that the page has nothing on it, rather than
    transcribing it. Such a page is `empty` — a true fact about the book, not a
    failure to read it — so it must not be counted against the source's health.

    The length bound is load-bearing; see `_NO_TEXT_MAX_CHARS`.
    """
    return len(text) <= _NO_TEXT_MAX_CHARS and bool(_NO_TEXT_PATTERNS.search(text))


def _looks_like_garbage(text: str) -> bool:
    """True when a long-enough response is mostly not letters/digits/whitespace/
    punctuation — the signature of mojibake or a mis-decoded image, which must
    never reach the index as if it were the tutor's book.

    Unicode CATEGORIES, not an ASCII allowlist: Greek is a first-class language
    here, and `str.isascii()`-style screens fail an entire Greek corpus.
    """
    if len(text) < _GARBAGE_MIN_CHARS:
        return False
    junk = sum(
        1 for ch in text
        if not (ch.isspace() or unicodedata.category(ch)[0] in ("L", "N", "P"))
    )
    return junk / len(text) > _GARBAGE_RATIO


@dataclass
class OcrResult:
    total: int
    ready: int
    failed: int


# --- WHICH question this page is being sent to vision to answer -------------

def _publisher_text(page: Page) -> str:
    """The page's OWN words as currently stored — never our description of its
    pictures.

    Strips every `[FIGURE]...[/FIGURE]` REGION because a page can be picked up
    more than once (an embed hiccup, a resumed run, an explicit re-read), and
    what it must merge onto EVERY time is the publisher's text — not the previous
    run's output, which would stack a description onto a description onto a
    description. Idempotent by construction: the only writer of those markers is
    `_VisionTask.merge`.

    Regions, not "everything from the first marker on" — which was correct only
    because this is called on the describe path today, and would have thrown away
    the back half of a transcribed page the moment anything called it on one. The
    contract holds in both modes now, so this holds for whoever calls it next.
    """
    return book_text(page.text or "")


@dataclass(frozen=True)
class _VisionTask:
    """What to ask about one page, and what its answer means.

    A page's answer is not interchangeable with another page's: for a scan it IS
    the page, for a Powers tab page it is an ADDITION to a page we already have.
    Making that a value decided once, up front, is what keeps the difference out
    of the retry loop — where it would be an `if` inside error handling, read by
    nobody, in the exact place this module can least afford one.
    """
    mode: str                   # "transcribe" | "describe" — for the log line
    prompt: str
    preserved_text: str         # publisher text this page must not lose ("" if none)
    screens_ocr_garbage: bool
    screens_figure_markup: bool

    def merge(self, response: str) -> str:
        """This page's final text. MERGE, never overwrite.

        `describe` mode APPENDS, and an empty response is not a blank page — it
        is a page with no picture worth describing, whose publisher text is
        untouched and perfectly good. The unconditional `page.text = text or
        None` this replaces would have blanked it and rolled it up `empty`: a
        page of the tutor's book deleted by a model shrug.
        """
        if self.mode == "transcribe":
            return response
        if not response:
            return self.preserved_text
        return f"{self.preserved_text}\n\n{_marked(response)}"

    def text_source(self, reader: str, *, described: bool) -> str:
        """Whose words `merge` just produced.

        A merged page is genuinely BOTH: the publisher wrote the text, the model
        wrote the picture description under the marker. "claude" alone would
        claim a transcription that never happened — the same lie the merge gap
        told, told by the column instead of the text. "text_layer" alone would
        hide that a model put content on the page at all, which is precisely the
        question this column exists to answer. So it says both, and the
        `[FIGURE]` marker says which is which, positionally, in the text itself.

        Clamped to the column's width: a 12-hour run must not die on its last
        page because someone added a provider with a long name.
        """
        if self.mode == "transcribe":
            return reader[:_TEXT_SOURCE_MAX]
        if not described:
            return TEXT_LAYER_SOURCE        # nothing was described; the text is untouched
        return f"{TEXT_LAYER_SOURCE}+{reader}"[:_TEXT_SOURCE_MAX]


def _marked(description: str) -> str:
    """Guarantee a `describe` response is BOUNDED by our markers, however the
    model marked it up — or didn't.

    An unmarked description is indistinguishable from the book's words. A
    HALF-marked one is worse: it reads as the book's words from wherever the
    model stopped writing markup, which is a fabricated quotation with a real
    page number, arriving quietly. So the contract cannot rest on compliance
    here, and it does not have to: `FIGURE_PROMPT` forbids transcription, so
    EVERY word of a describe response is ours, and wrapping the whole of it from
    first character to last is a true statement about it no matter what shape it
    came back in.

    A compliant response (regions, nothing outside them) is returned untouched,
    keeping the model's own per-picture split. Only a non-compliant one is
    re-wrapped as a single region, and only there are stray markers dropped —
    that is not the prose-scrubbing this module refuses elsewhere: it removes OUR
    OWN sentinel from text that is entirely ours, changes no attribution, and
    cannot eat a line of a real page, because the page's own words are never in a
    describe response.
    """
    text = description.strip()
    if not text:
        return text
    outside_any_region = _FIGURE_REGION.sub("", text).strip()
    if not outside_any_region:
        return text            # already nothing but well-formed regions — compliant
    return f"{FIGURE_MARKER}\n{_ANY_FIGURE_MARKER.sub('', text).strip()}\n{FIGURE_END}"


def _vision_task_for(page: Page, prompts=None) -> _VisionTask:
    """THE BRANCH THIS MODULE DID NOT HAVE, and whose absence was live.

    `ocr_source` selected pages on `Page.status` alone and never read
    `ocr_reason`, then did `page.text = text or None` unconditionally. Task 3b
    creates 43 `image_region` pages (Powers' tab pages) that KEEP their correct
    publisher text and are queued for vision ONLY so the tab picture gets
    described. On first pickup, every one of them would have (a) bought a vision
    call to re-transcribe text we already had for free — this plan's top severity
    class — and (b) silently DISCARDED that publisher text in favour of the
    transcription, leaving `text_source` NULL forever. Confident, permanent,
    silently wrong: the exact failure the whole plan exists to fix.

    BOTH conditions are checked, and the conjunction is deliberate:

      `ocr_reason == "image_region"` is the routing decision, but it is not a
      permanent record — a garbage transcription overwrites it with
      "ocr_garbage" (a documented value of the column), so it cannot be the sole
      witness forever.

      `preserved` — text we did not write — is the independent one. A page with
      no text has nothing to lose and is transcribed whole, which is also the
      right answer for every page ingested before `ocr_reason` existed: it is
      NULL there, and NULL must never silently mean "describe" (an inherited
      Tesseract layer taken as gospel is where this started).
    """
    preserved = _publisher_text(page)
    if page.ocr_reason == "image_region" and preserved:
        # `screens_figure_markup` is False for the reason `_marked`'s docstring
        # gives: every word of a describe response is ours, so unclosed markup is
        # repairable here without guessing at anything, and failing the page
        # instead would only throw away a correct description of one of Powers'
        # 43 tab pages. On the transcribe path the same repair WOULD be a guess —
        # see `_UnclosedFigureRegion`.
        return _VisionTask(mode="describe",
                           prompt=resolve(prompts, FIGURE_SLICE_ID, FIGURE_PROMPT),
                           preserved_text=preserved, screens_ocr_garbage=False,
                           screens_figure_markup=False)
    # `screens_ocr_garbage` is False above, and that is a decision, not an
    # oversight: `looks_like_ocr_garbage` judges a TRANSCRIPTION against the
    # shape of real prose, and a description of a tab diagram is neither — it is
    # inherently full of short tokens ("5 7 5", "E A D G B E"). Its own docstring
    # warns that a false positive there THROWS AWAY correct content and marks the
    # page failed, and the pages it would throw away are precisely Powers' 43 tab
    # pages, which is what `image_region` exists for. Mojibake
    # (`_looks_like_garbage`, a unicode-category screen) still applies to both.
    return _VisionTask(mode="transcribe",
                       prompt=resolve(prompts, OCR_SLICE_ID, OCR_PROMPT),
                       preserved_text="", screens_ocr_garbage=True,
                       screens_figure_markup=True)


# --- WHOSE words these are --------------------------------------------------

# `models/knowledge.py`: `Page.text_source` is `String(20)`. A column width is a
# fact about the bytes on disk (see `EMBED_DIM`'s comment for the last time this
# app learned that), so the value is clamped to it here rather than trusted to
# stay short by convention.
_TEXT_SOURCE_MAX = 20

# `paginate.py` stamps this on a clean digital page: the publisher's own embedded
# font, taken for free. It is also HALF the answer for an `image_region` page —
# whose text is the publisher's and whose figure description is the model's.
TEXT_LAYER_SOURCE = "text_layer"

# `Page.text_source`, by configured provider. Derived from the CONFIG rather than
# sniffed off the provider object, because the column answers "whose words are
# these" and the only thing that knows is the config that chose the model.
#
# `claude_cli` and `claude` both map to "claude": they are the same model bought
# from two different wallets (a subscription via the CLI bridge vs. API tokens —
# see `config.py`). The tutor is asking which MODEL read his book; "claude_cli"
# would be answering a question about billing.
_TEXT_SOURCE_BY_PROVIDER = {"claude": "claude", "claude_cli": "claude", "qwen": "qwen"}


def _current_text_source() -> str:
    """Which model is doing this run's reading. Resolved once per run.

    `resolve_llm_config(settings.ocr_provider)` — NOT the zero-arg chat
    resolution — because this labels the provider `get_ocr_provider()` actually
    dispatched to. When `OCR_PROVIDER` is unset the two are identical
    (`provider or settings.llm_provider` falls through), so every existing
    install's provenance is unchanged; when it is set, this is what stops the
    column claiming chat's provider read a page that OCR's provider did.

    Falls back to the raw provider name rather than a guess: a provider this map
    has never heard of is a fact worth recording honestly, and every value here
    is written to a `String(20)` column that nothing parses.
    """
    try:
        provider = resolve_llm_config(settings.ocr_provider).provider
    except Exception:            # noqa: BLE001 — provenance must never fail a run
        log.warning("ocr: could not resolve the provider name for text_source", exc_info=True)
        return "unknown"
    return _TEXT_SOURCE_BY_PROVIDER.get(provider, provider)


def ocr_source(db, source_id) -> OcrResult:
    # Pages this run will touch: a non-terminal/retryable status (PICKUP_STATUSES)
    # that has not already burned its attempt budget (MAX_PAGE_ATTEMPTS). The
    # attempt cap is what lets `empty` be retryable without re-billing the book's
    # blank pages on every run — see both constants' docstrings.
    #
    # The `IS NULL` branch is not decoration: the column is NOT NULL going forward
    # (migration d4f1a90c7b28 backfills 0), but in SQL `NULL < 3` is NULL, not
    # TRUE — so if a row ever did carry NULL here, a bare `<` would silently drop
    # that page out of every OCR run for good, which is precisely the class of
    # never-retried-again bug this filter exists to end.
    pages = (
        db.query(Page)
        .filter(Page.source_id == source_id,
                Page.status.in_(PICKUP_STATUSES),
                or_(Page.ocr_attempts.is_(None),
                    Page.ocr_attempts < MAX_PAGE_ATTEMPTS))
        .order_by(Page.page_no)
        .all()
    )
    # Two seams now, not one: the vision model that READS the page (Claude/Qwen,
    # remote) and the embedder that INDEXES it (local CPU). They were the same
    # object while one vLLM box served both; they are not any more.
    provider = get_ocr_provider()
    embedder = get_embedder()
    reader = _current_text_source()          # which model is doing this run's reading
    ready = failed = 0
    # The tutor's prompt overrides, resolved ONCE for the whole run rather than per
    # page. Two reasons, and the second is the one that matters: a `resolve(db, ...)`
    # inside the loop would be a query per page — 888 of them on this library — and it
    # would sit between `db.commit()` and the vision call, holding a pool connection
    # across every one. The prompt cannot change mid-run anyway; a book read under two
    # different prompts because he saved an edit at page 400 would be worse than
    # either prompt alone.
    prompts = overrides.snapshot(db)

    for page in pages:
        if page.image_path is None:
            # D2 degenerate page (url/text/note sources get exactly one Page
            # row with image_path=NULL): there is nothing to OCR here — any
            # text it has came from extraction, not vision(). Marking it
            # `failed` would be worse than doing nothing: `failed` pages are
            # re-picked-up by this same query, so it would fail forever with
            # a cryptic os.path.join(..., None) error on every run.
            continue

        page.status = "ocr_running"
        # Incremented BEFORE the call, and committed with the "ocr_running"
        # status, so it counts attempts that were MADE — a page whose vision()
        # call hard-kills the process still spent an attempt, and must not be
        # able to spend an unbounded number of them by never getting to record
        # any (`ocr_running` is itself a pickup status).
        page.ocr_attempts = (page.ocr_attempts or 0) + 1
        # In the SAME transaction as the status write, matching this module's
        # existing "status and chunks commit together" discipline. See the
        # function's docstring for why this is here rather than on each of the
        # three terminal paths that would otherwise each need it.
        _evict_refused_chunks(db, page)
        db.commit()
        # Decided BEFORE the call and from the page's committed state, so the
        # question we ask and the text we must not lose are one value, fixed for
        # this pickup, rather than something re-derived from a row that the
        # error paths below rollback and re-fetch.
        task = _vision_task_for(page, prompts)
        try:
            response = _transcribe_with_retry(provider, page, task)
        except LLMError as e:
            if e.kind == "rate_limit":
                # THE 429 RULE, same as the draft fan-out's: a rate limit is
                # not a bad page. Refund the attempt (it must not burn the
                # permanent budget), put the page back to pending, and STOP
                # the run — every page after this one is about to hit the
                # same limit, and a 77-page book can otherwise exhaust its
                # entire per-page budget inside one rate-limit window,
                # becoming permanently un-OCR-able with a Retry button that
                # silently does nothing.
                db.rollback()
                page = db.get(Page, page.id)
                page.ocr_attempts = max(0, (page.ocr_attempts or 1) - 1)
                page.status = "pending"
                page.ocr_error = "rate limited — retry in a few minutes"
                db.commit()
                log.warning("ocr: rate-limited at page %s — stopping this run; "
                            "the remaining pages stay pending", page.page_no)
                break
            log.warning("ocr: page %s failed permanently", page.page_no, exc_info=True)
            _mark_failed(db, page, e, task)
            failed += 1
            continue
        except Exception as e:
            log.warning("ocr: page %s failed permanently", page.page_no, exc_info=True)
            _mark_failed(db, page, e, task)
            failed += 1
            continue

        # The quality gate (Stage 7.4), in the one place that can still tell the
        # difference between "this page has nothing on it" and "we failed to read
        # this page". Getting that distinction wrong in either direction is a lie
        # the tutor pays for: an `empty` page rolls the source up GREEN, a
        # `failed` one turns it AMBER with a retry.
        #
        # Screens the model's RESPONSE, before the merge — not the merged text.
        # In `describe` mode a "no visible text" narration means "no picture worth
        # describing", which is a fact about our question and NOT a verdict on the
        # publisher text sitting on the page; `merge` turns the "" back into that
        # text and the page stays `ready`.
        if _looks_like_no_text(response):
            log.info("ocr: page %s narrated 'no visible text' — recording empty",
                     page.page_no)
            response = ""
        text = task.merge(response)

        page.text = text or None
        page.ocr_error = None
        # Never NULL after a pickup. NULL means "written before anyone tracked
        # this" — a page we just read is not that, and it is the only way the
        # tutor can tell a page Claude read from a page still carrying someone
        # else's Tesseract.
        page.text_source = task.text_source(reader, described=bool(response))

        if not text:
            page.status = "empty"
            # An `empty` page has no text, so it has no author. Naming a model
            # here would claim a transcription that does not exist.
            page.text_source = None
            db.commit()
            continue

        # Embed BEFORE committing `ready` — and commit the page's status
        # together with its chunks in one transaction. This is what makes
        # "status==ready implies chunks exist" hold: a page can never be
        # durably ready with zero chunks, because the only commit that sets
        # status="ready" is the same commit that persists those chunks.
        #
        # An embed() failure gets the SAME per-page handling as a transcribe
        # failure: mark this page `failed`, record why, and continue the
        # batch — an embedding-service hiccup on page 60 must not cost pages
        # 1-59, and must not silently leave page 60 "ready" with nothing to
        # cite.
        try:
            n_chunks = _embed_page(db, embedder, page)
        except Exception as e:
            log.warning("ocr: page %s embed failed", page.page_no, exc_info=True)
            _mark_failed(db, page, f"embedding failed: {e}", task)
            failed += 1
            continue

        if n_chunks == 0:
            # Non-empty transcription that chunk_sections still couldn't
            # turn into anything chunkable (e.g. whitespace-only after
            # normalization). There's nothing to cite, so this can't be
            # `ready` either — that would repeat the same "ready with zero
            # chunks" bug this fix exists to close.
            page.status = "empty"
            db.commit()
            continue

        page.status = "ready"
        db.commit()                       # page + its chunks, one transaction
        ready += 1

    _rollup_source_status(db, source_id)

    return OcrResult(total=len(pages), ready=ready, failed=failed)


def _evict_refused_chunks(db, page: Page) -> None:
    """THE INVARIANT: A PAGE WHOSE `text` IS NULL HAS NO CHUNKS.

    `paginate_source` refuses a GlyphLessFont layer — `Page.text=None`, someone
    else's Tesseract, every fraction glyph gone. `ingest_source` then re-opens the
    same PDF bytes, re-extracts THE VERY LAYER it just refused, and chunks it
    against those same pending pages; `retrieve.search` joins Chunk->Page only to
    resolve a page number and never filters on status, so that text is fully
    retrievable and citable. One half of the pipeline refuses it and the other
    adopts it, thirty lines apart.

    CALLED AT PICKUP, and that is the whole design. The obvious fix is to delete
    on each terminal path — the `empty` branch, `_mark_failed`, the 429 park — but
    that is three places that must never drift, in a module whose failure paths
    are exactly where drift hides. Evicting when we take the page instead makes
    every one of those paths correct with no code of their own: by the time any of
    them runs, the refused chunks are already gone and committed, and the ONLY
    thing that ever puts chunks back is `_embed_page` on the success path. The
    two halves agree by construction rather than by three reminders.

    `page.text is None` IS THE TEST, not `status != "ready"`, and the difference
    is load-bearing: an `image_region` page keeps correct PUBLISHER text
    (paginate.py) and the chunks that go with it. Judging by status would strip
    Powers' 43 tab pages out of the index the moment a figure description failed —
    deleting content the model never even disputed.
    """
    if page.text is not None:
        return
    n = db.query(Chunk).filter(Chunk.page_id == page.id).delete()
    if n:
        log.info("ocr: page %s — evicted %d chunk(s) of the text layer paginate "
                 "refused; this page has no text until we read it", page.page_no, n)


def _mark_failed(db, page: Page, error, task: _VisionTask) -> None:
    """Record one page as unreadable: `failed`, why, and by whom — in the
    rollback-and-re-fetch shape every failure path in `ocr_source` already used,
    now in one place because there are four of them and they must not drift.

    THE RE-FETCH IS NOT CEREMONY. The rollback discards this page's uncommitted
    writes (including, on the embed path, the text and the chunks), which detaches
    the identity-mapped object; `db.get` re-reads the committed row so the status
    lands on something real.
    """
    db.rollback()
    page = db.get(Page, page.id)
    page.status = "failed"
    page.ocr_error = str(error)
    # "failed" is a documented `text_source` value and it is not the same fact as
    # NULL. NULL means "written before anyone tracked this"; this page is one we
    # tried, and lost, and the column must be able to say so.
    page.text_source = "failed"
    # The garbage verdict REPLACES the routing reason — "ocr_garbage" is one of
    # the column's four documented values, and for a scan it IS now the reason the
    # page needs re-reading. EXCEPT for an `image_region` page, whose reason is a
    # live routing decision this module still needs on the next pickup: overwrite
    # it and the page silently falls back to transcribe mode and REPLACES the
    # publisher text it was only ever queued to keep. Nothing is lost by leaving
    # it — the failure is in `ocr_error`, which is where a failure belongs.
    if isinstance(error, _GarbageTranscription) and page.ocr_reason != "image_region":
        page.ocr_reason = "ocr_garbage"
    db.commit()


def source_status_from_page_counts(*, has_unread: bool, char_count: int, failed: int) -> str:
    """THE ONE DECISION behind `KnowledgeSource.status`, shared by `ingest.py`
    (right after upload, before any OCR has ever run) and `_rollup_source_status`
    below (after an OCR run, however far it got) — see both callers' docstrings
    for why a source's status must tell the same truth at both moments, and
    `_evict_refused_chunks`'s docstring for why this app puts a shared rule in
    ONE place rather than trusting N call sites to agree by reminder.

    Priority order, and it IS a priority — each rule below only applies once
    every rule above it says no:

      1. `has_unread` (this source has a `Page` still `pending`/`ocr_running`)
         -> "partial", no matter what `char_count`/`failed` say. THE LIVE BUG
         this rule exists to close, in two shapes: mid-run, Powers read
         `status="ready"` with 9 pages still `pending` (a rate limit or a
         killed process can leave any run short of the finish line, and the
         old rule never asked); and — the more serious half — upload
         deliberately does NOT auto-start OCR, so a freshly uploaded scanned
         book sat "ready" with ZERO pages ever read by anything trustworthy,
         its inherited-Tesseract chunks fully retrievable, and nothing
         anywhere telling the tutor a button existed to press. That window
         was indefinite, not transient. "partial" is right even at
         `char_count == 0`: the source is not done, and "empty" (a dead end
         with no implied next step) is a worse signal than "partial" (amber,
         points at Re-read) for a book nobody has read yet.
      2. `char_count == 0` (and nothing left unread) -> "empty": genuinely
         nothing usable was found anywhere in this source (SPEC D6).
      3. `failed > 0` (and nothing left unread) -> "partial": usable text
         exists, but some pages could not be read at all — the SAME value as
         rule 1, a DIFFERENT reason (Stage 7.2's 74-good/3-unreadable book).
      4. otherwise -> "ready": every page is resolved, and none of them failed.

    `empty` PAGES ARE NOT FAILURES (rule 3 does not see them). A real 77-page
    scan has genuinely blank pages (section breaks, the verso of a plate), and
    the quality gate deliberately records a model's "no visible text"
    narration as `empty` too. Counting those as failures would paint a
    perfectly healthy book amber — the specific false alarm Stage 7.4 was
    warned about.
    """
    if has_unread:
        return "partial"
    if char_count == 0:
        return "empty"
    if failed:
        return "partial"
    return "ready"


def _rollup_source_status(db, source_id) -> None:
    """Re-derive the parent `KnowledgeSource`'s `status`/`char_count` from its
    pages, after the per-page loop above has finished (however far it got —
    including a run the 429 rule parked early, which is exactly the shape that
    exposed the bug this function now guards against; see
    `source_status_from_page_counts`'s docstring, rule 1).

    Guarded the same swallow-and-log way as every other commit in this module
    (module docstring, D5): a rollup failure is logged and rolled back, but must
    not raise back into the caller and must not undo the per-page work already
    durably committed above.
    """
    try:
        source = db.get(KnowledgeSource, source_id)
        if source is None:
            return
        source_pages = db.query(Page).filter(Page.source_id == source_id).all()
        char_count = sum(
            len(p.text) for p in source_pages if p.status == "ready" and p.text
        )
        failed = sum(1 for p in source_pages if p.status == "failed")
        has_unread = any(p.status in ("pending", "ocr_running") for p in source_pages)
        source.char_count = char_count
        source.status = source_status_from_page_counts(
            has_unread=has_unread, char_count=char_count, failed=failed
        )
        db.commit()
    except Exception:
        log.warning("ocr: source status rollup failed for source_id=%s", source_id, exc_info=True)
        db.rollback()


@dataclass
class PageCounts:
    """What a source's pages actually add up to. The Library renders this
    verbatim ("71 of 77 pages read · 6 failed") — before Stage 7.2 it had no way
    to know any of it, and a partially-failed book was a green checkmark."""
    total: int = 0
    ready: int = 0
    failed: int = 0
    empty: int = 0
    pending: int = 0        # pending + ocr_running: not yet resolved either way

    @property
    def resolved(self) -> int:
        """Pages OCR has finished with, whatever the outcome. `resolved + 1` is
        the page a running job is working on right now."""
        return self.ready + self.failed + self.empty


def page_counts(db, source_ids: list) -> dict:
    """`{source_id: PageCounts}` for every id given, in ONE grouped query — not
    a count per source per status, which on the Library's list endpoint would be
    an N×5 fan-out on every poll while a book is being read."""
    counts: dict = {sid: PageCounts() for sid in source_ids}
    if not source_ids:
        return counts
    rows = (
        db.query(Page.source_id, Page.status, func.count(Page.id))
        .filter(Page.source_id.in_(source_ids))
        .group_by(Page.source_id, Page.status)
        .all()
    )
    for source_id, status, n in rows:
        c = counts.setdefault(source_id, PageCounts())
        c.total += n
        if status == "ready":
            c.ready += n
        elif status == "failed":
            c.failed += n
        elif status == "empty":
            c.empty += n
        else:                                  # pending | ocr_running
            c.pending += n
    return counts


# A job is "in flight" in exactly these two statuses. `pending` counts: the row is
# committed by the request handler BEFORE `BackgroundTasks` runs the runner (see
# `routers/library.py::start_ocr`), so there is a real window in which a job that
# is absolutely about to run still reads `pending` — a guard that ignored it would
# let a fast double-click through, which is the entire bug this exists to stop.
# (A process restart cannot strand this guard: `jobs/sweep.py::sweep_orphaned_jobs`
# fails every pending/running job at boot.)
_IN_FLIGHT = ("pending", "running")


def active_ocr_jobs(db, source_ids: list | None = None) -> dict:
    """`{source_id_str: GenerationJob}` for every in-flight OCR job.

    The `source_id` lives inside `GenerationJob.params`, a plain `sa.JSON`
    column — filtered in Python rather than with a JSON operator, because the
    in-flight set is at most a handful of rows (this app runs OCR from
    `BackgroundTasks`, one job per press) and a `->>` predicate would tie this
    to Postgres for no measurable gain.
    """
    jobs = (
        db.query(GenerationJob)
        .filter(GenerationJob.kind == "ocr", GenerationJob.status.in_(_IN_FLIGHT))
        .order_by(GenerationJob.created_at)
        .all()
    )
    wanted = {str(s) for s in source_ids} if source_ids is not None else None
    out: dict = {}
    for job in jobs:
        sid = str((job.params or {}).get("source_id") or "")
        if not sid or (wanted is not None and sid not in wanted):
            continue
        out[sid] = job                          # latest wins; they're time-ordered
    return out


def active_ocr_job(db, source_id):
    """The in-flight OCR job for one source, or None. THE IN-FLIGHT GUARD.

    Without it, `POST /ocr` and `POST /retry` each enqueued a fresh job every
    time they were called — so a tutor who clicked Retry twice (which he did,
    because a reload during the 9-minute OCR used to show his book as RED with a
    Retry button) got TWO jobs reading the same book at once, both running the
    same page's delete-then-insert of chunks, racing.
    """
    return active_ocr_jobs(db, [source_id]).get(str(source_id))


def _render_for_vision(page: Page, dpi: int) -> bytes | None:
    """This page, re-rasterised from the SOURCE PDF at `dpi` — or None if there
    is no PDF to rasterise.

    The stored scan is 110dpi and upscaling it recovers nothing that was not
    captured; the only thing that can produce a real 150dpi page is the original,
    which is why `paginate.py` now keeps it. See `settings.ocr_render_dpi` for why
    110 is a Qwen ceiling rather than a quality choice.

    None (not an exception) for a source with no stored PDF: every book ingested
    before Task 9 has scans and no original, and reading those at the stored
    110dpi is exactly what was always going to happen to them. Raising would make
    the tutor's existing library permanently un-re-readable in the name of
    quality, which is a bad trade and a silent one — the caller logs the fallback.
    """
    path = source_pdf_path(page.source_id)
    if not os.path.exists(path):
        return None
    doc = fitz.open(path)
    try:
        index = page.page_no - 1        # Page.page_no is 1-based, as printed
        if not 0 <= index < doc.page_count:
            # The row and the PDF disagree about how long the book is. Fall back
            # rather than raise: the stored scan for this page is right there, and
            # it is a better answer than failing the page.
            log.warning("ocr: page %s is outside the stored PDF (%d pages) for "
                        "source=%s — falling back to the stored scan",
                        page.page_no, doc.page_count, page.source_id)
            return None
        return doc[index].get_pixmap(dpi=dpi).tobytes("jpeg")
    finally:
        doc.close()


def _stored_scan(page: Page) -> bytes:
    path = os.path.join(settings.media_dir, page.image_path)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        # A clear, human-meaningful ocr_error, not a raw errno string — this
        # is what a tutor (or a future debugging session) actually needs to
        # know: the scan is missing from disk, not a transcription failure.
        raise RuntimeError(f"OCR image file not found on disk: {path}") from None
    except OSError as e:
        raise RuntimeError(f"OCR image file could not be read ({path}): {e}") from None


def _page_pixels(page: Page) -> bytes:
    """The image vision actually reads: the PDF re-rendered at
    `settings.ocr_render_dpi`, or the stored 110dpi scan if there is no PDF."""
    rendered = _render_for_vision(page, settings.ocr_render_dpi)
    if rendered is not None:
        return rendered
    log.info("ocr: no stored PDF for source=%s — reading page %s from its "
             "%ddpi scan instead of a %ddpi re-render",
             page.source_id, page.page_no, RENDER_DPI, settings.ocr_render_dpi)
    return _stored_scan(page)


def _transcribe_with_retry(provider, page: Page, task: _VisionTask) -> str:
    image_bytes = _page_pixels(page)

    last: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = (provider.vision(image_bytes, task.prompt) or "").strip()
            if len(text) >= _SUSPECTED_TRUNCATION_CHARS:
                raise _SuspectedTruncation(
                    f"vision() returned {len(text)} chars (>= "
                    f"{_SUSPECTED_TRUNCATION_CHARS}) — suspected truncation "
                    "at the max_tokens ceiling; finish_reason is not "
                    "available to confirm either way"
                )
            # Garbage is screened HERE, inside the retry loop, rather than at the
            # call site: mojibake is exactly the kind of transient decode failure
            # a second attempt can come back clean from, and if it doesn't, this
            # lands on the same `failed` path as any other unreadable page.
            if _looks_like_garbage(text):
                raise _GarbageTranscription(
                    f"vision() returned {len(text)} chars that are mostly not "
                    "language (>30% symbol/control codepoints) — the scan did "
                    "not decode"
                )
            # The stub-token screen, beside it and for the same reason. Only a
            # TRANSCRIPTION is judged (see `_vision_task_for`): 9 of 617 pages
            # across the three scanned books render blank in both MuPDF and
            # poppler — damaged in the PDF itself — and a model asked to
            # transcribe one does not fail, it produces the same wall of
            # 1-2 character stubs Tesseract did. That is a `failed` page, not
            # 544 characters of content.
            if task.screens_ocr_garbage and looks_like_ocr_garbage(text):
                raise _GarbageTranscription(
                    f"vision() returned {len(text)} chars that are mostly "
                    "1-2 character stubs rather than words — the page did not "
                    "read (it may be damaged in the PDF itself)"
                )
            # THE CONTRACT'S ENFORCEMENT, in the retry loop for the same reason
            # the screens above are: dropped markup is exactly the kind of
            # one-off non-compliance a second attempt comes back clean from, and
            # if it does not, this lands on the same `failed` path as any other
            # page we could not read. The alternative is committing a page whose
            # two halves nothing downstream can separate.
            if task.screens_figure_markup and not _figure_markup_is_closed(text):
                raise _UnclosedFigureRegion(
                    f"vision() returned a transcription with {FIGURE_MARKER} markup "
                    f"that never closes — without a {FIGURE_END} there is no way to "
                    "tell where our description of a picture stops and the book's "
                    "own words resume, and this page's text is quoted back as the "
                    "author's"
                )
            return text
        except LLMError as e:
            if e.kind == "rate_limit":
                # Do NOT spend the in-call retries on a 429 — two instant
                # re-fires into the same rate-limit window can only fail the
                # same way. Propagate immediately; `ocr_source` refunds the
                # attempt and parks the run.
                raise
            last = e
            log.info("ocr: page %s attempt %d failed", page.page_no, attempt)
        except Exception as e:                       # noqa: BLE001 — retry boundary
            last = e
            log.info("ocr: page %s attempt %d failed", page.page_no, attempt)
    raise last                                       # type: ignore[misc]


def _embed_page(db, embedder, page: Page) -> int:
    """Chunk + embed ONE page, deleting any prior chunks for it first so a
    re-run (retry) replaces rather than duplicates. Returns the number of
    chunks created.

    Deliberately does NOT commit: the caller (`ocr_source`) commits this
    page's `status="ready"` in the SAME transaction as these chunks, so a
    page can never be durably `ready` with zero chunks — if the embed()
    call raises, everything added here (and the delete above) rolls back
    together with it."""
    db.query(Chunk).filter(Chunk.page_id == page.id).delete()
    drafts = chunk_sections([Section(heading=None, text=page.text, page=page.page_no)])
    if not drafts:
        return 0
    vectors = embedder.embed([d.text for d in drafts], is_query=False)
    for draft, vector in zip(drafts, vectors):
        db.add(Chunk(
            source_id=page.source_id, page_id=page.id, text=draft.text,
            section_path=draft.section_path, embedding=vector,
        ))
    return len(drafts)
