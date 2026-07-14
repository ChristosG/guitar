"""The lexical arm of hybrid retrieval: Okapi BM25 over the whole chunk corpus.

WHY THIS EXISTS — it is not "belt and braces on top of the vectors". It is the arm
that fixes a measured, confident failure. Against the real 408-chunk library, on
the four gear queries in `scripts/retrieval_baseline.py`, asking only "does ANY
retrieved chunk actually CONTAIN the words asked for?":

    dense-only, top-10:  2/4        dense-only, rank #1:  1/4
    hybrid,     rank #1: 4/4

"Tube Screamer" is printed in the tutor's own book. The dense arm's top hit for
that exact query scores 0.837 and does not contain it — it returns something that
*sounds like pedal talk*. A dense model has no mechanism to prefer the document
that literally says the words, and it does not know that it doesn't. BM25 cannot
fail that way: a rare token either occurs in a document or it does not.

THE TOKENIZER IS THE WHOLE TRICK, and it is not a stock one.

  1. GEAR NAMES SURVIVE. A tutor writes the same pedal three ways — `TS-808`,
     `TS 808`, `TS808` — and his book prints a fourth. A tokenizer that splits
     on punctuation turns the first into {ts, 808} and leaves the third as the
     single opaque token {ts808}: two spellings of one pedal that share not one
     term. So a mixed alpha/digit token emits BOTH its whole form and its
     alpha/digit segments: `ts-808` and `ts808` both yield {ts-808|ts808, ts,
     808}, which overlap on {ts, 808}, while `TS 808` yields {ts, 808} directly.
     All three now retrieve the same chunk.

  2. A TOKEN WITH A DIGIT IS NEVER STEMMED. Snowball is a *linguistic* stemmer;
     handed `5150` (an amp) or `808` it is being asked a question it was not
     built for, and the answer it gives is not one we want in an index whose
     entire job here is exact model-name matching.

  3. GREEK IS FOLDED, THEN STEMMED. `fold()` (see `app.text.normalize`) strips
     accents and unifies final sigma; Snowball's Greek stemmer then takes
     `κιθάρας`/`κιθάρα` -> `κιθαρ`. Both steps are needed and neither is
     sufficient: fold alone leaves `κιθαρας != κιθαρα`, and Snowball's Greek
     algorithm is specified over unaccented lowercase input — hand it `κιθάρας`
     and it silently under-stems.

STALENESS, not invalidation. The index is a plain in-process dict; the corpus
behind it changes whenever a source is ingested, re-OCR'd, re-embedded or
deleted. Rather than wire an `invalidate()` call into every one of those paths
(and forget one), `get_index()` cheaply fingerprints the corpus — `COUNT(*)` +
`MAX(created_at)` over `chunk`, one aggregate, ~1ms — and rebuilds when it
moves. Correct by construction, at the cost of one trivial query per search.
`app.main`'s lifespan calls `warm_index()` so the tutor's FIRST question does
not pay the ~200ms build.
"""
from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from uuid import UUID

import Stemmer
from sqlalchemy import func, select

from app.models.knowledge import Chunk
from app.text.normalize import fold, has_greek

log = logging.getLogger(__name__)

# Okapi BM25's two knobs, at their standard values. Not tuned — there is no
# labelled relevance set on this corpus to tune them against, and pretending
# otherwise by shipping a hand-picked 1.37 would be theatre.
_K1 = 1.5
_B = 0.75

# A word is a run of letters/digits, optionally hyphen-joined (`ts-808`,
# `humbucker`, `single-coil`). Everything else is a separator. `\w` is avoided
# on purpose: it would swallow `_` and, under `re.UNICODE`, is fine for Greek —
# but being explicit about the two scripts this corpus actually contains keeps
# the token set inspectable.
_WORD_RE = re.compile(r"[a-z0-9Ͱ-Ͽἀ-῿]+(?:-[a-z0-9Ͱ-Ͽἀ-῿]+)*")

# Splits a mixed alpha/digit run at every letter<->digit boundary: `ts808` ->
# ["ts", "808"]. See point 1 of the module docstring.
_ALPHANUM_SEG_RE = re.compile(r"[a-zͰ-Ͽἀ-῿]+|[0-9]+")

_DIGIT_RE = re.compile(r"[0-9]")

_EN_STEMMER = Stemmer.Stemmer("english")
_EL_STEMMER = Stemmer.Stemmer("greek")


def tokenize(text: str) -> list[str]:
    """Fold -> split -> (expand gear names) -> stem. See the module docstring.

    Returns a term LIST, not a set: BM25 needs term frequencies, and a chunk
    that says "Tube Screamer" four times is more about the Tube Screamer than
    one that mentions it once.
    """
    terms: list[str] = []
    for word in _WORD_RE.findall(fold(text)):
        if _DIGIT_RE.search(word):
            # A model name. Emit the whole form (so an exact `ts-808` match
            # scores twice) plus its segments (so `ts808` and `TS 808` can
            # meet it halfway). NEVER stemmed.
            terms.append(word)
            segs = [s for part in word.split("-") for s in _ALPHANUM_SEG_RE.findall(part)]
            if segs != [word]:
                terms.extend(segs)
            continue
        if len(word) < 2:
            continue  # a lone letter carries no retrieval signal, only noise
        stemmer = _EL_STEMMER if has_greek(word) else _EN_STEMMER
        terms.append(stemmer.stemWord(word))
    return terms


