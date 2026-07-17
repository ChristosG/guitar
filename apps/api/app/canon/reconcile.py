"""Pass 2 — reconcile concept names across books. Merge synonyms; discard nothing.

THIS PASS READS NAMES, NOT BOOKS, and that is why it is nearly free. Ten books
produce maybe 1,000 concept NAMES — "pickup height" / "pickup adjustment" /
"adjusting pickup height". `rapidfuzz` blocks the candidates deterministically
before the model sees anything, and the model only adjudicates the near-misses.
Nothing here re-reads a page.

---------------------------------------------------------------------------
THE ASYMMETRY IS THE WHOLE DESIGN.

C2 deliberately let every book name concepts in its OWN words. The spec is
explicit about why: *"a pre-fixed vocabulary is a filter, and a filter's failure
mode is dropping the unique take that justified buying the tenth book. Free-form
naming means reconciliation can only ever merge synonyms — it has no mechanism for
silent discard."*

That promise is kept HERE or nowhere. So every rule below is aimed down one
gradient:

    a WRONG merge  -> two teachings silently fused into one. The canon says one
                      thing where his books said two, no test downstream can see
                      it, and the information is gone.
    a MISSED merge -> the canon carries two concepts that could have been one.
                      Slightly redundant. Nothing lost. C4 renders both.

There is no operation in this module that deletes a claim, an alias, or a concept
anybody still supports. `concept_alias` is the receipt: every alias keeps its
`source_id`, so a bad merge is undone with a query instead of by re-spending ~$13
re-reading ten books.

---------------------------------------------------------------------------
`rapidfuzz` BLOCKS. IT DOES NOT DECIDE. MEASURED, ON THE PLAN'S OWN EXAMPLE.

The obvious shortcut is an auto-merge band: "above 90, just merge them". It is
wrong, and not marginally. Real synonyms and real distinctions occupy THE SAME
SCORE RANGE:

    SYNONYMS                                          ratio  WRatio
    "pickup adjustment"  ~ "adjusting pickup height"   45.0    71.2
    "string bending"     ~ "bending strings"           48.3    91.7
    "tonewoods"          ~ "tone woods"                94.7    94.7

    GENUINELY DIFFERENT — must never merge
    "major scale"        ~ "minor scale"               81.8    81.8
    "6th chord"          ~ "9th chord"                 88.9    88.9
    "hammer-ons"         ~ "hammer-ons and pull-offs"  58.8    90.0

A real synonym scores 45; two different chords score 88.9. **There is no
threshold that separates them at any point on the scale**, because the one
character that differs is frequently the entire meaning. So the fuzzy score is
used for the only thing it is honest for — CANDIDATE GENERATION — and every
surviving pair goes to a model that can read.

Blocking is therefore tuned for RECALL, not precision. `BLOCK_THRESHOLD` is low
on purpose: a pair blocked in error costs the model a moment's attention; a pair
missed is a merge that can never happen. Measured at 60 on a realistic 63-name
tone vocabulary: 61 edges, biggest cluster 15. At 55 the graph collapses into one
39-name blob (blocking's classic failure), and at 90 the plan's own trio stops
being connected.

---------------------------------------------------------------------------
THE MODEL PARTITIONS; IT DOES NOT VOTE ON PAIRS.

Pairwise adjudication has a transitivity trap: told A~B and B~C but A!~C, any
merge is arbitrary, and connected components would fuse A with C — a wrong merge
the model never agreed to. So the model is handed each blocked CLUSTER and asked
to PARTITION it. A partition is transitive by construction, so the trap cannot
arise and no reconciliation of contradictory pair votes is needed.
"""
from __future__ import annotations

import logging
from itertools import combinations
from uuid import UUID

from sqlalchemy import select
from rapidfuzz import fuzz

from app.llm.factory import get_provider
from app.models.canon import Concept, ConceptAlias, ConceptClaim
from app.models.knowledge import KnowledgeSource

log = logging.getLogger(__name__)

# See the module docstring. RECALL, not precision — the model is the filter, this
# is the sieve. `WRatio` rather than plain `ratio`: it is rapidfuzz's own combined
# scorer and it is the one that survives word ORDER, which is exactly the shape of
# this problem ("adjusting pickup height" vs "pickup height adjustment").
BLOCK_THRESHOLD = 60

# Past this, a cluster is a blocking collapse rather than a synonym set, and the
# model is being asked to partition a blob. It still gets asked — refusing would
# be a silent discard — but somebody should know.
_CLUSTER_WARN = 40


# ---------------------------------------------------------------------------
# Blocking — deterministic, free, and never the decider
# ---------------------------------------------------------------------------

