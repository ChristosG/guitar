"""Retrieval grounding for curriculum authoring (Plan 12 Task 2, G1/G3).

Chris, verbatim, on the deployed app: "when i click 'Generate a curriculum'
seems like it just uses the llm general knowledge ... it would be beneficial
here to select things from our library." He is right —
`app.curriculum.generate.generate_curriculum` used to run exactly ONE
guided-JSON call, seeded with a single library search over the WHOLE course,
then never touched the library again while drafting every module's content.
This module is what lets each module's content actually come from HIS
material: `ground_topic` retrieves the passages a specific module should be
drafted from, with a relevance floor that keeps library junk out of the
prompt (see below).

THE RELEVANCE FLOOR — calibrated against the REAL deployed library (`guitar`
db), not picked by feel (this task's report has the full numbers). Findings:

1. Cosine score ALONE does not separate junk from real content in this
   corpus/embedder. Querying realistic curriculum-module topics ("pickup
   types and how they shape guitar tone", "amplifier gain staging", ...)
   against the live library:
     - real hits (the 77pp book, Wikipedia, real course pages) score ~0.60-0.75
     - a KNOWN-JUNK source — `truefire.com/.../v13776`, an 86-character
       unrendered page title ("Guitar Effects Survival Guide: Introduction -
       Jeff McErlain - Guitar Lesson - TrueFire") — scored as high as 0.68 on
       an "overdrive and distortion pedals" query and 0.62 on "compressor
       pedal technique". A score-only floor at 0.60 does NOT exclude it.
     - a second KNOWN-JUNK source — `truefire.com/.../kings-of-tone/c176`, a
       2,305-char page of unrendered `{{video.title}}`/`{{course.promo.code}}`
       JS template syntax — scored 0.55-0.57 on genuine tone-module queries,
       i.e. inside the SAME 0.5-0.75 band real content occupies.
   So score is necessary but not sufficient here.

2. Passage LENGTH is the discriminator score misses. Every identified junk
   snippet is short: the 86-char JS-template-title source, a 295-char
   Berklee enrollment-boilerplate source ("Proof of a bachelor's degree is
   required to enroll..."), and OCR artifacts on blank/table book pages ("PART
   ONE The Guitar" = 19 chars, "There is no visible text on this page." = 38
   chars). Real substantive passages in this corpus are consistently >=530
   chars (chunking targets ~1000-1200 chars; the shortest genuine hit
   observed across calibration was 532 chars). A 200-char floor comfortably
   excludes every piece of identified junk with wide margin while sitting
   well below every genuine passage observed.

   The one junk source LENGTH alone can't catch — the 1,119-1,197-char
   `{{video.title}}` template chunks — is exactly where the SCORE floor does
   its job: those chunks never cleared 0.60 on any genuine module-topic
   query tested (max seen: 0.566).

Combined: `score >= 0.60 AND len(text) >= 200` passed every real tone-module
query tried (pickups, amp gain, overdrive, picks, compressor: 3-15 passages
kept per query, all from the book/Wikipedia/real course pages) while
excluding all three identified junk sources on every query, INCLUDING
adversarial queries built from the junk's own vocabulary
("kings of tone course video download"). It also correctly returns nothing
for topics the library genuinely doesn't cover (tested against "vibrato and
legato technique" and "reading guitar tablature notation": 0 passages above
floor out of the top 15 raw hits each) — which is exactly the G3 gap
behaviour `generate_curriculum` depends on this module for.

Neither number is claimed to be universal — it's this corpus, this embedder
(Qwen3-Embedding-4B via `app.llm.embeddings`), calibrated against the
library Chris actually has today. Revisit if the corpus composition changes
materially (e.g. once his real course URLs replace the synthetic Course
Spine — see this plan's design doc).
"""
import uuid
from dataclasses import dataclass

from app.brain.retrieve import search

# See the module docstring above for the calibration run and real numbers
# behind these two values.
SCORE_FLOOR = 0.60
LEN_FLOOR = 200

# search() returns its top-k BEFORE the floor is applied, and the floor
# typically discards roughly half to two-thirds of raw hits in this corpus
# (see calibration numbers above) — overfetch so a caller asking for `k`
# grounding passages actually gets up to `k` post-floor, rather than fewer
# just because search()'s own top-k cutoff happened to land on hits the
# floor would have discarded anyway.
_OVERFETCH = 4


@dataclass
class Passage:
    """One retrieved, floor-cleared grounding passage for curriculum
    authoring. Mirrors `app.brain.retrieve.Hit`'s fields (this IS a Hit,
    reshaped) but under names `generate.py`'s provenance/prompt-building code
    owns independently of `Hit`'s own field names/defaults.
    """
    text: str
    source_id: uuid.UUID
    source_title: str
    page_no: int | None
    page_id: uuid.UUID | None
    score: float


def ground_topic(
    db, topic: str, *, source_ids: list[uuid.UUID] | None = None, k: int = 5,
    domain: str | None = None,
) -> list[Passage]:
    """Top-`k` grounding passages for `topic`, above the relevance floor
    (see module docstring), optionally scoped to `source_ids` (the sources
    the tutor chose to build this curriculum from) and/or `domain`.

    Wraps `app.brain.retrieve.search` — does not reimplement retrieval.
    `source_ids`, when given, filters `search`'s own hits down to those
    sources AFTER retrieval (searching over the full corpus, then keeping
    only the chosen sources) rather than pushing an `IN` clause into
    `search` itself: `search`'s existing `domain`/`language` filters already
    apply to the query stage, and adding a THIRD filter dimension there
    would grow that function's contract for a single caller. Retrieval is
    over-fetched (see `_OVERFETCH`) precisely because this post-filter (and
    the relevance floor below) can only shrink the candidate set, never grow
    it.

    `domain`, unlike `source_ids`, IS pushed straight into `search()`'s own
    `domain=` kwarg — a HARD SQL filter on `KnowledgeSource.domain` at the
    query stage (review fix, IMPORTANT: the old single-phase generator did
    exactly this, `search(db, query, k=12, domain=domain)`; the two-phase
    rewrite dropped it and instead mashed `domain` into the free-text query
    in `generate.py`, downgrading it from a hard filter to a soft semantic
    hint — a "theory" curriculum could then cite a well-scoring "tone"
    passage). `search`'s own `domain` filter already exists and does exactly
    this job, so this is restoring behaviour, not adding a new filter
    dimension.
    """
    raw_k = max(k * _OVERFETCH, k)
    hits = search(db, topic, k=raw_k, domain=domain)

    if source_ids is not None:
        allowed = set(source_ids)
        hits = [h for h in hits if h.source_id in allowed]

    passages = [
        Passage(
            text=h.text, source_id=h.source_id, source_title=h.source_title,
            page_no=h.page, page_id=h.page_id, score=h.score,
        )
        for h in hits
        if h.score >= SCORE_FLOOR and len(h.text) >= LEN_FLOOR
    ]
    passages.sort(key=lambda p: p.score, reverse=True)
    return passages[:k]
