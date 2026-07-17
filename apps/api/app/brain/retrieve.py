"""Hybrid retrieval: dense (e5, 384-dim) + lexical (BM25), fused with RRF —
plus the ONE relevance floor in this codebase, and the query normalisation that
makes cross-lingual search actually work.

Everything here was calibrated against the tutor's REAL 408-chunk library, not
against intuition. `scripts/retrieval_baseline.py` is the harness; its numbers
are quoted throughout, and it still runs.

---------------------------------------------------------------------------
WHY TWO ARMS. Dense retrieval on this corpus is confidently, silently wrong about
gear. Measured on the re-embedded library, asking only "does ANY retrieved chunk
actually contain the words the query asked for?":

    query                       dense top-10   dense rank #1   hybrid rank #1
    ---------------------------------------------------------------------------
    Tube Screamer                    no             no              YES
    TS-808                          yes             no              YES
    5150                             no             no              YES
    Stratocaster single coil        yes            yes              YES
    ---------------------------------------------------------------------------
                                    2/4            1/4              4/4

"Tube Screamer" occurs verbatim in the tutor's own book. The dense arm's top hit
for that exact query scores 0.837 and does not contain it — it returns something
that *sounds like pedal talk*. A dense model has no mechanism to prefer the
document that literally says the words, and no way to tell you that it didn't.
BM25 (`app.brain.lexical`) cannot fail that way: a rare token is either in a
document or it is not. Neither arm subsumes the other, so both run and both count.

FUSION IS RECIPROCAL RANK FUSION (k=60), equal weights, hardcoded. RRF fuses
RANKS, not scores, which is the only defensible choice here: a cosine in [0,1]
and an unbounded BM25 score share no unit, and every scheme for inventing one
(per-query min-max, z-scores) is a knob nobody has the data to turn.

---------------------------------------------------------------------------
`Hit.score` IS THE ORDERING KEY AND NOTHING ELSE. `Hit.vector_score` IS THE
COSINE. There is a real bug behind that distinction.

`score` used to BE the cosine, and `curriculum/ground.py` thresholded it at
`>= 0.60` to decide whether a curriculum module was covered by the library. RRF
scores top out around 2/(60+1) ~= 0.033. Had `score` quietly become the RRF
score, EVERY module would have fallen below 0.60, every one would have been
reported as an uncovered "gap", and the failure would have looked like a content
problem rather than a units problem.

    score         -> the RRF fusion score. Ordering only. NEVER thresholded.
    vector_score  -> the cosine, in [0,1]. The ONLY quantity a floor may compare
                     against. Always populated, including for hits that arrived
                     via the lexical arm alone.
    lexical_score -> Okapi BM25. 0.0 when the lexical arm didn't rank the chunk.

---------------------------------------------------------------------------
THE FLOOR IS NOT A COSINE THRESHOLD, AND THIS IS THE HONEST PART.

The temptation is an absolute cosine floor, because the Qwen-era code had one
(0.60) and it looked calibrated. It is neither transferable nor, on its own, a
good instrument. Two measured facts:

  * e5 scores COMPRESS into roughly 0.78-0.92 on this corpus. The old 0.60 would
    pass literally everything — porting that constant would have inverted gap
    detection while appearing to change nothing.
  * The margin is 0.021. Lowest COVERED topic: 0.881. Highest UNCOVERED topic:
    0.860. A 0.021 gap between "the library teaches this" and "it does not" is
    not an instrument; it is a coin flip with a decimal point.

So the floor is `length AND noise-screen AND (confident-dense OR complete-lexical)`
— see `_passes_floor`, which carries the measured table. In outline:

  1. LENGTH (`MIN_PASSAGE_CHARS`). The strongest single discriminator, and the
     one a score misses entirely. Every identified piece of junk in the real
     library is short: a 19-char OCR fragment ("PART ONE The Guitar" — which
     really scores 0.64 and really has been cited to the tutor), a 38-char
     "There is no visible text on this page.", an 86-char unrendered page title,
     a 295-char enrolment boilerplate. The shortest GENUINE passage observed
     across every calibration query was 532 chars. 200 excludes every known junk
     item with a 2.6x margin and sits far below every real one.

  2. LEXICAL COVERAGE (this is what saves gap detection, and it is the strong
     part of the design). A passage is admitted on lexical evidence only if it
     contains EVERY distinctive term the query used — where a term the corpus has
     NEVER SEEN counts as distinctive and permanently unmatched. So a question
     about "tablature notation", two words absent from all 408 chunks, cannot be
     answered from the library no matter how well its other words match. A dense
     model can never reach this conclusion: it always has a nearest neighbour, and
     it always feels fine about it.

  3. ABSOLUTE COSINE, twice, in two different roles. `WEAK_COSINE_FLOOR` is a
     NOISE SCREEN, set far below the covered/uncovered boundary. `STRONG_COSINE`
     is the dense arm's "I am sure" line, and it necessarily sits INSIDE that
     0.021 margin — it is the weakest link here, it is not asked to decide alone,
     and its own comment says so rather than pretending otherwise.

---------------------------------------------------------------------------
QUERY NORMALISATION — the highest-value few lines in this file.

The tutor's default locale is Greek. His library is an ENGLISH book. e5 is
multilingual, so a Greek query does retrieve English passages — just badly:
across the four Greek/English twin queries in the baseline, the gold chunk came
back 1 time in 4. Translating the query into the corpus's language first (one
cheap Haiku-class call, ~150ms, cached) took that to 4/4, ALL AT RANK #1.

It happens inside `search()`, so every caller inherits it — including the chat
pre-hop, which grounds off the raw user message and has no opportunity to ask the
model for a translation. A failed translation is never fatal: the raw query runs.

---------------------------------------------------------------------------
NO `language` OR `domain` FILTER. Both are gone from this function, deliberately.

`domain` had a NULL-exclusion bug (fixed in 5d77bd0: 12 of 16 real sources are
untagged, so `domain="tone"` discarded the tutor's whole library). `language` has
the SAME bug plus one worse property: it was MODEL-CHOSEN. Under a Greek default
locale the model passes `language="el"` and filters his English book to exactly
zero results — in the language he actually works in. A filter that can silently
empty the corpus, on a value the model guessed, is not a feature. Both columns
survive as Library display metadata. When a caller genuinely needs to scope
retrieval, it uses `source_ids` — which the TUTOR chooses.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from sqlalchemy import select

from app.brain.lexical import get_index, tokenize
from app.i18n import DEFAULT_LOCALE, answer_in, language_directive
from app.prompts.overrides import resolve
from app.llm.embed_factory import get_embedder
from app.llm.factory import get_provider
from app.models.knowledge import Chunk, KnowledgeSource, Page
from app.text.normalize import has_greek

log = logging.getLogger(__name__)

# Candidates pulled from EACH arm before fusion. 50 is generous on a 408-chunk
# corpus (12% of it) and costs nothing at this scale. Over-fetching is the point:
# a chunk ranked #40 by one arm and #2 by the other must still be able to reach
# the top of the fused list. That recovery IS the hybrid.
_ARM_K = 50

# RRF's damping constant, at the value the original paper reports and everyone
# uses. Its job is to stop rank-1 from dominating so completely that the second
# arm can never move anything. Not tuned — there is no labelled relevance set on
# this corpus to tune it against, and inventing one would be theatre.
_RRF_K = 60

# See point 1 of the module docstring. Also imported by `curriculum/interview.py`
# as its "is this source substantive enough to pre-select" threshold — the same
# question asked at a different granularity.
MIN_PASSAGE_CHARS = 200

# A NOISE SCREEN, not a coverage test. Nothing in this corpus that is even
# vaguely on-topic scores below this; it exists to drop the dregs.
WEAK_COSINE_FLOOR = 0.70

# The dense arm's "I am sure" line. Measured on the real library (top-hit cosine):
#
#     COVERED topics      0.880  0.882  0.883  0.887  0.893  0.893
#     UNCOVERED topics    0.809  0.844  0.862
#     the adversarial junk query                   0.856
#
# 0.87 sits in that gap — a 0.018 margin. THAT IS TOO THIN TO TRUST, and it is
# not asked to be trusted: it is one arm of an OR, not the decision. Everything
# below it can still be admitted by full lexical coverage, which is how the gear
# queries (top cosines 0.787-0.856 — the dense arm is genuinely unsure about a
# bare model number) survive at all. Said plainly rather than dressed up: this
# constant is the weakest link in the floor, and the lexical arm is what makes it
# survivable.
STRONG_COSINE = 0.87


@dataclass
class Hit:
    chunk_id: UUID
    source_id: UUID
    source_title: str
    text: str
    section_path: str | None
    page: int | None
    # THE ORDERING KEY ONLY (RRF). Never threshold it — see the module docstring.
    score: float
    # Page.id (FK), resolved by joining Chunk.page_id -> Page, so a citation is
    # VERIFIABLE (the page scan is fetchable) rather than merely claimed. None for
    # chunks whose source predates the Page model.
    page_id: UUID | None = None
    # The cosine, in [0,1]. The only quantity a floor may compare against.
    # Backfilled for lexical-only hits so it is NEVER None and no caller has to
    # write `if h.vector_score is not None`.
    vector_score: float = 0.0
    lexical_score: float = 0.0


# ---------------------------------------------------------------------------
# Query normalisation to the corpus language
# ---------------------------------------------------------------------------

_TRANSLATE_SYSTEM = (
    "You translate short search queries for a guitar-teaching knowledge base. "
    "Reply with ONLY the translated English query — no quotes, no explanation, no "
    "preamble. Never translate machine tokens: chord and note names (C, Am7, G7), "
    "tunings (Drop D), fret and string numbers, and gear model names (Tube "
    "Screamer, TS-808, 5150) are reproduced exactly as given."
)


# THE CIRCUIT BREAKER. Retrieval must not inherit the chat provider's downtime.
#
# Without this, an unreachable provider costs every Greek query a full connect
# timeout (plus the SDK's retries) BEFORE the search it was only ever going to
# improve. Search still worked — `_translate_to_english` swallows everything — it
# just took thirty seconds to do it, which to the tutor is indistinguishable from
# broken. So a failure trips the breaker and subsequent queries skip the call.
#
# TIME-BASED after all (review fix): the permanent latch treated every failure
# as "no key / wrong URL / no network" — but ONE transient 429 or a two-second
# network blip also latched it, silently collapsing Greek retrieval quality
# (raw-Greek BM25 against an English corpus) for the remaining LIFETIME of the
# process, on an app that is meant to run for months. A 10-minute cooldown
# keeps the original economics — a genuinely dead provider re-pays one timeout
# every ten minutes, not per query — while a hiccup self-heals. The breaker
# also resets when the tutor saves a new key (`reset_translation_breaker`,
# called from the provider-cache clear).
_TRANSLATION_COOLDOWN_S = 600.0
_translation_blocked_until = 0.0


def reset_translation_breaker() -> None:
    """Re-arm query translation immediately — called when the LLM settings
    change: a freshly pasted key deserves a fresh try, not the tail of the old
    key's cooldown."""
    global _translation_blocked_until
    _translation_blocked_until = 0.0


