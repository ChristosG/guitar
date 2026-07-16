"""Repair a `KnowledgeSource` that reports `status="ready"` (so, per spec
D6, it genuinely has content) but has zero `Page` rows — the Reader has
nowhere to open, exactly the bug Chris hit live on "Guitar Tone & Gear —
Course Spine" (ingested in Plan 7, before the Page model existed).

This can only happen to data ingested BEFORE `paginate_source` was wired
into `ingest_source` (Plan 9 Task 5): every source that goes through
`ingest_source` since then gets its Page row(s) created FIRST, and only
THEN can `status` ever become "ready" (see `app/brain/ingest.py`'s ordering)
— so "ready implies >=1 Page" holds by construction for anything ingested
through the normal pipeline. That invariant is pinned by
`tests/test_ingest_status.py::test_real_content_still_becomes_ready_with_a_page`
and `tests/test_ready_implies_page_invariant.py`. This module exists purely
to heal the pre-existing rows that predate that guarantee (and to fail
closed, not crash, if some future bug ever reintroduces the shape) rather
than leaving the Reader a permanent dead end — see `routers/library.py`'s
`_ensure_pages_exist`, the call site.

A `kind="url"` source can be healed the honest way: re-fetch its own stored
`KnowledgeSource.url` (Plan 9 Task 1), exactly what `POST .../retry` already
does for a *failed* url source. Anything else (text/note/image, or a url
row that somehow has no stored url, or a pdf whose original bytes were
never retained) has no original left to re-fetch — but its text survives,
split and overlapping, across the source's own Chunk rows
(`chunk_sections`' sliding window, `app/brain/chunk.py`). `reassemble_chunk_texts`
below reverses that window, and `repair_pageless_source` feeds the result
back through the SAME `ingest_source` pipeline as a fresh kind="text"
ingest, which re-paginates (creating the missing Page), re-chunks, and
re-embeds — the repaired row ends up indistinguishable from one that had a
Page from day one, modulo losing any original scan (a pdf's raster images
were never rendered for this legacy shape in the first place, so there is
nothing to lose that still exists).
"""
import logging

from sqlalchemy.orm import Session

from app.models.knowledge import Chunk, KnowledgeSource

log = logging.getLogger(__name__)

# Matches chunk_sections' own default overlap (app/brain/chunk.py) — used
# only as the upper bound of the search below; a real overlap shorter than
# this (e.g. the tail of a short section) is still found, since the search
# tries every length up to the bound, longest first.
_MAX_OVERLAP_SEARCH = 300
# Below this many characters, treat an apparent suffix/prefix match as
# coincidental common text (e.g. a shared "the ") rather than a genuine
# chunk_sections overlap.
_MIN_OVERLAP_TO_TRUST = 20


def _overlap_len(a: str, b: str) -> int:
    """Longest k (<= _MAX_OVERLAP_SEARCH) such that a's last k characters
    equal b's first k characters. 0 if none found (or only a trivial match
    below _MIN_OVERLAP_TO_TRUST)."""
    cap = min(len(a), len(b), _MAX_OVERLAP_SEARCH)
    for k in range(cap, _MIN_OVERLAP_TO_TRUST - 1, -1):
        if a[-k:] == b[:k]:
            return k
    return 0


def order_chunk_texts(texts: list[str]) -> list[str]:
    """Reconstruct the original document order of a set of overlapping
    chunk texts. `Chunk` carries no order column (nothing needed one until
    now — see the module docstring), so DB row order is not trustworthy
    across a dump/restore or a VACUUM. Instead, chain chunks by MUTUAL best
    suffix/prefix overlap match: for a real `chunk_sections` run, chunk i's
    tail and chunk i+1's head are byte-identical over exactly
    `overlap_chars`, and nothing else in the set matches as well.

    Only reorders when the chunks form exactly ONE chain covering every
    input chunk — the shape a single `chunk_sections` run over one
    contiguous block of text always produces. If they don't (multiple
    Sections with no overlap between them, chunks that aren't from the same
    document, or a rare zero-overlap transition inside `chunk_sections`
    itself — see that module's `next_start <= start` branch), this gives up
    and returns the input order unchanged, logged, rather than guessing at
    an order it can't actually prove.
    """
    n = len(texts)
    if n <= 1:
        return list(texts)

    best_next: dict[int, tuple[int, int]] = {}  # i -> (best j, overlap len)
    best_prev: dict[int, tuple[int, int]] = {}  # j -> (best i, overlap len)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            ov = _overlap_len(texts[i], texts[j])
            if ov == 0:
                continue
            if ov > best_next.get(i, (-1, -1))[1]:
                best_next[i] = (j, ov)
            if ov > best_prev.get(j, (-1, -1))[1]:
                best_prev[j] = (i, ov)

    # Keep edge i->j only when it is each other's mutual best match — avoids
    # chaining on an ambiguous or weaker overlap.
    edges: dict[int, int] = {
        i: j for i, (j, _ov) in best_next.items() if best_prev.get(j, (-1, -1))[0] == i
    }

    heads = [i for i in range(n) if i not in edges.values()]
    if len(heads) != 1:
        log.warning(
            "order_chunk_texts: %d chunks do not form a single chain (%d chain heads found); "
            "falling back to input order",
            n, len(heads),
        )
        return list(texts)

    order = [heads[0]]
    seen = {heads[0]}
    node = heads[0]
    while node in edges:
        node = edges[node]
        if node in seen:  # cycle guard — should be unreachable, defense only
            log.warning("order_chunk_texts: cycle detected; falling back to input order")
            return list(texts)
        seen.add(node)
        order.append(node)

    if len(order) != n:
        log.warning(
            "order_chunk_texts: chain covers %d of %d chunks; falling back to input order",
            len(order), n,
        )
        return list(texts)

    return [texts[i] for i in order]


