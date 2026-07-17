"""Pass 3 — the canon block. Ten books, keyed by concept, with the disagreements
kept.

WHAT THIS IS FOR, IN THE OWNER'S WORDS:

    "10 books all of them talking for guitar TONE, with much information
     repeated, but also some unique perspectives from each writer. So whats the
     plan there to create the ultimate curriculum, combining the knowledge of the
     10 books all together?"

**THE DIVERGENCES ARE THE PRODUCT**, and this module is the only place they can
become visible. Ten tone books are ~80% the same content; C3 collapses that
redundancy at the CONCEPT level (2,500 names -> ~250 concepts). What survives
underneath is each author's own position — and where two of them contradict each
other, that contradiction is the one thing no single book could ever have given
him.

---------------------------------------------------------------------------
WHY THIS BEATS FULL-CONTEXT EVEN IF TEN BOOKS FIT.

The spec is blunt about it: *"Even if ten books fit, full-context would be the
wrong answer. It would read Hunter and Gallagher disagreeing about pickup height
and silently average them into consensus mush... nothing in the prompt asks the
model to notice that two of its 900,000 tokens contradict each other."*

The fix is not a smarter model. It is JUXTAPOSITION. Hunter's sentence and
Gallagher's sentence are 400,000 tokens apart in the library and forty characters
apart here:

    divergence: Tone Manual (Hunter): warns a low pickup kills sustain (S1 p.113)
             vs Guitar Tone (Gallagher): prefers a lower treble-side height (S2 p.201)

A model reading that cannot miss the disagreement. THAT is the feature — the
renderer's job is the juxtaposition, not the verdict.

---------------------------------------------------------------------------
CONSENSUS IS EARNED. DIVERGENCE IS THE DEFAULT. THE ASYMMETRY IS THE DESIGN.

Deciding that two positions "agree" is a semantic judgement, and this module
refuses to make it on a guess, because the two failure directions are not
comparable:

  * a MISSED consensus  -> both positions render, side by side, with both
                           citations. The canon is slightly larger. Nothing is
                           lost, and a reader can see they agree.
  * a FALSE consensus   -> two authors are merged into one line and ONE of them
                           gets the other's position attributed to him, WITH HIS
                           REAL PAGE NUMBER ON IT. That is a fabricated citation,
                           and it is exactly the failure this codebase is
                           architected against.

MEASURED, and it is why there is no fuzzy threshold here. Lexical similarity does
not track agreement — the one word that differs is usually the whole
disagreement:

    "major scale"                vs "minor scale"                WRatio 81.8
    "6th chord"                  vs "9th chord"                  WRatio 88.9
    "insists tone is in the hands" vs "insists tone is in the wood"   ~93

Any threshold low enough to collapse real paraphrases is high enough to collapse
those. So consensus requires EXACT equality of the normalised stance — two books
that said the same thing in the same words. Everything else is rendered as what
it is: two authors, two positions, two citations.

The redundancy still collapses; it just collapses in the right pass. C3 merges
the NAMES (2,500 -> ~250 concepts), which is the 80% the owner is describing.
What is left under each concept is each writer's own take, which is the part he
bought the tenth book for.

---------------------------------------------------------------------------
OUR DESCRIPTION OF A PHOTO IS NOT THE AUTHOR'S SENTENCE.

`brain/ocr.py:38-52` classifies every character of every page: inside a
`[FIGURE]` region is OURS, outside it is the page's own words. C2 recorded that
per claim as `grounding`. THIS MODULE IS THE LAST PLACE IT CAN BE LOST — the
canon is read by the model that writes the lesson, and a figure-grounded claim
rendered as an ordinary citation is our caption about to be quoted as Hunter's
prose, with a real page number on it. So the citation carries `FIGURE`, and the
block's own legend tells the reader what that means. Citable, never quotable.

---------------------------------------------------------------------------
THE SHAPE IS `LibraryContext`'S, DELIBERATELY AND EXACTLY.

C5 swaps this in for `library.text` inside `corpus.prefix_messages`. Same fields,
same meanings, same `page_index` contract that `draft.py` validates every citation
against — so the swap needs no downstream change. `page_index` is "what the model
was SHOWN", which here means THE PAGES THIS BLOCK ACTUALLY CITES, not the pages in
the database. A page the canon never named is a page the model never read.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.llm.factory import get_provider
from app.models.canon import Concept, ConceptClaim
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)

# How deeply a book treats a concept, as a sort key. `None` sorts last: a claim
# whose depth nobody recorded must not out-rank one a model deliberately called
# "primary".
_DEPTH_RANK = {"primary": 0, "secondary": 1, "mention": 2}

# A rendered position is a LABEL ON A POINTER, not the content. The content is the
# cited page, which Pass 5 hydrates verbatim. `stance` is already capped at 40
# chars by C2, so this only ever bites the fallback path (a claim whose stance the
# model omitted, where `text` — a whole sentence — stands in for it). The POINTER
# is never truncated; only the label is.
_POSITION_MAX = 160

_WS = re.compile(r"\s+")


@dataclass
class CanonContext:
    """The canon as one prompt block, plus what we measured. Mirrors
    `corpus.LibraryContext` field for field — see the module docstring.

    `page_index` is the contract `draft.py` validates citations against: a
    `{ref: {page_no, ...}}` map of every (source, page) pair THIS BLOCK NAMED. A
    citation outside it is not a citation, it is a hallucination that renders as a
    chip the tutor clicks — landing him on a page that does not say what the
    lesson claims. Worse than no citation, because it is one he will trust.
    """

    text: str
    token_count: int
    fits: bool
    sources: list[dict] = field(default_factory=list)   # {ref, id, title, pages, chars}
    page_index: dict[str, set[int]] = field(default_factory=dict)
    ref_to_source_id: dict[str, UUID] = field(default_factory=dict)
    concept_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.page_index

    def summary(self) -> str:
        """*"3 books · 214 concepts · 41,200 tokens · fits whole"* — shown before
        he spends anything."""
        n = len(self.sources)
        verdict = "fits whole" if self.fits else "TOO LARGE — will fall back to retrieval"
        return (f"{n} book{'s' if n != 1 else ''} · {self.concept_count:,} concepts · "
                f"{self.token_count:,} tokens · {verdict}")


# ---------------------------------------------------------------------------
# The notation, explained once, to the model that reads the block
# ---------------------------------------------------------------------------

# THE LEGEND IS NOT DECORATION. Two of the four lines below are load-bearing:
#
#   `FIGURE` — without this, the marker on a citation is noise the model ignores,
#   and our description of a photograph gets quoted as the author's sentence.
#
#   `divergence` — this is the sentence that makes the whole design work. A
#   full-context prompt cannot surface a disagreement because nothing ASKS. This
#   asks, in the one place a model reading the canon cannot miss it.
CANON_LEGEND = (
    "HOW TO READ THIS:\n"
    "  consensus:  these books say the same thing. Cite any of them.\n"
    "  divergence: THESE AUTHORS DISAGREE. This is the most valuable thing here — "
    "it is what a shelf of ten books gives that no single book can. Do NOT average "
    "them into one bland claim. Teach the disagreement: say who says what, and why "
    "a player might choose either.\n"
    "  only in:    just one book covers this. It is that author's own take.\n"
    "  depth:      the book to send someone to, and the pages that teach it.\n"
    "  (S1 p.47)   a real page of a real book. The teacher CLICKS these.\n"
    "  (S1 p.47 FIGURE)  OUR description of a picture/diagram/tab on that page — "
    "NOT the author's words. Cite it; never quote it as his sentence."
)


def _norm(text: str) -> str:
    """Two books saying the same thing in the same words, modulo typography.

    Case and whitespace only — DELIBERATELY not stemming, not synonyms, not a
    fuzzy ratio. Everything omitted here is something that could change meaning,
    and the cost of changing meaning in this direction is a fabricated citation
    (see the module docstring's measurements).
    """
    return _WS.sub(" ", (text or "").strip().lower()).strip(" .;:!")


def _page_ranges(pages) -> str:
    """`[57] -> "p.57"`, `[47, 48] -> "pp.47-48"`, `[110,111,112,118] ->
    "pp.110-112, 118"`.

    Runs are compacted, GAPS ARE NOT BRIDGED. Rendering [110, 118] as "pp.110-118"
    would put eight pages into the prompt that no claim supports and that
    `page_index` therefore rejects — the model would read a range and cite p.114,
    and the citation would be silently stripped. Say what is true.
    """
    ordered = sorted({p for p in pages if isinstance(p, int) and not isinstance(p, bool)})
    if not ordered:
        return ""
    runs: list[list[int]] = []
    for page in ordered:
        if runs and page == runs[-1][-1] + 1:
            runs[-1].append(page)
        else:
            runs.append([page])
    body = ", ".join(f"{r[0]}-{r[-1]}" if len(r) > 1 else str(r[0]) for r in runs)
    return f"{'p.' if len(ordered) == 1 else 'pp.'}{body}"


def _cite(ref: str, claim: ConceptClaim) -> str:
    """`S1 p.47`, or `S1 p.47 FIGURE` when the claim came from our description of
    a picture rather than from the author's own prose."""
    marker = " FIGURE" if claim.grounding == "figure" else ""
    return f"{ref} {_page_ranges(claim.pages)}{marker}"


def _position(claim: ConceptClaim) -> str:
    """What this author THINKS, in his emphasis — the thing C2 bought with the
    `stance` field and the reason the compile is not a summariser.

    Falls back to the claim text when a stance is missing, because a position with
    no label is still a position and dropping it would be the silent discard the
    whole design rules out.
    """
    position = (claim.stance or "").strip() or (claim.text or "").strip()
    if len(position) > _POSITION_MAX:
        position = position[: _POSITION_MAX - 1].rstrip() + "…"
    return position


def _label(name: str, value: str) -> str:
    return f"  {name + ':':<12}{value}"


# ---------------------------------------------------------------------------
# One concept
# ---------------------------------------------------------------------------

def _render_concept(concept: Concept, claims: list[ConceptClaim], refs: dict,
                    titles: dict, book_count: int) -> tuple[str, dict[str, set[int]]]:
    """One CONCEPT block, plus the (ref -> pages) it actually cited.

    The page map is RETURNED rather than accumulated globally on purpose: what
    goes into `page_index` is exactly what this function rendered, so the two
    cannot drift. A page in the index that no line named would be a page the model
    can cite without ever having been shown it.
    """
    lines = [f"CONCEPT: {concept.key}"]
    name = concept.label_en or concept.key
    if concept.label_el:
        # The tutor is Greek and `el` is the default locale — a canon he cannot
        # read the index of is a canon he cannot steer.
        name = f"{name} / {concept.label_el}"
    lines.append(_label("name", name))

    cited: dict[str, set[int]] = {}

    def note(claim: ConceptClaim) -> str:
        ref = refs[claim.source_id]
        cited.setdefault(ref, set()).update(
            p for p in claim.pages
            if isinstance(p, int) and not isinstance(p, bool))
        return ref

    # Group the positions by what was literally said. See the module docstring:
    # equality, never similarity.
    by_position: dict[str, list[ConceptClaim]] = {}
    for claim in claims:
        by_position.setdefault(_norm(_position(claim)), []).append(claim)

    consensus: list[list[ConceptClaim]] = []
    solo: list[ConceptClaim] = []
    for key in sorted(by_position):
        group = by_position[key]
        if len({c.source_id for c in group}) > 1:
            consensus.append(group)      # two DIFFERENT books, same words
        else:
            solo.extend(group)

    # Deterministic throughout: this text goes in the CACHED prefix, and a byte
    # that moves between two calls silently re-mints a 40K-token cache entry at
    # 1.25x. The only symptom would be an invoice a month later (`corpus.py`).
    consensus.sort(key=lambda g: (-len({c.source_id for c in g}),
                                  refs[g[0].source_id], _norm(_position(g[0]))))
    solo.sort(key=lambda c: (refs[c.source_id], min(c.pages or [0]),
                             _norm(_position(c))))

    for group in consensus:
        group = sorted(group, key=lambda c: (refs[c.source_id], min(c.pages or [0])))
        cites = ", ".join(_cite(note(c), c) for c in group)
        lines.append(_label("consensus", f"{_position(group[0])} → {cites}"))

    # A book that departs from what the others agreed on IS a divergence — it does
    # not need a second dissenter. And when nobody agreed on anything, every book
    # is a dissenter, which is the same thing.
    diverging = len({c.source_id for c in solo}) > 1 or (consensus and solo)
    if diverging:
        for i, claim in enumerate(solo):
            ref = note(claim)
            entry = f"{titles[claim.source_id]}: {_position(claim)} ({_cite(ref, claim)})"
            lines.append(_label("divergence", entry) if i == 0
                         else f"{'vs':>13} {entry}")
    else:
        # Exactly one book covers this concept. There is nobody to disagree with,
        # and calling that a "divergence" would be a lie — but it is still the
        # thing the owner asked for ("some unique perspectives from each writer"),
        # so it renders under its own honest label. What it must never do is vanish.
        for i, claim in enumerate(solo):
            ref = note(claim)
            entry = f"{titles[claim.source_id]}: {_position(claim)} ({_cite(ref, claim)})"
            lines.append(_label("only in", entry) if i == 0
                         else f"{'':>14}{entry}")

    lines.append(_label("coverage",
                        f"{len({c.source_id for c in claims})}/{book_count} books"))

    # WHICH BOOK TO SEND SOMEONE TO. This is what stops a lesson being drafted
    # from the book that name-checks a concept when another one has a chapter on it.
    deepest = min(
        {c.source_id for c in claims},
        key=lambda sid: (
            min(_DEPTH_RANK.get(c.depth, 3) for c in claims if c.source_id == sid),
            -len({p for c in claims if c.source_id == sid for p in c.pages}),
            refs[sid],
        ),
    )
    deep_pages = sorted({p for c in claims if c.source_id == deepest for p in c.pages})
    lines.append(_label("depth",
                        f"{refs[deepest]} {_page_ranges(deep_pages)} goes deepest"))
    return "\n".join(lines), cited


# ---------------------------------------------------------------------------
# The canon
# ---------------------------------------------------------------------------

def build_canon_context(db, source_ids: list[UUID] | None) -> CanonContext:
    """The compiled canon for the selected books, as ONE prompt block.

        <canon books="2">
        S1 = Tone Manual (Hunter)
        S2 = Guitar Tone (Gallagher)
        ...
        CONCEPT: pickup-height
          name:       Pickup height / Ύψος μαγνήτη
          divergence: Tone Manual (Hunter): warns a low pickup kills sustain (S1 p.113)
                   vs Guitar Tone (Gallagher): prefers a lower treble-side height (S2 p.201)
          coverage:   2/2 books
          depth:      S1 pp.110-118 goes deepest
        </canon>

    `source_ids=None` means the whole library — the honest reading of an unscoped
    request. An empty LIST means "none", which is a different and deliberate
    answer, and is respected as one (`corpus.build_library_context` does exactly
    this, and C5 swaps one for the other).

    READS THE LEDGER, NEVER THE PAGES. This is the property that makes the canon
    flat in book count: a 388-page book and a 57-page book cost whatever their
    CONCEPTS cost, and a second book covering the same ground adds citations
    rather than a second copy of the content.

    Does NOT call `reconcile` (C3) and does not compile. This is a render of what
    is already on disk — reading ten books cost real money on the tutor's own
    subscription, and a render that could trigger a re-read would be the top
    severity class in this plan.
    """
    stmt = select(KnowledgeSource).order_by(
        # `created_at` matches `corpus.build_library_context`. `id` is the
        # tie-break it lacks and this needs: two books ingested in the same
        # transaction share a timestamp exactly, and an unstable ref assignment
        # would re-mint the cached prefix at 1.25x for nothing.
        KnowledgeSource.created_at, KnowledgeSource.id)
    if source_ids is not None:
        if not source_ids:
            return CanonContext(text="", token_count=0, fits=True)
        stmt = stmt.where(KnowledgeSource.id.in_(source_ids))
    sources = db.scalars(stmt).all()
    selected = {s.id for s in sources}

    claims = db.scalars(
        select(ConceptClaim).where(ConceptClaim.source_id.in_(selected))
    ).all() if selected else []
    if not claims:
        return CanonContext(text="", token_count=0, fits=True)

    # Only books that actually contributed get a ref. A selected book with no
    # ledger is not a book here, it is a row — the same call `corpus` makes about
    # a source with no readable text. It also keeps `coverage: 9/10` honest: the
    # denominator is the books in THIS canon, not the books someone ticked.
    contributing = [s for s in sources if any(c.source_id == s.id for c in claims)]
    refs = {s.id: f"S{i}" for i, s in enumerate(contributing, start=1)}
    titles = {s.id: (s.title or "") for s in contributing}

    by_concept: dict[UUID, list[ConceptClaim]] = {}
    for claim in claims:
        by_concept.setdefault(claim.concept_id, []).append(claim)

    concepts = db.scalars(
        select(Concept).where(Concept.id.in_(by_concept))
    ).all() if by_concept else []

    # MOST-COVERED FIRST — the concepts many books touch are the ones most likely
    # to carry a disagreement, and a disagreement is the reason this block exists.
    # `key` last, so the order is a pure function of the canon's content.
    concepts.sort(key=lambda c: (-len({x.source_id for x in by_concept[c.id]}),
                                 -len(by_concept[c.id]), c.key))

    blocks: list[str] = []
    page_index: dict[str, set[int]] = {}
    for concept in concepts:
        rendered, cited = _render_concept(
            concept, sorted(by_concept[concept.id],
                            key=lambda c: (refs[c.source_id], min(c.pages or [0]),
                                           _norm(_position(c)))),
            refs, titles, len(contributing))
        blocks.append(rendered)
        for ref, pages in cited.items():
            page_index.setdefault(ref, set()).update(pages)

    header = "\n".join(f"{refs[s.id]} = {titles[s.id]}" for s in contributing)
    text = (f'<canon books="{len(contributing)}" concepts="{len(concepts)}">\n'
            f"{header}\n\n{CANON_LEGEND}\n\n" + "\n".join(blocks) + "\n</canon>")
    token_count = _count_tokens(text)

    meta = []
    for source in contributing:
        ref = refs[source.id]
        own = [c for c in claims if c.source_id == source.id]
        meta.append({
            "ref": ref,
            "id": str(source.id),
            "title": source.title,
            "pages": len(page_index.get(ref, ())),
            "chars": sum(len(_position(c)) for c in own),
            "concepts": len({c.concept_id for c in own}),
        })

    return CanonContext(
        text=text,
        token_count=token_count,
        fits=token_count <= settings.full_context_budget,
        sources=meta,
        page_index=page_index,
        ref_to_source_id={ref: sid for sid, ref in refs.items()},
        concept_count=len(concepts),
    )


def _count_tokens(text: str) -> int:
    """Exact when the provider can tell us, estimated when it cannot. NEVER raises
    — same contract and same reasoning as `corpus._count_tokens`, bound to THIS
    module's `get_provider` so the seam is monkeypatchable in one place per
    module."""
    if not text:
        return 0
    try:
        return get_provider().count_tokens(text)
    except Exception:
        log.warning("count_tokens failed; falling back to the local estimate",
                    exc_info=True)
        return len(text) // 3 + 1
