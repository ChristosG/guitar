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
from dataclasses import dataclass, field, replace
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


def build_library_context(
    db, source_ids: list[UUID] | None, *, ref_start: int = 1
) -> LibraryContext:
    """The selected sources' complete text as ONE page-annotated block:

        <source id="S1" title="Getting Great Guitar Sounds">
        [p.19] ...text... [p.20] ...text...
        </source>

    `source_ids=None` means "everything in the library" — the honest reading of
    an unscoped request, and what the non-interview `POST /curricula/generate`
    sends when the caller names no sources. An empty LIST means "none", which is
    a different, deliberate answer and is respected as one: the result is an empty
    context and every module will be a general-knowledge tier.

    `ref_start` offsets the `S{n}` numbering — the MIXED context in
    `build_curriculum_context` concatenates this block after a canon block that
    already claimed S1..Sk, and two sources sharing one ref would corrupt every
    citation check downstream.

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

    for i, source in enumerate(sources, start=ref_start):
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


# ---------------------------------------------------------------------------
# ROUTING — full-context below the threshold, the canon above it (C5)
# ---------------------------------------------------------------------------

class CurriculumContextError(Exception):
    """The selection cannot be turned into an honest prompt, and we refuse rather
    than ship a silently-partial one.

    `code` is machine-readable (one Greek sentence hangs off it upstream); the
    message names what the tutor must do — compile the listed books, or pick
    fewer. The whole point of this class is that it is a VISIBLE refusal: the job
    fails and says why, instead of an unattended 20-lesson run quietly drafting
    from a canon that was missing one of his books."""

    code = "curriculum_context"

    def __init__(self, message: str, *, uncompiled: list[dict] | None = None):
        super().__init__(message)
        self.uncompiled = uncompiled or []


def _uncompiled_contributors(db, library: "LibraryContext") -> list[dict]:
    """The books whose TEXT is in `library` but whose compile is not `ready`.

    This is exactly the set the canon would silently omit: `library.sources` is
    the sources that actually contributed readable text, and a canon built from
    the ledger drops any of them that never got a `ready` compile. A source with
    no readable text (a note, or a book mid-OCR with no pages yet) is not here —
    it is in neither representation, so it cannot be silently lost by choosing
    one over the other.
    """
    if not library.sources:
        return []
    from app.models.canon import BookCompile

    ids = [UUID(s["id"]) for s in library.sources]
    ready = {
        str(sid)
        for sid in db.scalars(
            select(BookCompile.source_id).where(
                BookCompile.source_id.in_(ids),
                BookCompile.status == "ready",
            )
        ).all()
    }
    return [s for s in library.sources if s["id"] not in ready]


def build_curriculum_context(db, source_ids: list[UUID] | None):
    """THE ROUTER. Returns the context the curriculum prompt should read — a
    `LibraryContext` (verbatim, today's path) at or below `canon_threshold`, or a
    `CanonContext` (the compiled canon) above it. Both mirror each other field for
    field, so `prefix_messages` and every downstream citation check take either
    without a special case.

        <= canon_threshold  -> full-context verbatim   (unchanged)
        >  canon_threshold  -> the canon               (its divergences are the point)

    The size that decides the route is the RAW library's — the thing the tutor
    would otherwise read whole — not the canon's (which is always small). So this
    builds the library first (it needs its token count regardless) and only
    reaches for the canon above the line.

    A selected book that is not yet compiled cannot enter the canon, and a canon
    that silently omits one of his books during an unattended run is the exact bug
    this subsystem exists to prevent. So above the threshold:

      * every contributing book compiled -> the canon;
      * a book uncompiled but the library still FITS whole -> full context, which
        reads everything (not a degrade — it is the higher-fidelity path);
      * a book uncompiled and it does NOT fit -> refuse, naming the book. Never a
        partial canon, never a silent miss.

    Below the threshold none of this applies: the library is read whole exactly as
    before, compiled or not.
    """
    library = build_library_context(db, source_ids)
    if library.token_count <= settings.canon_threshold:
        return library

    uncompiled = _uncompiled_contributors(db, library)
    if not uncompiled:
        from app.canon.render import build_canon_context

        canon = build_canon_context(db, source_ids)
        if canon.is_empty:
            # Every contributor SAYS it is compiled (`BookCompile.status ==
            # "ready"`) yet the canon holds zero claims — lost rows, a purged
            # ledger, a bad migration. Returning the empty canon here fed
            # `prefix_messages`' NO_LIBRARY branch, and the tutor paid full
            # price for a 24-lesson course drafted ENTIRELY from general
            # knowledge with his library never sent and no error anywhere —
            # the only tell was the tier badges. Retrieval reads every chunk
            # of every source regardless of compile status; it is the honest
            # floor, not a refusal.
            log.warning(
                "curriculum: %d contributing source(s) report compiled but the "
                "canon is EMPTY — falling back to retrieval grounding instead "
                "of drafting from general knowledge",
                # NOT len(source_ids): an unscoped selection passes None (the
                # "whole library" contract), and this rung is exactly where a
                # crash must not happen.
                len(library.sources),
            )
            return build_retrieval_context(db, source_ids)
        return canon

    if library.fits:
        # Full context still fits, and it reads every book whole — including the
        # uncompiled one. Strictly better here than a canon missing a book.
        log.info(
            "curriculum: %d selected book(s) not compiled yet (%s) — reading the "
            "library WHOLE instead of the canon, which still fits (%d tokens)",
            len(uncompiled), ", ".join(s.get("title") or s["id"] for s in uncompiled),
            library.token_count,
        )
        return library

    # Too large to read whole AND some contributors are uncompiled. This used to
    # REFUSE ("compile them and try again") — which is how the tutor's real run
    # died twice on 2026-07-20: he had selected his 4 compiled books plus a text
    # doc and five URL sources, and the wizard failed after the fact instead of
    # building what it honestly could. Two graceful rungs replace the refusal:
    #
    #   1. MIXED: the canon for the compiled books + the uncompiled sources read
    #      VERBATIM after it. His uncompiled sources are the small ones (URLs, a
    #      course-spine note); the canon is ~40K tokens — together they almost
    #      always fit, and NOTHING is silently left out: compiled books are in
    #      the canon, uncompiled ones are on the page.
    #   2. RETRIEVAL: if even that doesn't fit, ground per-lesson via retrieval,
    #      which searches every chunk of every source regardless of compile
    #      status — the same degrade `run_curriculum_draft_job` already trusted.
    uncompiled_ids = [UUID(s["id"]) for s in uncompiled]
    compiled_ids = [
        UUID(s["id"]) for s in library.sources if s["id"] not in {u["id"] for u in uncompiled}
    ]
    from app.canon.render import build_canon_context

    canon = build_canon_context(db, compiled_ids)
    tail = build_library_context(
        db, uncompiled_ids, ref_start=len(canon.ref_to_source_id) + 1
    )
    mixed_tokens = canon.token_count + tail.token_count
    if not canon.is_empty and mixed_tokens <= settings.full_context_budget:
        log.info(
            "curriculum: MIXED context — canon for %d compiled book(s) (%d tokens) "
            "+ %d uncompiled source(s) verbatim (%d tokens)",
            len(compiled_ids), canon.token_count, len(uncompiled_ids), tail.token_count,
        )
        return LibraryContext(
            text=f"{canon.text}\n\n{tail.text}",
            token_count=mixed_tokens,
            fits=True,
            sources=canon.sources + tail.sources,
            page_index={**canon.page_index, **tail.page_index},
            ref_to_source_id={**canon.ref_to_source_id, **tail.ref_to_source_id},
        )

    titles = ", ".join(s.get("title") or s["id"] for s in uncompiled)
    log.warning(
        "curriculum: selection too large to read whole (%d tokens), uncompiled "
        "source(s) (%s) keep it out of the canon, and the mixed context does not "
        "fit either (%d tokens) — grounding per-lesson via retrieval",
        library.token_count, titles, mixed_tokens,
    )
    return build_retrieval_context(db, source_ids)


def build_retrieval_context(db, source_ids: list[UUID] | None) -> LibraryContext:
    """The selected library as a RETRIEVAL-GROUNDED context — for the paths that
    must never read the whole library and must never REFUSE.

    A `LibraryContext` whose text is the same one `build_library_context` reads, but
    flagged `fits=False` unconditionally, so every draft grounds PER-LESSON via
    `draft.draft_lesson`'s existing `ground_topic` fallback (which searches ALL of
    the sources' chunks regardless of canon-compile status) instead of sending the
    whole book or raising `CurriculumContextError`.

    This is what makes the whole-library REFUSAL's concern moot: retrieval covers
    every chunk of every source — compiled or not — so nothing is silently left out.
    Where retrieval finds little, `draft.py`'s tier framing lets the model teach from
    its own knowledge. Two callers:

      * the revise-chained draft (`jobs/curriculum_revise`) — a revise CHANGES a
        lesson, the library is optional grounding, never the whole-library gate;
      * the graceful fallback for `run_curriculum_draft_job`'s former REFUSE case
        (too large + a contributing book uncompiled), which used to fail the run.

    When the selection has no readable text at all the empty context is returned
    unchanged (`is_empty` stays True): the draft then teaches honestly from general
    knowledge (the NO_LIBRARY / gap-tier framing), never pretending to a library it
    cannot read. Note `page_index`/`sources`/`ref_to_source_id` are the REAL ones
    from `build_library_context`, so a citation the model does land is still
    validated against the pages that actually exist.
    """
    library = build_library_context(db, source_ids)
    if library.is_empty:
        return library
    return replace(library, fits=False)


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


CURRICULUM_SYSTEM_SLICE_ID = "curriculum.system"

# The three prompts below are lifted out of the builders byte-identically so the tutor
# can rewrite them. Each is a full replacement for a block the model reads; the
# `{placeholders}` are the only parts this app fills in, and `overrides.validate`
# refuses an edit that drops one.
#
# ALL THREE SIT INSIDE THE CACHED PREFIX (`cache: True`, below), so an edit re-mints
# the cache ONCE — the UI says so before he saves, which is the whole reason
# `cache_prefix` is carried on the registry entry.
LIBRARY_TOO_LARGE = (
    "The tutor's library is TOO LARGE to read in full for this course "
    "({token_count} tokens). You are not being shown it. "
    "You will instead be given the passages retrieved for each specific "
    "module, below. Tier a module 'library' ONLY where such a passage "
    "actually supports it — never from memory of a book you have not "
    "been shown."
)
LIBRARY_TOO_LARGE_SLICE_ID = "curriculum.library_too_large"

NO_LIBRARY = (
    "The tutor selected NO library sources for this course (or they "
    "contain no readable text). You have nothing of his to read, so "
    "tier every module honestly as 'general_knowledge' — never as "
    "'library'."
)
NO_LIBRARY_SLICE_ID = "curriculum.no_library"

# `{library}` is HIS BOOKS — the whole point of the message, and the one thing in it
# this app did not author. An edit that drops it leaves the model an instruction to
# read a library it was never given, which is why the placeholder is enforced.
LIBRARY_MESSAGE = (
    "Here is the tutor's ENTIRE library — his own books and materials, "
    "complete, with page markers. Read it. Everything you write for him "
    "should come from this where it possibly can.\n\n"
    "{library}"
)
LIBRARY_MESSAGE_SLICE_ID = "curriculum.library"


def prefix_messages(library: LibraryContext, source=None) -> list[dict]:
    """`[system, cached library]` — THE stable prefix, byte-identical across every
    full-context curriculum call. Build on top of this; never edit the result.

    When the library is empty there is nothing to cache and the caller gets an
    honest substitute block instead, so downstream prompts can still say "tier
    honestly — you read nothing of his".

    When the library does NOT FIT (`library.fits is False`), the same thing
    happens for the opposite reason: the block is too large to send at all, let
    alone cache. See below.

    `source` is the tutor's overrides (a Session, a `snapshot()` mapping, or None) —
    see `app/prompts/overrides.py`. NOTE FOR THE CACHE: an override changes this
    prefix, so it re-mints the cache ONCE and then re-warms. That is a fact about
    editing, shown to him before he saves; it is not a reason to keep the prompt out
    of his hands.
    """
    from app.prompts.overrides import resolve

    system = resolve(source, CURRICULUM_SYSTEM_SLICE_ID, CURRICULUM_SYSTEM)
    messages: list[dict] = [{"role": "system", "content": system}]
    if not library.is_empty and not library.fits:
        # The docstring promised this for two days and never did it. Above the
        # budget the library block is NOT sent: `draft.py` tops the tail up with
        # retrieval, and that top-up is the substitute, not an addition. Shipping
        # both is what the code did before — strictly worse than either alone, and
        # it re-wrote a 600K block into the cache at 1.25x on every outline.
        messages.append({
            "role": "user",
            "content": resolve(
                source, LIBRARY_TOO_LARGE_SLICE_ID, LIBRARY_TOO_LARGE,
            ).format(token_count=f"{library.token_count:,}"),
        })
        return messages
    if not library.is_empty:
        messages.append(library_message(library, source))
    else:
        messages.append({
            "role": "user",
            "content": resolve(source, NO_LIBRARY_SLICE_ID, NO_LIBRARY),
        })
    return messages


def library_message(library: LibraryContext, source=None) -> dict:
    """The library as ONE cached user message — THE STABLE PREFIX.

    `cache: True` becomes `cache_control: {"type": "ephemeral"}` at the wire
    (`llm/anthropic_wire.py`). Everything the caller adds after this message is
    volatile and outside the cache breakpoint, which is exactly where it belongs.
    """
    from app.prompts.overrides import resolve

    return {
        "role": "user",
        "cache": True,
        "content": resolve(
            source, LIBRARY_MESSAGE_SLICE_ID, LIBRARY_MESSAGE,
        ).format(library=library.text),
    }
