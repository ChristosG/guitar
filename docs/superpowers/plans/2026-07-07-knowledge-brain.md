# Knowledge Brain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ingest heterogeneous sources (PDF, pasted text, URL, transcript) → structure-aware chunks → Qwen embeddings in pgvector, and serve cross-lingual retrieval + a grounded-answer endpoint, plus a Knowledge cockpit page — so the tutor's own material becomes searchable and answerable.

**Architecture:** A `app/brain/` package (extract → chunk → embed → store; and retrieve) built on the Foundations `LLMProvider` seam and the `KnowledgeSource`/`Chunk` models. Ingestion runs synchronously in the API for the PoC (a source is small); retrieval is pgvector cosine top-k with the asymmetric query prefix. A Next.js `/[locale]/knowledge` page drives it.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, pgvector (HNSW cosine index), PyMuPDF (`pymupdf`), `trafilatura` (URL/article extraction), the existing `QwenVLLM` provider. Next.js/React on the web side.

## Global Constraints (from spec + Foundations — verbatim)

- **Embeddings:** 2560-dim; provider already L2-normalizes, sorts by `index`, batches 16, and applies the query prefix when `is_query=True`. Documents embedded with `is_query=False`; queries with `is_query=True`. Never re-implement embedding — call `get_provider().embed(...)`.
- **Cross-lingual:** store each source's `language`; embed as-is; retrieval works across languages; a grounded answer is generated in the **requested** locale regardless of source language.
- **Foundations interfaces to consume (do not redefine):**
  - `from app.llm.factory import get_provider` → `.embed(texts: list[str], *, is_query: bool=False) -> list[list[float]]`, `.chat(messages: list[dict], *, temperature=0.3, enable_thinking=False) -> str`, `.health() -> dict`.
  - `from app.db import Base, engine, SessionLocal, get_db`.
  - `from app.models.knowledge import KnowledgeSource, Chunk` — `KnowledgeSource(id,type,title,status,language,created_at,…)`, `Chunk(id,source_id,text,section_path,page,embedding)`.
  - Routers mounted in `app/main.py` via `app.include_router(...)`; settings in `app.config.settings`.
- **DB access in tests/alembic:** host runs use `DATABASE_URL=postgresql+psycopg://guitar:guitar@localhost:5434/guitar`; live-model tests use `LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1`. Use `apps/api/.venv/bin/{pytest,alembic}`.
- **Bilingual UI:** GR/EN via next-intl; add message keys to both `en.json` and `el.json`.
- **Drive the real model** for any embed/answer verification (skips are failures when servers are up).

> Implementers: verify current library APIs (`pymupdf`, `trafilatura`, pgvector SQLAlchemy operators) via the `context7` MCP tool if unsure.

## File Structure

```
apps/api/app/
  brain/
    __init__.py
    extract.py     # extract_text(kind, *, data=None, url=None, path=None) -> list[Section]
    chunk.py       # chunk_sections(sections, ...) -> list[ChunkDraft]
    ingest.py      # ingest_source(db, source_id) -> None   (extract→chunk→embed→store, status)
    retrieve.py    # search(db, query, *, k, domain?, language?) -> list[Hit]; answer(db, query, locale) -> Answer
  routers/
    knowledge.py   # /sources CRUD+ingest, /search, /ask
  schemas/
    knowledge.py   # Pydantic request/response models
  models/knowledge.py  # (extend: domain tag, error, char_count)
apps/web/src/
  app/[locale]/knowledge/page.tsx
  components/knowledge/{source-list,add-source,search-box}.tsx
  lib/api.ts       # typed fetch to NEXT_PUBLIC_API_BASE
```

---

## Task 1: Schema extensions + HNSW index

**Files:** Modify `apps/api/app/models/knowledge.py`; new Alembic migration.

**Interfaces produced:** `KnowledgeSource.domain` (str, nullable), `.error` (str, nullable), `.char_count` (int, nullable); an HNSW cosine index on `chunk.embedding`.