@lru_cache(maxsize=512)
def _translate_call(query: str, system: str) -> str:
    """The RAISING inner call, cached. `lru_cache` does not memoize raised
    exceptions — which is the entire reason for this split: with the try/except
    inside the cached function, a query that failed ONCE (during an outage, a
    429) had its raw-query fallback cached FOREVER, so that exact query stayed
    untranslated for the process lifetime even after the provider recovered.

    `system` IS THE RESOLVED PROMPT, PASSED IN RATHER THAN READ HERE, and taking it
    as an argument is what makes the tutor's override work through a cache: it joins
    the cache KEY, so the moment he edits this prompt every memoized translation of
    it is orphaned and the next query re-translates under his new text. Reading the
    override inside this function instead would have been the subtle version of the
    bug this whole feature exists to kill — his edit saved, the screen showing it,
    and the model still being sent last week's prompt for every query he had already
    searched once. (It also cannot take a Session: `lru_cache` needs a hashable key.)
    """
    out = get_provider().chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        temperature=0.0,
        enable_thinking=False,
    )
    out = (out or "").strip()
    # A model that answers a translation request with a paragraph has not
    # translated anything. Guard on length rather than trusting the prompt to hold.
    if not out or len(out) > 4 * len(query) + 40:
        return query
    return out