def _names_of(concept: Concept, aliases: list[ConceptAlias]) -> list[str]:
    """Every string any book has ever used for this concept, including its label.

    ALL of them, not just `label_en`, and that matters after the first merge: a
    concept whose label is "pickup height" may carry the alias "adjusting pickup
    height" from another book, and the eleventh book's "height adjustment" blocks
    against the alias while scoring nothing against the label. Comparing labels
    alone would make reconcile quietly worse the more it had already merged.
    """
    return sorted({n for n in [concept.label_en, *(a.alias for a in aliases)]
                   if (n or "").strip()})


def _blocked_clusters(names_by_concept: dict[UUID, list[str]]) -> list[list[UUID]]:
    """Candidate clusters: concepts whose names are close enough to be worth a
    model's attention. Deterministic and content-derived — no DB order, no UUIDs.
    """
    ids = sorted(names_by_concept, key=lambda i: names_by_concept[i][0])
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in combinations(ids, 2):
        if find(a) == find(b):
            continue
        if any(fuzz.WRatio(x, y) >= BLOCK_THRESHOLD
               for x in names_by_concept[a] for y in names_by_concept[b]):
            parent[find(a)] = find(b)

    clusters: dict[UUID, list[UUID]] = {}
    for i in ids:
        clusters.setdefault(find(i), []).append(i)
    out = [sorted(c, key=lambda i: names_by_concept[i][0])
           for c in clusters.values() if len(c) > 1]
    out.sort(key=lambda c: names_by_concept[c[0]][0])
    for cluster in out:
        if len(cluster) > _CLUSTER_WARN:
            log.warning(
                "canon: blocking produced a %d-concept cluster — the fuzzy graph "
                "has probably collapsed; the model is being asked to partition a "
                "blob. First names: %s",
                len(cluster), [names_by_concept[i][0] for i in cluster[:5]])
    return out


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

RECONCILE_SYSTEM = (
    "You are tidying the index of a working guitar teacher's library. Several "
    "books have each been read and had their concepts written down, each book in "
    "its own words. Your ONLY job is to spot where two books named the SAME idea "
    "differently.\n\n"
    "Output ONLY the JSON matching the schema you are given — no prose, no "
    "markdown, no commentary outside the JSON object."
)

# THE PROMPT'S REAL JOB IS TO MAKE "LEAVE IT ALONE" THE COMFORTABLE ANSWER.
#
# A model handed a list of similar names and asked to group them will group them;
# that is the shape of the task and the pull is strong. But the cost here is
# lopsided and invisible from inside the call: fusing "6th chord" with "9th chord"
# deletes a teaching and nothing downstream can ever notice, while leaving two
# synonyms apart costs a duplicate row in an index. So the instruction says the
# quiet part out loud, and says it with the examples the measurement produced.
RECONCILE_TASK = (
    "Below are groups of concept names that LOOK similar. They came from "
    "different books, and the [brackets] say which book used each name.\n"
    "\n"
    "For each group, decide which names — if any — are the SAME IDEA under "
    "different words, and return those together.\n"
    "\n"
    "1. LOOKING SIMILAR IS NOT BEING THE SAME. These names were pre-filtered by "
    "spelling, not by meaning, so most pairs here are NOT synonyms. "
    "\"major scale\" and \"minor scale\" differ by two letters and are different "
    "concepts. So are \"6th chord\" and \"9th chord\", \"neck pickup\" and "
    "\"bridge pickup\", \"tube amp bias\" and \"tube amp basics\". A single "
    "character is often the entire idea.\n"
    "\n"
    "2. THE COST IS NOT SYMMETRIC, SO WHEN IN DOUBT, LEAVE THEM APART. If you "
    "merge two ideas that are different, one of this teacher's books loses its "
    "teaching and nobody will ever find out. If you leave two synonyms apart, his "
    "index has one extra line. Merge only where you are confident the two names "
    "would be the SAME entry in the same book's index.\n"
    "\n"
    "3. A BROADER NAME IS NOT A SYNONYM OF A NARROWER ONE. "
    "\"hammer-ons and pull-offs\" covers more than \"hammer-ons\"; merging them "
    "loses the pull-offs. Same for \"tone\" and \"amp tone\".\n"
    "\n"
    "4. IF ONE BOOK USED BOTH NAMES, that author drew a distinction between them "
    "on purpose. Leave them apart.\n"
    "\n"
    "5. Return ONLY groups of two or more names that ARE the same idea. Leave out "
    "everything else — a name you do not return is simply left alone, which is "
    "always a safe answer. Return every name exactly as it is written below."
)

