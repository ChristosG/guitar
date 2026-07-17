"""The tutor's SELECTED library, whole, page-annotated, as one cached prompt
prefix. Curriculum authoring reads the book; it does not search it.

WHY THIS IS NOT RETRIEVAL, MEASURED RATHER THAN ASSERTED.

His entire library is ~359,000 characters ~= 90K tokens. That is 9% of Sonnet 5's
1M-token window. At his own "5x max" growth estimate it is ~450K tokens and it
still fits. So the question "should curriculum authoring use RAG?" has a measured
answer, and the answer is no — for one reason that has nothing to do with
elegance:

    A CURRICULUM GENERATES 20 LESSONS UNATTENDED. A silent retrieval miss becomes
    a false "your library doesn't cover this", becomes general knowledge instead
    of his book, with nobody watching. That IS the bug he reported.

And retrieval on this corpus really does miss, confidently. "Tube Screamer"
appears verbatim in 7 chunks of his book; the dense arm's top hit for that exact
query scores 0.844 and contains none of them. The covered/uncovered cosine margin
is 0.021. Chat can survive that — the tutor is reading the answer and can
rephrase. Twenty lessons drafted overnight cannot.

Retrieval is still the right tool for chat, for library search, and for the
oversized-library fallback below. It is the wrong tool for this.

---------------------------------------------------------------------------
THE PREFIX MUST BE STABLE, AND THAT CONSTRAINT REACHES INTO EVERY CALLER.

The library block carries `cache_control: ephemeral`. The outline call writes it
(90K tokens at 1.25x = $0.34); each of the 20 lesson drafts then READS it (0.1x =
$0.027 apiece), and every read refreshes the 5-minute TTL, so a 2-worker fan-out
firing every ~40 seconds keeps it warm for the whole run. Total ~$2.72 for a
fully-grounded 45,000-word Greek curriculum.

That only holds if the cached prefix is BYTE-IDENTICAL across calls. So:

    everything volatile — the module title, the lesson objective, the student
    brief, the course brief — goes strictly AFTER the library block, never
    inside it.

Get that wrong and nothing breaks. The curriculum still generates. It just costs
ten times as much, and the only symptom is an invoice a month later. Hence
`ClaudeProvider.last_usage` and the assertion in this module's tests: the second
call of a run MUST report non-zero `cache_read_input_tokens`.

---------------------------------------------------------------------------
WHEN IT DOES NOT FIT, WE SAY SO.

`count_tokens` is free and exact (`messages.count_tokens`). Above
`settings.full_context_budget` (600K, leaving room for a 32K output plus the
volatile tail) `fits` goes False and the caller degrades to per-module retrieval
— WITH AN HONEST BANNER, never silently. A tutor whose library quietly stopped
being read in full, and who was never told, is back to the bug we started with.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource, Page

log = logging.getLogger(__name__)

# A page whose text is shorter than this contributes nothing but noise and page
# markers — an OCR'd blank, a running head, "There is no visible text on this
# page." Same instinct as `retrieve.MIN_PASSAGE_CHARS`, at a coarser granularity:
# there we are protecting a citation, here we are protecting a token budget.
MIN_PAGE_CHARS = 40


@dataclass
class LibraryContext:
    """The whole selected library as one prompt block, plus what we measured.

    `page_index` is the contract `app.curriculum.draft` validates citations
    against: a `{source_id: {page_no, ...}}` map of every (source, page) pair the
    model was ACTUALLY SHOWN. A citation outside it is not a citation, it is a
    hallucination that renders as a chip the tutor can click — and lands him on a
    page that does not say what the lesson claims it says. That is worse than no
    citation at all, because it is a citation he will trust.
    """

    text: str
    token_count: int
    fits: bool
    sources: list[dict] = field(default_factory=list)   # {ref, id, title, pages, chars}
    page_index: dict[str, set[int]] = field(default_factory=dict)
    # ref ("S1") -> the real KnowledgeSource.id, so a validated citation can be
    # persisted as something the Reader can actually deep-link to.
    ref_to_source_id: dict[str, UUID] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.page_index

    def summary(self) -> str:
        """*"3 sources · 92,400 tokens · fits whole"* — shown at the source-
        selection step, before he spends anything."""
        n = len(self.sources)
        verdict = "fits whole" if self.fits else "TOO LARGE — will fall back to retrieval"
        return f"{n} source{'s' if n != 1 else ''} · {self.token_count:,} tokens · {verdict}"


def _page_texts(db, source_id: UUID) -> list[tuple[int, str]]:
    """(page_no, text) for one source, in reading order.

    `Page.text` FIRST, chunks only as a fallback, and the order matters.
    `Page.text` is what OCR wrote (`brain/ocr.py`) and what `paginate.py`
    opportunistically captured from a PDF's text layer — it is the page, once.
    `Chunk.text` is the same material re-cut with a 150-character OVERLAP between
    consecutive chunks (`brain/chunk.py`), so concatenating chunks duplicates a
    sentence at every boundary. Harmless for retrieval (they are never
    concatenated there); here it would put ~12% of the book into the prompt twice
    and pay for it.

    The fallback exists anyway, for the source whose pages carry no text at all
    but whose chunks do — an older ingest, or a text/url source whose single Page
    row predates the text column being filled.
    """
    pages = db.scalars(
        select(Page).where(Page.source_id == source_id).order_by(Page.page_no)
    ).all()

    out: list[tuple[int, str]] = []
    for page in pages:
        text = (page.text or "").strip()
        if len(text) >= MIN_PAGE_CHARS:
            out.append((page.page_no, text))
    if out:
        return out

    rows = db.execute(
        select(Chunk.text, Page.page_no)
        .outerjoin(Page, Chunk.page_id == Page.id)
        .where(Chunk.source_id == source_id)
        .order_by(Page.page_no.nulls_last(), Chunk.created_at)
    ).all()
    by_page: dict[int, list[str]] = {}
    for text, page_no in rows:
        by_page.setdefault(page_no or 1, []).append(text)
    return [
        (page_no, "\n".join(parts))
        for page_no, parts in sorted(by_page.items())
        if len("\n".join(parts)) >= MIN_PAGE_CHARS
    ]


def build_library_context(db, source_ids: list[UUID] | None) -> LibraryContext:
    """The selected sources' complete text as ONE page-annotated block:

        <source id="S1" title="Getting Great Guitar Sounds">
        [p.19] ...text... [p.20] ...text...
        </source>

    `source_ids=None` means "everything in the library" — the honest reading of
    an unscoped request, and what the non-interview `POST /curricula/generate`
    sends when the caller names no sources. An empty LIST means "none", which is
    a different, deliberate answer and is respected as one: the result is an empty
    context and every module will be a general-knowledge tier.

    The `[p.N]` markers are not decoration. They are the only mechanism by which
    a model reading 90K tokens of prose can tell us WHICH PAGE a claim came from,
    and they are what `draft.py` validates every citation against. Claude's native
    `citations` feature cannot help here: it is mutually exclusive with
    `output_config.format` (400), and we need structured output far more than we
    need its citation format.
    """
    stmt = select(KnowledgeSource).order_by(KnowledgeSource.created_at)
    if source_ids is not None:
        if not source_ids:
            return LibraryContext(text="", token_count=0, fits=True)
        stmt = stmt.where(KnowledgeSource.id.in_(source_ids))
    sources = db.scalars(stmt).all()

    blocks: list[str] = []
    meta: list[dict] = []
    page_index: dict[str, set[int]] = {}
    ref_to_source_id: dict[str, UUID] = {}

    for i, source in enumerate(sources, start=1):
        pages = _page_texts(db, source.id)
        if not pages:
            continue  # a source with no readable text is not a source, it is a row
        ref = f"S{i}"
        body = " ".join(f"[p.{page_no}] {text}" for page_no, text in pages)
        title = (source.title or "").replace('"', "'")  # keep the attribute well-formed
        blocks.append(f'<source id="{ref}" title="{title}">\n{body}\n</source>')
        page_index[ref] = {page_no for page_no, _ in pages}
        ref_to_source_id[ref] = source.id
        meta.append({
            "ref": ref,
            "id": str(source.id),
            "title": source.title,
            "pages": len(pages),
            "chars": sum(len(t) for _, t in pages),
        })

    text = "\n\n".join(blocks)
    token_count = _count_tokens(text)

    return LibraryContext(
        text=text,
        token_count=token_count,
        fits=token_count <= settings.full_context_budget,
        sources=meta,
        page_index=page_index,
        ref_to_source_id=ref_to_source_id,
    )


def _count_tokens(text: str) -> int:
    """Exact when the provider can tell us, estimated when it cannot.

    NEVER raises. A `count_tokens` call that fails (no key yet, a rate limit) must
    not take down the source-selection screen — the estimate on `LLMProvider` is
    pessimistic by design, so a fallback errs towards "this might not fit", which
    degrades to retrieval WITH A BANNER rather than towards a context-length 400
    ninety seconds into a call.
    """
    if not text:
        return 0
    try:
        return get_provider().count_tokens(text)
    except Exception:
        log.warning("count_tokens failed; falling back to the local estimate", exc_info=True)
        return len(text) // 3 + 1


# THE ONE SYSTEM MESSAGE for every full-context curriculum call — outline, lesson
# draft, deepen, add-module. The cache key covers tools + system + messages up to
# the breakpoint, so a system string that varies BETWEEN calls (the old drafts
# baked each lesson's word target and tier directive into theirs) silently mints a
# separate 90K-token cache entry at 1.25x per variant. Nothing breaks; the invoice
# is just bigger. Task-specific instructions (counts, length, tier, language) all
# live in the volatile tail AFTER the library block — which is also the strongest
# position for them: with a 90K-token book in the middle, the model weighs the end
# of the prompt hardest.
CURRICULUM_SYSTEM = (
    "You are writing curriculum for a working guitar teacher, from his OWN "
    "library, which you are about to read in full. After the library you will "
    "be given ONE specific task — outline a course, design a module, or write "
    "out a complete lesson.\n\n"
    "Output ONLY the JSON matching the schema you are given — no prose, no "
    "markdown, no commentary outside the JSON object."
)


def prefix_messages(library: LibraryContext) -> list[dict]:
    """`[system, cached library]` — THE stable prefix, byte-identical across every
    full-context curriculum call. Build on top of this; never edit the result.

    When the library is empty there is nothing to cache and the caller gets an
    honest substitute block instead, so downstream prompts can still say "tier
    honestly — you read nothing of his".

    When the library does NOT FIT (`library.fits is False`), the same thing
    happens for the opposite reason: the block is too large to send at all, let
    alone cache. See below.
    """
    messages: list[dict] = [{"role": "system", "content": CURRICULUM_SYSTEM}]
    if not library.is_empty and not library.fits:
        # The docstring promised this for two days and never did it. Above the
        # budget the library block is NOT sent: `draft.py` tops the tail up with
        # retrieval, and that top-up is the substitute, not an addition. Shipping
        # both is what the code did before — strictly worse than either alone, and
        # it re-wrote a 600K block into the cache at 1.25x on every outline.
        messages.append({
            "role": "user",
            "content": (
                "The tutor's library is TOO LARGE to read in full for this course "
                f"({library.token_count:,} tokens). You are not being shown it. "
                "You will instead be given the passages retrieved for each specific "
                "module, below. Tier a module 'library' ONLY where such a passage "
                "actually supports it — never from memory of a book you have not "
                "been shown."
            ),
        })
        return messages
    if not library.is_empty:
        messages.append(library_message(library))
    else:
        messages.append({
            "role": "user",
            "content": (
                "The tutor selected NO library sources for this course (or they "
                "contain no readable text). You have nothing of his to read, so "
                "tier every module honestly as 'general_knowledge' — never as "
                "'library'."
            ),
        })
    return messages


def library_message(library: LibraryContext) -> dict:
    """The library as ONE cached user message — THE STABLE PREFIX.

    `cache: True` becomes `cache_control: {"type": "ephemeral"}` at the wire
    (`llm/anthropic_wire.py`). Everything the caller adds after this message is
    volatile and outside the cache breakpoint, which is exactly where it belongs.
    """
    return {
        "role": "user",
        "cache": True,
        "content": (
            "Here is the tutor's ENTIRE library — his own books and materials, "
            "complete, with page markers. Read it. Everything you write for him "
            "should come from this where it possibly can.\n\n"
            f"{library.text}"
        ),
    }