def reassemble_chunk_texts(ordered_texts: list[str]) -> str:
    """Join chunk texts already in document order back into one string,
    deduplicating the overlap between each consecutive pair. A pair with no
    detectable overlap is concatenated directly with no separator inserted
    — chunk_sections never adds characters that weren't in the original, so
    reassembly must not either (a real, if rare, zero-overlap transition can
    happen inside a single Section — see `order_chunk_texts`'s docstring;
    inserting a synthetic "\\n\\n" there would fabricate whitespace that was
    never in the source text)."""
    if not ordered_texts:
        return ""
    out = ordered_texts[0]
    for chunk_text in ordered_texts[1:]:
        ov = _overlap_len(out, chunk_text)
        out = out + chunk_text[ov:]
    return out


def _delete_orphaned_chunks(db: Session, source_id) -> None:
    """Delete every existing Chunk row for `source_id` before re-ingesting.

    `Chunk.page_id` FKs to `page.id`; a source with ZERO Pages (this
    function only ever runs on one) cannot have a single properly-linked
    Chunk, so every Chunk row it has is already an orphan (`page_id IS
    NULL`) — the exact leftover of the legacy pre-Page-model ingest this
    module heals. `paginate_source`'s own cleanup (app/brain/paginate.py)
    only cascades away Chunks attached to a Page it is about to delete;
    since there is no Page here for that cascade to key off of, it would
    never touch these, and `ingest_source` would otherwise layer a fresh,
    correctly-linked set of Chunks on TOP of the orphans instead of
    replacing them — silently doubling the source's retrieval index (found
    live: repairing "Course Spine" without this left 15 stale orphaned
    Chunks sitting alongside the 15 fresh ones). Bulk delete + `expire_all`,
    same idiom as `paginate_source`'s own Page replacement.
    """
    db.query(Chunk).filter(Chunk.source_id == source_id).delete(synchronize_session=False)
    db.expire_all()


def repair_pageless_source(db: Session, source: KnowledgeSource) -> bool:
    """Give `source` its missing Page(s) back.

    Returns True if a repair was actually performed, False if there was
    nothing to repair FROM (no stored url and no Chunks at all) — the
    latter should be unreachable for a genuinely "ready" source per spec
    D6 (char_count > 0 implies chunks were persisted), but this stays a
    no-op rather than fabricating content out of nothing.

    Caller's responsibility (see `routers/library.py::_ensure_pages_exist`)
    to have already checked `source.status == "ready"` and that it
    currently has zero Pages — this function does not re-check either.
    """
    # Local import: avoids a module-load cycle (ingest.py does not import
    # repair.py, so this only documents that the dependency runs one way).
    from app.brain.ingest import IngestPayload, ingest_source

    if source.type == "url" and source.url:
        # PRE-FLIGHT THE FETCH BEFORE DELETING ANYTHING. This repair runs off
        # a GET (merely opening the source in the Reader), and it used to
        # delete the chunks FIRST and find out the URL was dead second —
        # `extract_text` never raises, a 404'd page yields [], and the
        # source rolled up "empty" with the only remaining copy of its text
        # gone. The population this repair targets is pre-Plan-9 rows that
        # can be YEARS old; their URLs being dead is the expected case, not
        # the edge. A dead URL now falls through to the chunk-reassembly
        # path below — the text we still have beats the text we might fetch.
        from app.brain.extract import extract_text

        try:
            sections = extract_text("url", url=source.url)
        except Exception:
            sections = []
        if sections and any((s.text or "").strip() for s in sections):
            log.info(
                "repair_pageless_source: source_id=%s is type=url and its "
                "stored url still serves content — re-fetching it directly",
                source.id,
            )
            _delete_orphaned_chunks(db, source.id)
            ingest_source(db, source.id, IngestPayload(kind="url", url=source.url))
            return True
        log.warning(
            "repair_pageless_source: source_id=%s stored url yields nothing "
            "(dead link?) — preserving the existing chunks and reassembling "
            "them instead",
            source.id,
        )

    chunks = (
        db.query(Chunk)
        .filter_by(source_id=source.id)
        .order_by(Chunk.id)
        .all()
    )
    if not chunks:
        return False

    ordered = order_chunk_texts([c.text for c in chunks])
    text = reassemble_chunk_texts(ordered)

    log.info(
        "repair_pageless_source: reassembled %d chunks (%d chars) for "
        "source_id=%s; re-ingesting as kind=text",
        len(chunks), len(text), source.id,
    )
    _delete_orphaned_chunks(db, source.id)
    ingest_source(db, source.id, IngestPayload(kind="text", text=text))
    return True