- [ ] **Step 1 (failing test):** in `apps/api/tests/test_brain_schema.py`, assert a `KnowledgeSource` accepts `domain="tone"` and that `Chunk` supports a cosine-distance ordered query:
```python
import pytest
from sqlalchemy import select
from app.db import SessionLocal, engine, Base, text as _  # noqa
from app.models.knowledge import KnowledgeSource, Chunk
try:
    with engine.connect() as c: c.execute(__import__("sqlalchemy").text("SELECT 1"))
except Exception:
    pytest.skip("no db", allow_module_level=True)

def test_domain_and_cosine_order():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        s = KnowledgeSource(type="text", title="t", status="ready", language="en", domain="tone")
        db.add(s); db.flush()
        for i in range(3):
            db.add(Chunk(source_id=s.id, text=f"c{i}", embedding=[float(i)]*2560))
        db.commit()
        q = [0.0]*2560; q[0] = 2.9
        rows = db.scalars(select(Chunk).order_by(Chunk.embedding.cosine_distance(q)).limit(1)).all()
        assert rows and rows[0].text == "c1"  # nearest to ~[2.9,0,...] among c0/c1/c2 rows
    finally:
        db.close()
```
- [ ] **Step 2:** run `DATABASE_URL=…localhost:5434… .venv/bin/pytest tests/test_brain_schema.py -v` → FAIL (`domain` unknown / no cosine support import).
- [ ] **Step 3:** add columns to `KnowledgeSource` in `models/knowledge.py`:
```python
    domain: Mapped[str | None] = mapped_column(String(30), nullable=True)   # tone/beginner/theory
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
```
(ensure `Integer`, `Text` imported.) `Chunk.embedding.cosine_distance(...)` is provided by pgvector's `Vector` type — no model change needed.
- [ ] **Step 4:** `alembic revision --autogenerate -m "brain: source domain/error/char_count + chunk hnsw index"`; in the migration `upgrade()` append after the `add_column`s:
```python
    op.create_index("ix_chunk_embedding_hnsw", "chunk", ["embedding"],
                    postgresql_using="hnsw",
                    postgresql_with={"m": 16, "ef_construction": 64},
                    postgresql_ops={"embedding": "vector_cosine_ops"})
```
and drop it in `downgrade()`.
- [ ] **Step 5:** `DATABASE_URL=…localhost:5434… .venv/bin/alembic upgrade head`; rerun the test → PASS.
- [ ] **Step 6:** commit `git add apps/api/app/models/knowledge.py apps/api/alembic/versions apps/api/tests/test_brain_schema.py && git commit -m "feat(brain): source metadata + hnsw cosine index"`.

---

## Task 2: Text extraction (`extract.py`)

**Files:** Create `apps/api/app/brain/{__init__,extract}.py`, `apps/api/tests/test_extract.py`. Add `pymupdf>=1.24` and `trafilatura>=1.8` to `pyproject.toml` deps.

**Interfaces produced:**
```python
from dataclasses import dataclass
@dataclass
class Section:
    heading: str | None      # nearest heading/section path, or None
    text: str
    page: int | None

def extract_text(kind: str, *, data: bytes | None = None, url: str | None = None,
                 text: str | None = None) -> list[Section]: ...
# kind ∈ {"pdf","url","text"}. Returns ordered Sections. Never raises on empty; returns [].
```
- [ ] **Step 1 (failing test):** `test_extract.py` — plain text returns one Section; a tiny inline PDF (build with pymupdf in the test) yields Sections whose joined text contains the inserted string; URL extraction is marked `@pytest.mark.integration` (skips offline). Include a test that extracts the **real book** when present:
```python
import os, pytest
from app.brain.extract import extract_text
BOOK = "/mnt/nvme2TB/guitar_tutor/Getting Great Guitar Sounds.pdf"

def test_text_passthrough():
    secs = extract_text("text", text="Hello tone")
    assert len(secs) == 1 and "Hello tone" in secs[0].text

@pytest.mark.skipif(not os.path.exists(BOOK), reason="book not present")
def test_pdf_book_extracts_prose():
    with open(BOOK, "rb") as f: secs = extract_text("pdf", data=f.read())
    joined = " ".join(s.text for s in secs).lower()
    assert "pickup" in joined and len(joined) > 5000   # real gear prose came through
```
- [ ] **Step 2:** run → FAIL (no module).
- [ ] **Step 3:** implement `extract.py`: `text` → `[Section(None, text, None)]`; `pdf` → open with `fitz` (pymupdf) from bytes, per page extract text, detect headings by font-size heuristic or `page.get_text("dict")` spans (fallback: page-as-section with `page=n`); `url` → `trafilatura.fetch_url`+`extract` (fallback `httpx.get` + strip tags). Guard empties.
- [ ] **Step 4:** `.venv/bin/pip install -e ".[dev]"` then run `pytest tests/test_extract.py -v` (non-integration) → PASS incl. the book test.
- [ ] **Step 5:** commit.

