"""The retrieval harness — originally Gate 0.2's one-shot baseline of the OLD
(Qwen3-Embedding-4B, 2560-dim, vector-only) index; now the GATE that Plan 13
Stage 4's hybrid index has to clear.

Same query set, same axes, same three assertions. What changed underneath is the
entire retrieval stack (e5-small 384-dim + Okapi BM25, fused with RRF, one floor
inside `search()`), and the point of keeping this file comparable is that "did we
make it worse?" stays an answerable question rather than a vibe.

The query set is not invented — it is the one `app.curriculum.ground`'s own
docstring calibrated its old floors against, plus the axes that docstring never
tested:

  - topical_en / topical_el : the queries the old `ground.py` used, and their
    Greek equivalents (the tutor's default locale is `el`, his library is an
    ENGLISH book — cross-lingual retrieval is load-bearing and went unmeasured
    until Stage 4).
  - gear             : exact model names. The old index was vector-only, so it had
    NO lexical channel; these were expected to do badly and are the entire
    justification for the BM25 arm.
  - uncovered        : topics the library genuinely does not cover. These MUST
    keep returning nothing, or curriculum gap-detection (G3) silently reports
    every module as grounded.
  - adversarial      : a query built from the junk sources' own vocabulary.

THE THREE ASSERTIONS (Gate 4.6). All must pass:

  A1  no known-junk passage survives the floor, on any query.
  A2  every "uncovered" topic returns ZERO. Gap detection survives the swap.
  A3  every "gear" query retrieves a chunk that ACTUALLY CONTAINS the term —
      not merely something that sounds like gear talk. This is what BM25 is for.

Run (host-side, or in the container — the local embedder needs no network):

    docker compose exec -T api python - < scripts/retrieval_baseline.py
"""
import json
from dataclasses import asdict

from sqlalchemy import select

from app.brain.lexical import get_index
from app.brain.retrieve import (
    MIN_PASSAGE_CHARS,
    STRONG_COSINE,
    WEAK_COSINE_FLOOR,
    search,
)
from app.db import SessionLocal
from app.models.knowledge import Chunk, KnowledgeSource

TOP_K = 10

# The three sources app/curriculum/ground.py's docstring identified as junk.
# Keyed by a stable substring of the URL rather than a uuid, so a re-seed cannot
# silently invalidate them.
JUNK_URL_FRAGMENTS = [
    "guitar-effects-survival-guide-introduction/v13776",  # 86-char page title
    "kings-of-tone/c176",                                 # {{video.title}} template
    "online.berklee.edu/courses/getting-your-guitar-sound",  # enrollment boilerplate
]

QUERIES: list[tuple[str, str]] = [
    ("topical_en", "pickup types and how they shape guitar tone"),
    ("topical_en", "amplifier gain staging"),
    ("topical_en", "overdrive and distortion pedals"),
    ("topical_en", "how pick thickness affects tone"),
    ("topical_el", "τύποι μαγνητών και πώς επηρεάζουν τον ήχο της κιθάρας"),
    ("topical_el", "ενίσχυση και gain staging στον ενισχυτή"),
    ("topical_el", "υπερφόρτωση και πετάλια παραμόρφωσης"),
    ("gear", "Tube Screamer"),
    ("gear", "TS-808"),
    ("gear", "5150"),
    ("gear", "Stratocaster single coil"),
    ("uncovered", "vibrato and legato technique"),
    ("uncovered", "reading guitar tablature notation"),
    ("uncovered", "fingerstyle arrangement of classical pieces"),
    ("adversarial", "kings of tone course video download"),
]

# A3: the literal strings a retrieved chunk must CONTAIN for a gear query to count
# as answered. Spelling variants are listed on purpose — `lexical.tokenize` is
# built to make `TS-808`/`TS808`/`TS 808` one thing, and this is where that claim
# gets checked against the tutor's real book.
GEAR_TERMS: dict[str, list[str]] = {
    "Tube Screamer": ["tube screamer"],
    "TS-808": ["ts-808", "ts808", "ts 808"],
    "5150": ["5150"],
    "Stratocaster single coil": ["stratocaster"],
}