RECONCILE_SYSTEM_SLICE_ID = "canon.reconcile.system"
RECONCILE_TASK_SLICE_ID = "canon.reconcile.task"

RECONCILE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "description": (
                "Only the sets of names that genuinely mean the same thing. "
                "Omit anything you are not confident about."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Two or more names, copied exactly, that are the same "
                            "idea in different words."
                        ),
                    },
                },
                "required": ["names"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["groups"],
    "additionalProperties": False,
}


def build_reconcile_messages(clusters: list[list[str]], books: dict[str, list[str]],
                             source=None) -> list[dict]:
    """`[system, the candidate groups, the task]`.

    The book titles are not decoration: *"who called it this"* is the whole
    question when deciding whether two books meant the same thing, and it is the
    evidence behind rule 4 — one author using both names is him drawing a
    distinction on purpose.
    """
    from app.prompts.overrides import resolve

    blocks = []
    for i, cluster in enumerate(clusters, start=1):
        lines = "\n".join(
            f"- {name}  [{', '.join(books.get(name, []))}]" for name in cluster)
        blocks.append(f"GROUP {i}:\n{lines}")
    return [
        {"role": "system", "content": resolve(source, RECONCILE_SYSTEM_SLICE_ID,
                                              RECONCILE_SYSTEM)},
        {"role": "user", "content": "\n\n".join(blocks)},
        {"role": "user", "content": resolve(source, RECONCILE_TASK_SLICE_ID,
                                            RECONCILE_TASK)},
    ]


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------

def _merge_into(db, canonical: Concept, loser: Concept) -> None:
    """Move everything the loser holds onto the canonical concept, THEN delete it.

    THE ORDER IS LOAD-BEARING AND IT IS NOT DEFENSIVE STYLE. Both `concept_claim`
    and `concept_alias` are ON DELETE CASCADE on `concept_id` (C1, deliberately —
    deleting a book must take its claims with it). So deleting the loser first
    takes its claims and its aliases with it, silently, and the only symptom is a
    canon quietly missing one book's entire take on a concept — with no error, and
    with the alias receipt that would have made it reversible gone too.
    """
    db.execute(
        ConceptClaim.__table__.update()
        .where(ConceptClaim.concept_id == loser.id)
        .values(concept_id=canonical.id)
    )
    db.execute(
        ConceptAlias.__table__.update()
        .where(ConceptAlias.concept_id == loser.id)
        .values(concept_id=canonical.id)
    )
    db.flush()
    if loser.label_el and not canonical.label_el:
        # Filling a NULL is additive. Overwriting would let whichever concept
        # happened to lose a merge rename the survivor.
        canonical.label_el = loser.label_el
    db.delete(loser)
    db.flush()


def _sort_key(concept: Concept, stats: dict) -> tuple:
    """Which concept of a group is the canonical one — a pure function of the
    CANON'S CONTENT, never of insertion order or of a UUID.

    Widest support first (the name the most books recognise is the best name for
    the merged idea), then the fullest ledger, then the key. The last one is not a
    formality: without a total, content-derived tie-break, "same inputs, same
    canonical keys" quietly becomes "same inputs and the same compile order", and
    re-compiling one book would silently rename concepts that C4 renders, C7 shows
    and C8 searches.
    """
    return (-stats[concept.id]["sources"], -stats[concept.id]["claims"], concept.key)