@dataclass
class LexicalIndex:
    """An immutable BM25 index over the chunk corpus. Built by `_build`."""

    chunk_ids: list[UUID]
    doc_terms: list[Counter]          # term -> tf, per document
    doc_len: list[int]
    df: Counter                       # term -> number of documents containing it
    avg_len: float
    fingerprint: tuple[int, object]
    pos: dict[UUID, int]              # chunk_id -> its row in the lists above

    @property
    def n_docs(self) -> int:
        return len(self.chunk_ids)

    def idf(self, term: str) -> float:
        """The non-negative Robertson/Sparck-Jones IDF (`log(1 + ...)`).

        The classic form goes NEGATIVE for a term present in more than half the
        corpus, which lets a common term *subtract* from a document's score —
        harmless in a web index, actively wrong in a 408-chunk corpus where
        "guitar" is in most documents and would push the guitar chunks DOWN.
        """
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int) -> list[tuple[UUID, float]]:
        """Top-`k` (chunk_id, bm25_score). Only documents that share at least
        one query term are scored, so this is O(postings), not O(corpus)."""
        q_terms = tokenize(query)
        if not q_terms or self.n_docs == 0:
            return []
        scored: list[tuple[UUID, float]] = []
        for i, tf in enumerate(self.doc_terms):
            score = 0.0
            for term in set(q_terms):
                f = tf.get(term, 0)
                if not f:
                    continue
                norm = f * (_K1 + 1) / (
                    f + _K1 * (1 - _B + _B * self.doc_len[i] / self.avg_len)
                )
                score += self.idf(term) * norm
            if score > 0.0:
                scored.append((self.chunk_ids[i], score))
        scored.sort(key=lambda p: p[1], reverse=True)
        return scored[:k]

    def matched_terms(self, query: str, chunk_id: UUID) -> set[str]:
        """Which of the query's own terms this chunk actually contains.

        `retrieve.py`'s floor asks this question directly, and it is not the
        same question as "did BM25 score it above zero" — a chunk can score on
        a single near-universal term ("guitar") while containing nothing
        distinctive the query asked about. See `retrieve._passes_floor`.
        """
        i = self.pos.get(chunk_id)
        if i is None:
            return set()
        tf = self.doc_terms[i]
        return {t for t in tokenize(query) if t in tf}

    def distinctive(self, query: str, *, common_df_fraction: float = 0.10) -> set[str]:
        """The query terms THIS corpus can actually discriminate on.

        A term is distinctive unless it is near-universal here (`guitar` is in 278
        of 408 chunks; matching it tells you nothing). Measured against the real
        library, that puts the cut at 10% of documents — see
        `retrieve._passes_floor` for the table.

        **A TERM WITH df=0 IS DISTINCTIVE.** This is the counter-intuitive half,
        and it is the entire gap-detection mechanism: `tablature` and `notation`
        appear in ZERO chunks of the tutor's library, so a query about reading
        tablature carries two terms nothing can ever match — which is precisely
        what "your library does not cover this" MEANS. Silently dropping absent
        terms (the obvious implementation) throws away the only unambiguous
        evidence in the whole system, and lets `reading guitar tablature notation`
        pass on the strength of matching the word "read".
        """
        cutoff = common_df_fraction * self.n_docs
        return {t for t in tokenize(query) if self.df.get(t, 0) <= cutoff}


def _build(chunk_ids: list[UUID], texts: list[str], fingerprint: tuple[int, object]) -> LexicalIndex:
    doc_terms = [Counter(tokenize(t)) for t in texts]
    doc_len = [sum(c.values()) for c in doc_terms]
    df: Counter = Counter()
    for c in doc_terms:
        df.update(c.keys())
    avg_len = (sum(doc_len) / len(doc_len)) if doc_len else 1.0
    return LexicalIndex(
        chunk_ids=chunk_ids,
        doc_terms=doc_terms,
        doc_len=doc_len,
        df=df,
        avg_len=max(avg_len, 1.0),
        fingerprint=fingerprint,
        pos={cid: i for i, cid in enumerate(chunk_ids)},
    )


_index: LexicalIndex | None = None
_lock = threading.Lock()


def _fingerprint(db) -> tuple[int, object]:
    """(row count, newest chunk) — moves on any ingest, re-OCR, re-embed or
    delete. Deliberately NOT a hash of the corpus text: that would cost a full
    scan on every search, which is the thing this fingerprint exists to avoid."""
    row = db.execute(select(func.count(Chunk.id), func.max(Chunk.created_at))).one()
    return (int(row[0] or 0), row[1])


def get_index(db) -> LexicalIndex:
    """The process-wide BM25 index, rebuilt iff the corpus moved."""
    global _index
    fp = _fingerprint(db)
    if _index is not None and _index.fingerprint == fp:
        return _index
    with _lock:
        if _index is not None and _index.fingerprint == fp:
            return _index  # another thread won the race and built the same index
        rows = db.execute(select(Chunk.id, Chunk.text).order_by(Chunk.id)).all()
        _index = _build([r[0] for r in rows], [r[1] or "" for r in rows], fp)
        log.info("BM25 index built: %d chunks, %d terms", _index.n_docs, len(_index.df))
        return _index


def warm_index(db) -> None:
    """Called from `app.main`'s lifespan. Building the index inside the first
    chat request would put a ~200ms corpus scan on the tutor's first question
    and nowhere else — the classic "it's slow the first time and nobody knows
    why" bug."""
    try:
        get_index(db)
    except Exception:
        # A cold BM25 index is a degraded search, not a dead app: `get_index`
        # will simply try again on the first real query.
        log.warning("BM25 warm-up failed; the index will build on first use", exc_info=True)


def reset_index() -> None:
    """Test hook. Production never needs this — `get_index` self-invalidates."""
    global _index
    _index = None