def main() -> None:
    db = SessionLocal()
    try:
        junk_source_ids: dict[str, str] = {}
        for frag in JUNK_URL_FRAGMENTS:
            src = db.scalars(
                select(KnowledgeSource).where(KnowledgeSource.title.contains(frag))
            ).first()
            if src is None:
                print(f"  !! junk source not found for fragment: {frag}")
                continue
            junk_source_ids[str(src.id)] = src.title

        non_empty = db.scalar(select(Chunk.id).limit(1)) is not None
        index = get_index(db)
        print(f"junk sources resolved: {len(junk_source_ids)}/{len(JUNK_URL_FRAGMENTS)}")
        print(f"corpus: {index.n_docs} chunks, {len(index.df)} lexical terms, non_empty={non_empty}")
        print(
            f"floor: len>={MIN_PASSAGE_CHARS} AND cos>={WEAK_COSINE_FLOOR} AND "
            f"(cos>={STRONG_COSINE} OR complete lexical coverage)\n"
        )

        out: dict = {
            "retriever": "multilingual-e5-small (384) + Okapi BM25, RRF(60)",
            "floor": {
                "min_chars": MIN_PASSAGE_CHARS,
                "weak_cosine": WEAK_COSINE_FLOOR,
                "strong_cosine": STRONG_COSINE,
            },
            "junk_sources": junk_source_ids,
            "queries": [],
        }

        print(f"{'axis':<12} {'query':<46} {'top_cos':>7} {'kept':>5} {'junk':>5} {'term?':>6}")
        print("-" * 88)

        for axis, q in QUERIES:
            kept = search(db, q, k=TOP_K)                       # the floor ON
            raw = search(db, q, k=TOP_K, apply_floor=False)     # what the floor saw

            rows = [
                {
                    "rank": rank,
                    "rrf": round(float(asdict(h)["score"]), 4),
                    "cos": round(h.vector_score, 4),
                    "bm25": round(h.lexical_score, 2),
                    "len": len(h.text or ""),
                    "source_id": str(h.source_id),
                    "source_title": h.source_title,
                    "page": h.page,
                    "is_junk_source": str(h.source_id) in junk_source_ids,
                    "preview": " ".join((h.text or "").split())[:110],
                }
                for rank, h in enumerate(kept, start=1)
            ]

            junk_kept = [r for r in rows if r["is_junk_source"]]
            term_ok = None
            if axis == "gear":
                needles = GEAR_TERMS[q]
                term_ok = any(
                    any(n in (h.text or "").lower() for n in needles) for h in kept
                )

            out["queries"].append({
                "axis": axis, "query": q, "hits": rows,
                "n_raw": len(raw), "n_kept": len(kept),
                "n_junk_kept": len(junk_kept),
                "contains_term": term_ok,
            })

            top_cos = max((h.vector_score for h in raw), default=0.0)
            flag = "-" if term_ok is None else ("YES" if term_ok else "NO")
            print(
                f"{axis:<12} {q[:46]:<46} {top_cos:7.3f} {len(kept):5d} "
                f"{len(junk_kept):5d} {flag:>6}"
            )

        with open("/tmp/baseline_e5.json", "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\nwrote /tmp/baseline_e5.json")

        print("\n=== GATE 4.6 — three assertions, all must pass ===")
        junk_leaks = sum(q["n_junk_kept"] for q in out["queries"])
        a1 = junk_leaks == 0
        print(f"A1 known-junk passages past the floor      : {junk_leaks:>3}     (target 0)  {'PASS' if a1 else 'FAIL'}")

        unc = [q for q in out["queries"] if q["axis"] == "uncovered"]
        unc_leaks = sum(1 for q in unc if q["n_kept"] > 0)
        a2 = unc_leaks == 0
        print(f"A2 uncovered topics returning anything     : {unc_leaks}/{len(unc)}     (target 0)  {'PASS' if a2 else 'FAIL'}")

        gear = [q for q in out["queries"] if q["axis"] == "gear"]
        gear_ok = sum(1 for q in gear if q["contains_term"])
        a3 = gear_ok == len(gear)
        print(f"A3 gear queries retrieving the ACTUAL term : {gear_ok}/{len(gear)}     (target {len(gear)}) {'PASS' if a3 else 'FAIL'}")

        el = [q for q in out["queries"] if q["axis"] == "topical_el"]
        el_ok = sum(1 for q in el if q["n_kept"] > 0)
        print(f"   GREEK topical queries retrieving anything: {el_ok}/{len(el)}     (cross-lingual)")

        print("\n" + ("ALL ASSERTIONS PASS" if (a1 and a2 and a3) else "*** GATE FAILED ***"))
    finally:
        db.close()


main()