def reconcile(db) -> list[Concept]:
    """Merge the synonyms across every compiled book. Returns the canon's
    concepts, by key. THE C3 ENTRY POINT, and the shape C4-C8 consume.

        concepts = reconcile(db)     # idempotent; a second run is a no-op

    ONE model call, or none at all. `rapidfuzz` blocks first, and a canon with no
    near-misses costs nothing — wasted LLM spend is the top severity class in this
    plan and "nothing to adjudicate" must therefore cost nothing to discover.

    NEVER DISCARDS. Every claim and every alias survives a merge, reparented onto
    the canonical concept and still carrying its own `source_id`. A concept the
    model was never asked about, or that it declined to merge, comes out untouched.
    There is no path through this function that loses a book's teaching.

    RAISES: the provider's error propagates, and nothing has been written when it
    does — the reads and the blocking all happen before the call, and the merge
    all happens after. A half-applied merge over user data that cost ~$13 to
    produce would be worse than no merge at all: re-running could not repair it,
    because the evidence it would need has already moved.
    """
    concepts = db.scalars(select(Concept)).all()
    if not concepts:
        return []

    aliases_by_concept: dict[UUID, list[ConceptAlias]] = {}
    for alias in db.scalars(select(ConceptAlias)).all():
        aliases_by_concept.setdefault(alias.concept_id, []).append(alias)

    stats: dict[UUID, dict] = {}
    sources_by_concept: dict[UUID, set[UUID]] = {}
    for concept in concepts:
        rows = db.execute(
            select(ConceptClaim.source_id).where(ConceptClaim.concept_id == concept.id)
        ).all()
        sources_by_concept[concept.id] = {r[0] for r in rows}
        stats[concept.id] = {"claims": len(rows),
                             "sources": len(sources_by_concept[concept.id])}

    by_id = {c.id: c for c in concepts}
    names_by_concept = {
        c.id: _names_of(c, aliases_by_concept.get(c.id, [])) for c in concepts}

    clusters = _blocked_clusters(names_by_concept)
    if not clusters:
        log.info("canon: reconcile found no near-miss names among %d concepts — "
                 "nothing to adjudicate, nothing spent", len(concepts))
        return sorted(concepts, key=lambda c: c.key)

    # name -> the concept(s) that answer to it, and name -> which books used it.
    concept_of_name: dict[str, list[UUID]] = {}
    books_of_name: dict[str, list[str]] = {}
    titles = {s.id: (s.title or "") for s in db.scalars(select(KnowledgeSource)).all()}
    for cid in {i for cluster in clusters for i in cluster}:
        for name in names_by_concept[cid]:
            concept_of_name.setdefault(name, []).append(cid)
            said_by = sorted({titles.get(a.source_id, "")
                              for a in aliases_by_concept.get(cid, [])
                              if a.alias == name})
            books_of_name.setdefault(name, [])
            books_of_name[name].extend(t for t in said_by
                                       if t not in books_of_name[name])

    name_clusters = [[n for cid in cluster for n in names_by_concept[cid]]
                     for cluster in clusters]
    data = get_provider().guided_json(
        build_reconcile_messages(name_clusters, books_of_name, db),
        RECONCILE_SCHEMA,
    )

    merged = _apply(db, data, concept_of_name, by_id, stats, sources_by_concept)
    db.commit()
    survivors = sorted(db.scalars(select(Concept)).all(), key=lambda c: c.key)
    log.info("canon: reconcile merged %d concept(s) away; %d remain of %d",
             merged, len(survivors), len(concepts))
    return survivors


def _apply(db, data, concept_of_name, by_id, stats, sources_by_concept) -> int:
    """The model's answer -> merges, with every group checked on the way in.

    VALIDATED, NEVER TRUSTED — the same discipline as C2's citations, for the same
    reason: this writes to user data that cost real money to produce, and the
    model has no way to know what it would be destroying.
    """
    merged = 0
    seen: set[UUID] = set()
    groups = (data or {}).get("groups") or []
    for group in groups:
        if not isinstance(group, dict):
            continue
        names = [n for n in (group.get("names") or []) if isinstance(n, str)]

        # A name nobody wrote down is a hallucination. It must not be able to take
        # a real concept with it, so it is dropped and the rest of the group still
        # stands or falls on its own merits.
        ids: list[UUID] = []
        for name in names:
            for cid in concept_of_name.get(name, []):
                if cid not in ids:
                    ids.append(cid)
            if name not in concept_of_name:
                log.warning("canon: reconcile ignoring a name nobody wrote down: %r",
                            name)
        ids = [i for i in ids if i not in seen and i in by_id]
        if len(ids) < 2:
            continue

        members = sorted((by_id[i] for i in ids), key=lambda c: _sort_key(c, stats))
        canonical, rest = members[0], members[1:]
        claimed = set(sources_by_concept[canonical.id])
        seen.add(canonical.id)

        for loser in rest:
            clash = claimed & sources_by_concept[loser.id]
            if clash:
                # ONE BOOK, ONE NAME PER CONCEPT. If a single author used both
                # names, he drew that distinction on purpose and C3 is not the
                # pass that erases it — C2 already reconciled within each book,
                # having been told not to force two ideas together. Refusing is
                # also what keeps `UNIQUE (concept_id, source_id)` on
                # `concept_alias` true by construction rather than by luck.
                log.info(
                    "canon: NOT merging %r into %r — %r already names %r, so that "
                    "author distinguishes them",
                    loser.key, canonical.key,
                    ", ".join(sorted(str(s) for s in clash)), canonical.key)
                continue
            _merge_into(db, canonical, loser)
            claimed |= sources_by_concept[loser.id]
            seen.add(loser.id)
            merged += 1
    return merged