---

## Task 3: Chunking (`chunk.py`)

**Files:** Create `apps/api/app/brain/chunk.py`, `apps/api/tests/test_chunk.py`.

**Interfaces produced:**
```python
@dataclass
class ChunkDraft:
    text: str
    section_path: str | None
    page: int | None

def chunk_sections(sections: list["Section"], *, target_chars: int = 1200,
                   overlap_chars: int = 150) -> list[ChunkDraft]: ...
# Structure-aware: never merge across a heading boundary; within a section, sliding
# window of ~target_chars with overlap; carries section_path + page through.
```
- [ ] **Step 1 (failing test):** assert (a) a 5000-char single section → multiple drafts each ≤ ~target+overlap, consecutive drafts overlap; (b) two sections with different headings never share a chunk (boundaries respected); (c) `section_path`/`page` preserved. Provide concrete asserts.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement: iterate sections; for each, window its text by sentences/whitespace up to `target_chars` with `overlap_chars` carryover; emit `ChunkDraft`s tagged with the section heading as `section_path`. No cross-section merge.
- [ ] **Step 4:** run → PASS.
- [ ] **Step 5:** commit.

---

## Task 4: Ingestion pipeline (`ingest.py`)

**Files:** Create `apps/api/app/brain/ingest.py`, `apps/api/tests/test_ingest.py`.

**Interfaces produced:**
```python
def ingest_source(db, source_id) -> None:
    # 1) load KnowledgeSource; set status="ingesting"
    # 2) extract_text(source.type, ...) using stored raw (see below) → sections
    # 3) chunk_sections(sections) → drafts
    # 4) embed draft texts via get_provider().embed(texts, is_query=False) (batch handled in provider)
    # 5) persist Chunk rows (text, section_path, page, embedding); set char_count
    # 6) status="ready"; on any exception status="failed", error=str(e)  (never leave "ingesting")
```
Raw payload handling: store the source's raw input at create time. For the PoC keep it simple — pass the extracted `sections` in, OR store `raw_text`/`url`/`pdf bytes` on a transient field. **Decision:** `ingest_source` re-derives from a `raw` argument the router passes through a module-level in-memory handoff is NOT allowed; instead the router calls `ingest_source(db, source_id, payload=IngestPayload(...))`. Update the signature to:
```python
@dataclass
class IngestPayload:
    kind: str; text: str | None = None; url: str | None = None; data: bytes | None = None
def ingest_source(db, source_id, payload: "IngestPayload") -> None: ...
```
- [ ] **Step 1 (failing, live-embed integration test):** create a `KnowledgeSource(type="text")`, call `ingest_source` with a payload of ~3 short paragraphs, assert: status→"ready", ≥1 Chunk persisted, each `len(embedding)==2560`, `char_count>0`. Mark `@pytest.mark.integration` (needs embed server). Also a failure-path test: a payload that extracts empty → status stays "ready" with 0 chunks OR "failed" with error (pick "ready, 0 chunks" for empty; "failed" only on exception) — assert your chosen contract.
- [ ] **Step 2:** run with `EMBED_BASE_URL=…8090… DATABASE_URL=…5434…` → FAIL.
- [ ] **Step 3:** implement per the contract; commit embeddings via pgvector column (pass Python lists).
- [ ] **Step 4:** run → PASS (real embeddings stored; verify count==2560).
- [ ] **Step 5:** commit.

---

## Task 5: Retrieval + API (`retrieve.py`, `routers/knowledge.py`, `schemas/knowledge.py`)

**Files:** Create those three; modify `app/main.py` to include the router.

