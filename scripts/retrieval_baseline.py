"""Gate 0.2 — capture the retrieval baseline of the CURRENT (Qwen3-Embedding-4B,
2560-dim) index against the REAL deployed library, before the embedding swap
replaces every vector.

This measurement is available exactly once. Plan 13 replaces `chunk.embedding`
with a 384/1024-dim local-CPU model; once that migration runs, there is no way
to reconstruct what retrieval used to do, and therefore no way to answer the
only question that matters afterwards: *did we make it worse?*

The query set is not invented — it is the one `app.curriculum.ground`'s own
docstring calibrated its floors against, plus the axes that docstring never
tested:

  - topical_en / topical_el : the queries `ground.py` used, and their Greek
    equivalents (the tutor's default locale is `el`, his library is an ENGLISH
    book — cross-lingual retrieval is load-bearing and was never measured).
  - gear             : exact model names. The current index is vector-only, so
    it has NO lexical channel; these are expected to do badly and are the
    justification for the BM25 arm.
  - uncovered        : topics the library genuinely does not cover. These MUST
    keep returning nothing above the floor after the swap, or the curriculum
    gap-detection (G3) silently reports every module as grounded.
  - adversarial      : a query built from the junk sources' own vocabulary.

`JUNK_SOURCE_IDS` are the three sources `ground.py` names as known junk, read
back from the live DB by id so a re-seed can't silently invalidate them.

Run (host-side; the vLLM embed server's container hostname is not resolvable
from here, hence the explicit EMBED_BASE_URL):

    docker compose exec -T api python - < scripts/retrieval_baseline.py
"""
import json
from dataclasses import asdict

from sqlalchemy import select

from app.brain.retrieve import search
from app.db import SessionLocal
from app.models.knowledge import Chunk, KnowledgeSource

TOP_K = 10

# The three sources app/curriculum/ground.py's docstring identifies as junk.
# Keyed by a stable substring of the URL rather than a uuid so this survives a
# re-seed of the library.
JUNK_URL_FRAGMENTS = [
    "guitar-effects-survival-guide-introduction/v13776",  # 86-char page title
    "kings-of-tone/c176",                                 # {{video.title}} template
    "online.berklee.edu/courses/getting-your-guitar-sound",  # enrollment boilerplate
]

QUERIES: list[tuple[str, str]] = [
    # (axis, query) — the four `ground.py` calibrated against:
    ("topical_en", "pickup types and how they shape guitar tone"),
    ("topical_en", "amplifier gain staging"),
    ("topical_en", "overdrive and distortion pedals"),
    ("topical_en", "how pick thickness affects tone"),
    # Greek — the tutor's DEFAULT locale, never measured before:
    ("topical_el", "τύποι μαγνητών και πώς επηρεάζουν τον ήχο της κιθάρας"),
    ("topical_el", "ενίσχυση και gain staging στον ενισχυτή"),
    ("topical_el", "υπερφόρτωση και πετάλια παραμόρφωσης"),
    # Exact gear names — vector-only has no lexical channel for these:
    ("gear", "Tube Screamer"),
    ("gear", "TS-808"),
    ("gear", "5150"),
    ("gear", "Stratocaster single coil"),
    # Genuinely uncovered — MUST stay empty above the floor (gap detection):
    ("uncovered", "vibrato and legato technique"),
    ("uncovered", "reading guitar tablature notation"),
    ("uncovered", "fingerstyle arrangement of classical pieces"),
    # Built from the junk sources' own vocabulary:
    ("adversarial", "kings of tone course video download"),
]

# ground.py's current floors, applied here only to REPORT what survives them —
# this script changes nothing.
SCORE_FLOOR = 0.60
LEN_FLOOR = 200


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

        total_chunks = db.scalar(select(Chunk.id).limit(1)) is not None
        print(f"junk sources resolved: {len(junk_source_ids)}/{len(JUNK_URL_FRAGMENTS)}")
        print(f"corpus non-empty: {total_chunks}\n")

        out: dict = {
            "embedder": "qwen3-emb-4b (2560-dim)",
            "floors": {"score": SCORE_FLOOR, "len": LEN_FLOOR},
            "junk_sources": junk_source_ids,
            "queries": [],
        }

        for axis, q in QUERIES:
            hits = search(db, q, k=TOP_K)
            rows = []
            for rank, h in enumerate(hits, start=1):
                d = asdict(h)
                text = d.get("text") or ""
                sid = str(d.get("source_id"))
                rows.append(
                    {
                        "rank": rank,
                        "score": round(float(d["score"]), 4),
                        "len": len(text),
                        "source_id": sid,
                        "source_title": d.get("source_title"),
                        "page": d.get("page_no"),
                        "is_junk_source": sid in junk_source_ids,
                        "passes_floor": float(d["score"]) >= SCORE_FLOOR
                        and len(text) >= LEN_FLOOR,
                        "preview": " ".join(text.split())[:110],
                    }
                )

            kept = [r for r in rows if r["passes_floor"]]
            junk_kept = [r for r in kept if r["is_junk_source"]]
            out["queries"].append(
                {"axis": axis, "query": q, "hits": rows,
                 "n_pass_floor": len(kept), "n_junk_past_floor": len(junk_kept)}
            )

            top = rows[0] if rows else None
            print(f"[{axis:11}] {q[:52]:<52} "
                  f"top={top['score'] if top else 0:.3f} "
                  f"kept={len(kept):>2}/{len(rows)} "
                  f"junk_past_floor={len(junk_kept)}")
            if top:
                print(f"{'':14} -> {top['source_title'][:70]}")

        with open("/tmp/baseline.json", "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("\nwrote /tmp/baseline.json")

        # The three assertions Gate 4.5 must reproduce on the NEW index.
        print("\n=== BASELINE ASSERTIONS (what the new index must match or beat) ===")
        junk_leaks = sum(q["n_junk_past_floor"] for q in out["queries"])
        print(f"A1 junk passages past the floor, all queries : {junk_leaks}  (target: 0)")
        unc = [q for q in out["queries"] if q["axis"] == "uncovered"]
        print(f"A2 uncovered topics returning >0 past floor  : "
              f"{sum(1 for q in unc if q['n_pass_floor'] > 0)}/{len(unc)}  (target: 0)")
        gear = [q for q in out["queries"] if q["axis"] == "gear"]
        gear_ok = sum(1 for q in gear if q["n_pass_floor"] > 0)
        print(f"A3 gear queries retrieving anything          : {gear_ok}/{len(gear)}"
              f"  (this is the arm BM25 must fix)")
        el = [q for q in out["queries"] if q["axis"] == "topical_el"]
        el_ok = sum(1 for q in el if q["n_pass_floor"] > 0)
        print(f"A4 GREEK topical queries retrieving anything : {el_ok}/{len(el)}"
              f"  (cross-lingual — never measured before)")
    finally:
        db.close()


main()