def _translate_to_english(query: str, system: str) -> str:
    """Greek query -> English query. Cached (successes only), because the tutor
    asks about the same dozen topics over and over and this is a network call on
    the retrieval hot path.

    NEVER raises. An unconfigured key, a rate limit, a timeout — every one of them
    means "search with the query we already have", which is merely the pre-Stage-4
    behaviour, not a broken app. A retrieval path that can 500 because a translation
    hiccuped would be a strictly worse trade than the one this function makes.
    """
    global _translation_blocked_until
    if time.monotonic() < _translation_blocked_until:
        return query
    try:
        return _translate_call(query, system)
    except Exception:
        _translation_blocked_until = time.monotonic() + _TRANSLATION_COOLDOWN_S
        log.warning(
            "query translation failed; searching with the raw query for the next "
            "%d minutes (see the circuit-breaker note in retrieve.py)",
            int(_TRANSLATION_COOLDOWN_S // 60),
            exc_info=True,
        )
        return query


def _corpus_is_greek(db) -> bool:
    """Is the library itself predominantly Greek?

    Measured, not assumed — from the BM25 index's own term dictionary, which is
    already in memory. No extra query, no language-detection dependency, and it
    folds text exactly the way the index does. "The library is English" is true
    today and stops being true the first time the tutor uploads his own notes.
    """
    index = get_index(db)
    if not index.df:
        return False
    greek_terms = sum(1 for term in index.df if has_greek(term))
    return greek_terms > 0.5 * len(index.df)


def normalize_query(db, query: str) -> str:
    """Put `query` into the corpus's script before it is embedded. See the module
    docstring: 1/4 -> 4/4 gold-chunk recall, all at rank #1."""
    if not has_greek(query):
        return query
    try:
        if _corpus_is_greek(db):
            return query
    except Exception:
        log.warning("corpus-language probe failed; assuming a non-Greek corpus", exc_info=True)
    translated = _translate_to_english(
        query, resolve(db, TRANSLATE_SLICE_ID, _TRANSLATE_SYSTEM),
    )
    if translated != query:
        log.info("query normalised to corpus language: %r -> %r", query, translated)
    return translated


# ---------------------------------------------------------------------------
# The two arms, and their fusion
# ---------------------------------------------------------------------------

def _vector_arm(db, query: str, source_ids: list[UUID] | None) -> list[tuple[UUID, float]]:
    """Top-`_ARM_K` by exact cosine. An exact brute-force scan, on purpose: at 408
    chunks (and at ten times that) it is sub-millisecond, and it always returns the
    true nearest neighbours with no ANN recall trade-off to reason about. The old
    `ix_chunk_embedding_hnsw` never matched this ORDER BY and was dropped in
    `a3c7e1b90d42`; when the corpus outgrows the scan, the index to build is the
    one that matches this expression.
    """
    qv = get_embedder().embed([query], is_query=True)[0]
    distance = Chunk.embedding.cosine_distance(qv)
    stmt = select(Chunk.id, distance.label("distance")).order_by(distance).limit(_ARM_K)
    if source_ids:
        stmt = stmt.where(Chunk.source_id.in_(source_ids))
    # A zero vector (an un-re-embedded chunk, mid-migration) gives NaN, which
    # Postgres sorts LAST — so such rows only surface once the real hits are
    # exhausted, and `1.0 - NaN` then fails every floor comparison below.
    return [(cid, 1.0 - float(dist)) for cid, dist in db.execute(stmt).all()]


def _lexical_arm(db, query: str, source_ids: list[UUID] | None) -> list[tuple[UUID, float]]:
    if not source_ids:
        return get_index(db).search(query, k=_ARM_K)
    # The BM25 index covers the WHOLE corpus (it is one in-process structure, not
    # one per source subset), so a source scope is applied after ranking — and the
    # candidate pool is widened first, or a narrow `source_ids` would be left with
    # nothing after the filter.
    hits = get_index(db).search(query, k=_ARM_K * 4)
    if not hits:
        return []
    keep = set(
        db.scalars(
            select(Chunk.id).where(
                Chunk.id.in_([cid for cid, _ in hits]),
                Chunk.source_id.in_(source_ids),
            )
        ).all()
    )
    return [(cid, s) for cid, s in hits if cid in keep][:_ARM_K]


def _rrf(ranked: list[list[UUID]]) -> dict[UUID, float]:
    """Reciprocal Rank Fusion over N ranked id lists, equal weights."""
    fused: dict[UUID, float] = {}
    for ids in ranked:
        for rank, cid in enumerate(ids, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (_RRF_K + rank)
    return fused


def _passes_floor(hit: Hit, distinctive: set[str], matched: set[str]) -> bool:
    """Length AND noise-screen AND (confident dense OR complete lexical evidence).

    Measured on the real library, per axis — every row is a top hit:

        axis          top cos   lexical coverage of the query's distinctive terms
        ----------------------------------------------------------------------
        covered       0.880+    partial (0.5-1.0)   -> admitted by the DENSE arm
        gear          0.787+    COMPLETE (1.0)      -> admitted by the LEXICAL arm
        uncovered     0.862-    partial (0.33-0.67) -> admitted by NEITHER. Zero.
        adversarial   0.856     0.75                -> admitted by NEITHER. Zero.

    The two arms fail on opposite axes, which is exactly why the rule is an OR and
    exactly why neither could ship alone. The dense arm cannot recognise a bare
    model number ("5150" tops out at 0.787), and the lexical arm cannot recognise
    a paraphrase (a covered topic's best passage often shares only half the query's
    words). Each one rescues what the other cannot see.

    COMPLETE coverage, not partial, and that strictness is load-bearing. `vibrato`
    (11 chunks) and `fingerstyle` (4) DO occur in this library — in an effects list
    and a passing aside. A "matches any distinctive term" rule therefore admits
    them and reports both uncovered topics as covered. Demanding that a passage
    contain EVERY distinctive term the query used is what separates "the library
    mentions this word" from "the library is about this". And since a term with
    df=0 can never be matched (see `LexicalIndex.distinctive`), a query naming
    something the library has never once written about — `tablature`, `notation` —
    can NEVER pass on this arm at all, however many of its other words happen to
    land. That is gap detection, and it is the one part of this floor that is
    strong rather than merely calibrated.

    When `distinctive` is EMPTY — every word the query used is near-universal here
    ("how do I get a good sound?") — the lexical arm ABSTAINS (the subset test is
    trivially true) rather than rejecting everything, and length + cosine decide
    alone. Deliberate: a floor that returns nothing for every vaguely-worded
    question is not precision, it is a broken search box.
    """
    if len(hit.text) < MIN_PASSAGE_CHARS:
        return False
    if not (hit.vector_score >= WEAK_COSINE_FLOOR):  # NaN-safe: NaN fails every `>=`
        return False
    if hit.vector_score >= STRONG_COSINE:
        return True
    return distinctive <= matched


def search(
    db,
    query: str,
    *,
    k: int = 8,
    source_ids: list[UUID] | None = None,
    apply_floor: bool = True,
) -> list[Hit]:
    """Top-`k` chunks for `query`: normalise -> dense + BM25 -> RRF -> floor.

    `apply_floor=False` exists for exactly one caller —
    `scripts/retrieval_baseline.py`, which has to see what the floor REJECTS in
    order to keep calibrating it. It is not an escape hatch for application code.
    The whole point of Stage 4 is that `curriculum/ground.py` and `agent/loop.py`
    no longer own thresholds of their own: they each had one, the two disagreed,
    and one of them had been ported unchanged from a different embedding model. If
    you find yourself filtering `search()`'s output by score, the fix belongs in
    `_passes_floor`.
    """
    normalized = normalize_query(db, query)

    vector_hits = _vector_arm(db, normalized, source_ids)
    lexical_hits = _lexical_arm(db, normalized, source_ids)
    if not vector_hits and not lexical_hits:
        return []

    vec_by_id = dict(vector_hits)
    lex_by_id = dict(lexical_hits)
    fused = _rrf([[cid for cid, _ in vector_hits], [cid for cid, _ in lexical_hits]])
    ordered = sorted(fused, key=lambda cid: fused[cid], reverse=True)

    index = get_index(db)
    # Computed from the NORMALISED query: the corpus is English, so matching the
    # raw Greek against it would find nothing, mark every Greek word "distinctive
    # and absent", and reject every hit for every Greek question the tutor asks.
    distinctive = index.distinctive(normalized)

    # Backfill the cosine for lexical-only hits — `vector_score` is documented as
    # never None and the floor depends on it. One extra query, only for the ids the
    # dense arm's top-50 missed.
    missing = [cid for cid in ordered if cid not in vec_by_id]
    if missing:
        qv = get_embedder().embed([normalized], is_query=True)[0]
        distance = Chunk.embedding.cosine_distance(qv)
        for cid, dist in db.execute(
            select(Chunk.id, distance.label("d")).where(Chunk.id.in_(missing))
        ).all():
            vec_by_id[cid] = 1.0 - float(dist)

    rows = db.execute(
        select(Chunk, KnowledgeSource, Page)
        .join(KnowledgeSource, Chunk.source_id == KnowledgeSource.id)
        # outerjoin, not join: chunks from sources ingested before Plan 9 Task 1
        # legitimately have page_id=None and must still be searchable.
        .outerjoin(Page, Chunk.page_id == Page.id)
        .where(Chunk.id.in_(ordered))
    ).all()
    by_id = {chunk.id: (chunk, source, page) for chunk, source, page in rows}

    out: list[Hit] = []
    for cid in ordered:
        row = by_id.get(cid)
        if row is None:
            continue  # deleted between the arm queries and this fetch
        chunk, source, page = row
        hit = Hit(
            chunk_id=chunk.id,
            source_id=chunk.source_id,
            source_title=source.title,
            text=chunk.text,
            section_path=chunk.section_path,
            page=page.page_no if page is not None else None,
            page_id=chunk.page_id,
            score=fused[cid],
            vector_score=vec_by_id.get(cid, 0.0),
            lexical_score=lex_by_id.get(cid, 0.0),
        )
        if apply_floor and not _passes_floor(
            hit, distinctive, index.matched_terms(normalized, cid)
        ):
            continue
        out.append(hit)
        if len(out) >= k:
            break
    return out


# The two grounded-answer prompts, lifted out of the builder byte-identically so the
# tutor can rewrite them. The USER halves carry his query and his passages; they are
# left as plain templates rather than slices because there is no app-authored prose in
# them to edit — a slice over `"{query}\n\nContext:\n{context}"` would be a textarea
# containing three placeholders and one word.
GROUNDED_SYSTEM = (
    "Answer strictly from the provided context. Cite sources as [n]. If the "
    "context does not contain the answer, say so.\n\n"
    "{language_directive}"
)
GROUNDED_SLICE_ID = "retrieval.grounded"
GROUNDED_USER = "{query}\n\nContext:\n{context}\n\n{answer_in}"

NO_HITS_SYSTEM = (
    "The tutor's own library was searched and contains NOTHING relevant "
    "to this question. Answer it well from your general knowledge of "
    "guitar teaching. START your answer by saying, in the answer's own "
    "language, that his library does not cover this and what follows is "
    "general knowledge. Do NOT cite any sources — you were shown none.\n\n"
    "{language_directive}"
)
NO_HITS_SLICE_ID = "retrieval.no_hits"
NO_HITS_USER = "{query}\n\n{answer_in}"

TRANSLATE_SLICE_ID = "retrieval.translate"


def build_grounded_messages(
    query: str, hits: list[Hit], *, locale: str, source=None,
) -> list[dict]:
    """Pure function: the {system,user} chat messages for a grounded answer.

    Kept separate from `answer()` (which also calls the live model) so the prompt
    shape itself — the locale instruction, the citation instruction, the numbered
    context block — is unit-testable without a model or a DB.

    The locale instruction is `app.i18n.language_directive`, not a hand-written
    "answer in {locale}" sentence. This is THE cross-lingual case the directive
    exists for: the query is Greek, the `hits` are English, and a translated
    quotation would silently break the citation contract (`[n]` points at a real
    page whose real words are English). `answer_in` is repeated at the TAIL of the
    context block because that context is the last thing the model reads before
    writing, and it is entirely in the wrong language.
    """
    if hits:
        system = resolve(source, GROUNDED_SLICE_ID, GROUNDED_SYSTEM).format(
            language_directive=language_directive(locale, source),
        )
        context = "\n\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
        user = GROUNDED_USER.format(
            query=query, context=context, answer_in=answer_in(locale, source),
        )
    else:
        # ZERO HITS IS NOT A REFUSAL SCRIPT. The old prompt still said "answer
        # strictly from the provided context" over an EMPTY context — a billed
        # call whose only possible output was "it's not in there". The tutor's
        # explicit product rule is the opposite: when his library is silent,
        # the model takes over — LABELLED. So: answer from general knowledge,
        # open by saying the library doesn't cover it, cite nothing.
        system = resolve(source, NO_HITS_SLICE_ID, NO_HITS_SYSTEM).format(
            language_directive=language_directive(locale, source),
        )
        user = NO_HITS_USER.format(query=query, answer_in=answer_in(locale, source))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


@dataclass
class Answer:
    text: str
    citations: list[Hit]


def answer(db, query: str, *, locale: str = DEFAULT_LOCALE, k: int = 8) -> Answer:
    hits = search(db, query, k=k)
    messages = build_grounded_messages(query, hits, locale=locale, source=db)
    text = get_provider().chat(messages, enable_thinking=False)
    return Answer(text=text, citations=hits)
