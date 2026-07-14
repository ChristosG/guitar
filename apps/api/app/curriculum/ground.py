"""Retrieval grounding for curriculum authoring (Plan 12 Task 2, G1/G3).

Chris, verbatim, on the deployed app: "when i click 'Generate a curriculum'
seems like it just uses the llm general knowledge ... it would be beneficial
here to select things from our library." He was right — `generate_curriculum`
used to run ONE guided-JSON call, seeded with a single library search over the
whole course, then never touch the library again while drafting every module's
content. `ground_topic` is what makes each module's content come from HIS
material: it retrieves the passages a specific module should be drafted from.

THIS MODULE NO LONGER OWNS A RELEVANCE FLOOR, AND THAT IS THE POINT (Plan 13,
Stage 4.4). It used to own two — `SCORE_FLOOR = 0.60` and `LEN_FLOOR = 200` —
calibrated against Qwen3-Embedding-4B. Under multilingual-e5-small the score
distribution compresses into roughly 0.78-0.92, so 0.60 would have passed
EVERYTHING: every gap reported as covered, silently, by a constant that still
looked deliberate. Meanwhile `agent/loop.py` owned a THIRD floor (0.15) that
agreed with neither.

So the floor moved into `app.brain.retrieve.search` — the one function that knows
which embedding model produced the numbers it is thresholding — and there is now
exactly one of it. `ground_topic` retrieves and reshapes; it does not judge. When
a topic is uncovered, `search` returns nothing and this returns an empty list,
which is precisely the G3 gap signal `generate.py` depends on.

`domain` is gone too. It was a hard SQL filter on `KnowledgeSource.domain`, a
field 12 of the 16 real sources leave NULL, and `search` no longer has such a
filter at all (see its docstring: a model-chosen filter that can silently empty
the corpus is not a feature). A curriculum is scoped by `source_ids` — which the
TUTOR picks.
"""
import uuid
from dataclasses import dataclass

from app.brain.retrieve import search


@dataclass
class Passage:
    """One retrieved grounding passage for curriculum authoring.

    This IS an `app.brain.retrieve.Hit`, reshaped — under field names
    `generate.py`'s provenance/prompt-building code owns independently of `Hit`'s
    own names and defaults.

    `score` is the COSINE (`Hit.vector_score`), NOT `Hit.score` — which is now an
    RRF fusion score, an ordering key with no absolute meaning (see `retrieve`'s
    docstring). Nothing may threshold this one either; it is carried for display
    and provenance, so a tutor looking at a citation chip can see how close the
    match was.
    """
    text: str
    source_id: uuid.UUID
    source_title: str
    page_no: int | None
    page_id: uuid.UUID | None
    score: float


def ground_topic(
    db, topic: str, *, source_ids: list[uuid.UUID] | None = None, k: int = 5,
) -> list[Passage]:
    """Top-`k` grounding passages for `topic`, optionally scoped to the sources
    the tutor chose to build this curriculum from.

    Wraps `app.brain.retrieve.search` — it does not reimplement retrieval, and it
    does not filter the results. `source_ids` is pushed INTO `search` (a real SQL
    scope on the dense arm, a post-rank scope on the lexical one) rather than
    applied afterwards, so a narrow selection still yields up to `k` passages
    instead of whatever happens to survive from a corpus-wide top-k.
    """
    hits = search(db, topic, k=k, source_ids=source_ids)
    return [
        Passage(
            text=h.text, source_id=h.source_id, source_title=h.source_title,
            page_no=h.page, page_id=h.page_id, score=h.vector_score,
        )
        for h in hits
    ]