**Interfaces produced:**
```python
@dataclass
class Hit: chunk_id; source_id; source_title; text; section_path; page; score  # score = 1 - cosine_distance
def search(db, query: str, *, k: int = 8, domain: str | None = None,
          language: str | None = None) -> list[Hit]:
    # qv = get_provider().embed([query], is_query=True)[0]
    # SELECT ... ORDER BY chunk.embedding <=> qv LIMIT k, joined to source, optional filters
@dataclass
class Answer: text: str; citations: list[Hit]
def answer(db, query: str, *, locale: str, k: int = 8) -> Answer:
    # hits = search(...); build a grounded prompt: system="answer ONLY from context, in {locale}, cite [n]";
    # user=query + numbered context; text = get_provider().chat(msgs); return Answer(text, hits)
```
Routes in `routers/knowledge.py` (`prefix="/knowledge"`):
- `POST /sources` (body: kind, title, domain?, language?, text?|url?; for PDF a separate `POST /sources/upload` multipart) → creates source, runs `ingest_source` synchronously, returns source with status.
- `GET /sources` → list (id,title,type,status,domain,language,char_count,created_at).
- `GET /sources/{id}` → detail + first N chunks (preview).
- `DELETE /sources/{id}` → cascN delete.
- `POST /search` (query,k?,domain?,language?) → hits.
- `POST /ask` (query, locale) → Answer.

- [ ] **Step 1 (failing integration test):** seed 2 sources (one EN "a humbucker cancels 60-cycle hum", one about delay), ingest both, then: `search("what removes hum?")` returns the humbucker chunk first; `answer("what is a humbucker?", locale="el")` returns Greek text that is non-empty and has ≥1 citation. Assert. `@pytest.mark.integration`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement retrieve + schemas + router; wire `app.include_router(knowledge.router)` in `main.py`. Use SQLAlchemy `Chunk.embedding.cosine_distance(qv)` for ordering; `score = 1 - distance`.
- [ ] **Step 4:** run integration → PASS (Greek answer printed in report). Also add a fast unit test for the grounded-prompt builder (pure function) asserting the locale instruction + numbered context appear.
- [ ] **Step 5:** commit.

---

## Task 6: Knowledge cockpit page + end-to-end

**Files:** `apps/web/src/lib/api.ts`; `apps/web/src/app/[locale]/knowledge/page.tsx`; `apps/web/src/components/knowledge/*`; message keys in `en.json`/`el.json`; `apps/web/tests/knowledge.spec.ts`.

**Interfaces produced:** a `/el/knowledge` + `/en/knowledge` page: **Add source** (paste text or URL; PDF upload), a **source list** with status badges, a **search box** returning ranked snippets, and an **Ask** box returning a grounded answer with citations. Uses `NEXT_PUBLIC_API_BASE` (wired in Foundations).

- [ ] **Step 1 (failing Playwright, port 3100):** navigate `/en/knowledge`; add a text source ("A humbucker cancels hum"); assert it appears in the list and reaches `ready`; type "hum" in search → a result snippet appears. (Mock the API with Playwright route interception so the web test is deterministic and offline — assert the UI wiring, not the model.) 
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** implement `lib/api.ts` (typed fetch), the page + components, message keys (GR/EN). Keep components small/focused.
- [ ] **Step 4:** run Playwright → PASS.
- [ ] **Step 5 (real end-to-end, manual-in-report):** with the stack up, `curl -F` or JSON-POST the **real book** to `POST /knowledge/sources/upload`, wait for `ready`, then `POST /knowledge/ask {"query":"What makes a great guitar tone according to this book?","locale":"el"}` and paste the Greek grounded answer + citations into the report. Also EN.
- [ ] **Step 6:** commit.

---

## Self-Review

Spec coverage: ingest (pdf/url/text/transcript-as-text) ✓T2/T4 · structure-aware chunking + fallback ✓T3 · embed via provider (no re-impl) ✓T4 · pgvector cosine + HNSW ✓T1/T5 · cross-lingual answer-in-locale ✓T5 · Sources UI + search + ask ✓T6 · real-book e2e ✓T6. Deferred (named): full agent/tools/HITL chat → Agent plan (this plan's `/ask` is a single-shot grounded answer, no tools); domain auto-tagging heuristics → later. Placeholder scan: none — every task has concrete asserts + code shape. Type consistency: `Section`(extract)→`chunk_sections`→`ChunkDraft`→`ingest_source(payload)`→`Chunk`; `Hit`/`Answer` shared by retrieve+router; `get_provider().embed(is_query=)` used exactly per Foundations.
