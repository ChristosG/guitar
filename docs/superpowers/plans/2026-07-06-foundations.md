# Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the monorepo, Dockerized Postgres+pgvector, a FastAPI skeleton, the `LLMProvider` seam wired to the local Qwen vLLM, the core database models + migrations, and a themed bilingual Next.js shell — so every later engine has a working, tested substrate to build on.

**Architecture:** A two-app monorepo (`apps/api` FastAPI + Python, `apps/web` Next.js + TS) fronted later by host nginx. Postgres (with the pgvector extension) is the single store for relational data **and** embeddings. The `api` container joins the existing external `platform-net` to reach the Qwen LLM (`qwen-vllm:6888`) and embedding (`qwen-emb-vllm:8090`) servers by alias. All model access goes through one `LLMProvider` interface so Qwen↔Claude is a config flip.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, pydantic-settings, SQLAlchemy 2.0, Alembic, pgvector, openai SDK (OpenAI-compatible client for vLLM), httpx, pytest. Next.js 15 (App Router, `output: "standalone"`), TypeScript, Tailwind, shadcn/ui, next-intl. Docker Compose.

## Global Constraints

*(Copied verbatim from the spec. Every task implicitly includes these.)*

- **Models (dev default = local Qwen):** `LLM_MODEL=/models/Qwen3.5-9B`, `LLM_BASE_URL=http://qwen-vllm:6888/v1` (in-docker) / `http://localhost:6888/v1` (host); `EMBED_MODEL=qwen3-emb-4b`, `EMBED_BASE_URL=http://qwen-emb-vllm:8090/v1` (in-docker) / `http://localhost:8090/v1` (host); API key is any non-empty string (`"none"`).
- **Embeddings:** 2560-dim; **L2-normalize** every returned vector; **asymmetric retrieval** — queries get the prefix `Instruct: Given a question, retrieve passages that answer it\nQuery: {q}`; documents embedded raw; batch documents at 16/req.
- **LLM call convention:** pass `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` by default (thinking on only for planning steps, later).
- **DB:** Postgres + pgvector — one store for relational + vectors + (later) LangGraph checkpoints.
- **Provider seam:** all model access via one `LLMProvider` interface with `QwenVLLM` and (later) `Claude` impls; provider-neutral.
- **Networking:** app containers join the **external** docker network `platform-net`; reach models by alias. Loopback publish ports (provisional): web `127.0.0.1:8790`, api `127.0.0.1:8791`.
- **Bilingual:** GR + EN, switchable; every content entity carries a `language`.
- **No auth** for the PoC.
- **Next.js:** `output: "standalone"`; middleware sets `Cache-Control: no-store` on HTML; `/_next/static/**` stays immutable.
- **Verification rule:** unit tests never substitute for driving the real model — provider work has a live integration smoke against `:6888`/`:8090`.

> **Note for implementers:** where a scaffolding CLI (create-next-app, shadcn, alembic init) or a library API may have shifted since this plan was written, verify the exact current invocation via the `context7` MCP tool before running it. The commands below are the intended shape.

---

## File Structure

```
guitar_tutor/
├── docker-compose.yml            # postgres(pgvector) + api + web + worker(stub)
├── .env.example                  # all config keys, safe placeholders
├── apps/
│   ├── api/                      # FastAPI + Python
│   │   ├── pyproject.toml
│   │   ├── Dockerfile
│   │   ├── alembic.ini
│   │   ├── app/
│   │   │   ├── __init__.py
│   │   │   ├── main.py           # FastAPI app + router mount
│   │   │   ├── config.py         # pydantic-settings Settings
│   │   │   ├── db.py             # engine, SessionLocal, Base, get_db
│   │   │   ├── models/
│   │   │   │   ├── __init__.py   # re-exports for Alembic autogenerate
│   │   │   │   ├── base.py       # TimestampMixin, UUID pk mixin
│   │   │   │   ├── student.py    # Student
│   │   │   │   ├── block.py      # Block (recursive curriculum tree)
│   │   │   │   └── knowledge.py  # KnowledgeSource, Chunk(Vector(2560))
│   │   │   ├── llm/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base.py       # LLMProvider ABC + dataclasses
│   │   │   │   ├── qwen.py       # QwenVLLM impl
│   │   │   │   ├── embeddings.py # l2_normalize, query_instruct helpers
│   │   │   │   └── factory.py    # get_provider() from settings
│   │   │   └── routers/
│   │   │       └── health.py     # /health/live, /health/ready
│   │   ├── alembic/
│   │   │   ├── env.py
│   │   │   └── versions/
│   │   └── tests/
│   │       ├── conftest.py
│   │       ├── test_health.py
│   │       ├── test_embeddings_helpers.py
│   │       ├── test_llm_qwen_integration.py   # live smoke, marked
│   │       └── test_models_roundtrip.py
│   └── web/                      # Next.js (scaffolded, then edited)
│       ├── package.json
│       ├── next.config.ts        # output:"standalone"
│       ├── Dockerfile
│       ├── src/
│       │   ├── middleware.ts      # next-intl + Cache-Control:no-store
│       │   ├── i18n/
│       │   │   ├── routing.ts     # locales ['en','el']
│       │   │   └── request.ts
│       │   ├── app/[locale]/
│       │   │   ├── layout.tsx     # ThemeProvider + NextIntlClientProvider
│       │   │   └── page.tsx       # shell landing (health badge)
│       │   ├── components/
│       │   │   ├── theme-provider.tsx
│       │   │   └── theme-toggle.tsx
│       │   └── messages/
│       │       ├── en.json
│       │       └── el.json
└── docs/superpowers/…            # (already present)
```

---

## Task 1: Monorepo skeleton + Docker Compose + Postgres(pgvector)

**Files:**
- Create: `docker-compose.yml`, `.env.example`, `apps/api/`, `apps/web/` (dirs)
- Test: `apps/api/tests/test_pgvector_up.sh` (shell smoke)

**Interfaces:**
- Produces: a running `postgres` service on the compose network with database `guitar`, user `guitar`, and the `vector` extension available; `DATABASE_URL=postgresql+psycopg://guitar:guitar@postgres:5432/guitar` (in-docker).

- [ ] **Step 1: Write the failing smoke test**

Create `apps/api/tests/test_pgvector_up.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
# Asserts postgres is up and the pgvector extension can be created.
docker compose exec -T postgres psql -U guitar -d guitar -c "CREATE EXTENSION IF NOT EXISTS vector;" \
  && docker compose exec -T postgres psql -U guitar -d guitar -c "SELECT '[1,2,3]'::vector;" \
  | grep -q '\[1,2,3\]'
echo "PGVECTOR_OK"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash apps/api/tests/test_pgvector_up.sh`
Expected: FAIL (no `docker-compose.yml` / service not running).

- [ ] **Step 3: Write `.env.example`**

```dotenv
# ---- Postgres ----
POSTGRES_USER=guitar
POSTGRES_PASSWORD=guitar
POSTGRES_DB=guitar
DATABASE_URL=postgresql+psycopg://guitar:guitar@postgres:5432/guitar

# ---- LLM provider (dev default: local Qwen via vLLM) ----
LLM_PROVIDER=qwen
LLM_API_KEY=none
LLM_BASE_URL=http://qwen-vllm:6888/v1
LLM_MODEL=/models/Qwen3.5-9B
EMBED_BASE_URL=http://qwen-emb-vllm:8090/v1
EMBED_MODEL=qwen3-emb-4b
EMBED_DIM=2560

# ---- App ----
API_PORT=8791
WEB_PORT=8790
NEXT_PUBLIC_API_BASE=http://localhost:8791
```

- [ ] **Step 4: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-guitar}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-guitar}
      POSTGRES_DB: ${POSTGRES_DB:-guitar}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-guitar} -d ${POSTGRES_DB:-guitar}"]
      interval: 5s
      timeout: 3s
      retries: 20
    networks: [appnet]

  api:
    build: { context: ./apps/api }
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
    ports:
      - "127.0.0.1:${API_PORT:-8791}:8000"
    networks: [appnet, platform-net]   # platform-net → reach qwen-vllm / qwen-emb-vllm

  worker:
    build: { context: ./apps/api }
    env_file: .env
    command: ["python", "-c", "print('worker stub — ingestion lands in the Brain plan'); import time; time.sleep(1e9)"]
    depends_on:
      postgres: { condition: service_healthy }
    networks: [appnet, platform-net]

  web:
    build: { context: ./apps/web }
    env_file: .env
    depends_on: [api]
    ports:
      - "127.0.0.1:${WEB_PORT:-8790}:3000"
    networks: [appnet]

volumes:
  pgdata:

networks:
  appnet:
  platform-net:
    external: true
```

- [ ] **Step 5: Bring postgres up and verify**

Run:
```bash
cp .env.example .env
docker network inspect platform-net >/dev/null 2>&1 || docker network create platform-net
docker compose up -d postgres
chmod +x apps/api/tests/test_pgvector_up.sh && bash apps/api/tests/test_pgvector_up.sh
```
Expected: prints `PGVECTOR_OK`.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml .env.example apps/api/tests/test_pgvector_up.sh
git commit -m "feat(foundations): monorepo skeleton + postgres/pgvector via compose"
```

---

## Task 2: FastAPI skeleton + config + liveness

**Files:**
- Create: `apps/api/pyproject.toml`, `apps/api/Dockerfile`, `apps/api/app/{__init__,main,config}.py`, `apps/api/app/routers/health.py`, `apps/api/tests/{conftest,test_health}.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings) reading the `.env` keys from Task 1; FastAPI app `app.main:app`; `GET /health/live → {"status":"ok"}`.
- Consumes: nothing.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "guitar-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pydantic-settings>=2.5",
  "sqlalchemy>=2.0",
  "psycopg[binary]>=3.2",
  "alembic>=1.13",
  "pgvector>=0.3",
  "openai>=1.54",
  "httpx>=0.27",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-asyncio>=0.24", "numpy>=2.0"]

[tool.pytest.ini_options]
markers = ["integration: hits live vLLM at :6888/:8090"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Write the failing test**

Create `apps/api/tests/test_health.py`:
```python
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_live():
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/api && pip install -e ".[dev]" && pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: app.main`.

- [ ] **Step 4: Write `config.py`**

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://guitar:guitar@postgres:5432/guitar"

    llm_provider: str = "qwen"
    llm_api_key: str = "none"
    llm_base_url: str = "http://qwen-vllm:6888/v1"
    llm_model: str = "/models/Qwen3.5-9B"
    embed_base_url: str = "http://qwen-emb-vllm:8090/v1"
    embed_model: str = "qwen3-emb-4b"
    embed_dim: int = 2560

settings = Settings()
```

- [ ] **Step 5: Write `routers/health.py` and `main.py`**

`app/routers/health.py`:
```python
from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])

@router.get("/live")
def live():
    return {"status": "ok"}
```

`app/main.py`:
```python
from fastapi import FastAPI
from app.routers import health

app = FastAPI(title="Guitar Tutor Copilot API")
app.include_router(health.router)
```
(also create empty `app/__init__.py` and `app/routers/__init__.py`)

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd apps/api && pytest tests/test_health.py -v`
Expected: PASS.

- [ ] **Step 7: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 8: Verify in-container**

Run: `docker compose up -d --build api && sleep 3 && curl -s http://localhost:8791/health/live`
Expected: `{"status":"ok"}`.

- [ ] **Step 9: Commit**

```bash
git add apps/api
git commit -m "feat(foundations): FastAPI skeleton + settings + liveness"
```

---

## Task 3: `LLMProvider` seam + `QwenVLLM` (chat · embed · health)

**Files:**
- Create: `apps/api/app/llm/{__init__,base,qwen,embeddings,factory}.py`, `apps/api/tests/{test_embeddings_helpers,test_llm_qwen_integration}.py`

**Interfaces:**
- Produces:
  - `l2_normalize(vec: list[float]) -> list[float]` and `query_instruct(q: str) -> str` in `app.llm.embeddings`.
  - ABC `LLMProvider` with `chat(messages: list[dict], *, temperature: float = 0.3, enable_thinking: bool = False) -> str`, `embed(texts: list[str], *, is_query: bool = False) -> list[list[float]]`, `health() -> dict`.
  - `QwenVLLM(LLMProvider)`; `get_provider() -> LLMProvider` in `app.llm.factory` selecting on `settings.llm_provider`.
- Consumes: `settings` (Task 2).
- Note: `stream()`, `chat_with_tools()`, and `guided_json()` are added to the ABC + `QwenVLLM` in the **Agent+Tools+HITL** plan, where they're driven end-to-end; Foundations proves connectivity with chat/embed/health.

- [ ] **Step 1: Write the failing unit test (pure helpers)**

Create `apps/api/tests/test_embeddings_helpers.py`:
```python
import math
from app.llm.embeddings import l2_normalize, query_instruct

def test_l2_normalize_unit_length():
    out = l2_normalize([3.0, 4.0])
    assert math.isclose(math.hypot(*out), 1.0, rel_tol=1e-9)
    assert math.isclose(out[0], 0.6) and math.isclose(out[1], 0.8)

def test_l2_normalize_zero_vector_is_safe():
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]

def test_query_instruct_prefix():
    assert query_instruct("what is a humbucker?").startswith(
        "Instruct: Given a question, retrieve passages that answer it\nQuery: "
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && pytest tests/test_embeddings_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: app.llm.embeddings`.

- [ ] **Step 3: Implement `embeddings.py`**

```python
import math

def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]

def query_instruct(q: str) -> str:
    return f"Instruct: Given a question, retrieve passages that answer it\nQuery: {q}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && pytest tests/test_embeddings_helpers.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Implement `base.py` (ABC)**

```python
from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    def chat(self, messages: list[dict], *, temperature: float = 0.3,
             enable_thinking: bool = False) -> str: ...

    @abstractmethod
    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]: ...

    @abstractmethod
    def health(self) -> dict: ...
```

- [ ] **Step 6: Implement `qwen.py`**

```python
import httpx
from openai import OpenAI
from app.config import settings
from app.llm.base import LLMProvider
from app.llm.embeddings import l2_normalize, query_instruct

class QwenVLLM(LLMProvider):
    def __init__(self) -> None:
        self._client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)

    def chat(self, messages, *, temperature=0.3, enable_thinking=False) -> str:
        resp = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
        )
        return resp.choices[0].message.content or ""

    def embed(self, texts, *, is_query=False) -> list[list[float]]:
        inputs = [query_instruct(t) for t in texts] if is_query else list(texts)
        vectors: list[list[float]] = []
        for i in range(0, len(inputs), 16):  # batch 16
            batch = inputs[i : i + 16]
            r = httpx.post(
                f"{settings.embed_base_url}/embeddings",
                json={"model": settings.embed_model, "input": batch},
                timeout=60,
            )
            r.raise_for_status()
            vectors.extend(l2_normalize(d["embedding"]) for d in r.json()["data"])
        return vectors

    def health(self) -> dict:
        out = {"llm": False, "embed": False}
        try:
            httpx.get(f"{settings.llm_base_url}/models", timeout=5).raise_for_status()
            out["llm"] = True
        except Exception:
            pass
        try:
            httpx.get(f"{settings.embed_base_url}/models", timeout=5).raise_for_status()
            out["embed"] = True
        except Exception:
            pass
        return out
```

- [ ] **Step 7: Implement `factory.py`**

```python
from app.config import settings
from app.llm.base import LLMProvider
from app.llm.qwen import QwenVLLM

def get_provider() -> LLMProvider:
    if settings.llm_provider == "qwen":
        return QwenVLLM()
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")  # 'claude' added later
```

- [ ] **Step 8: Write the live integration smoke (the "drive the real model" rule)**

Create `apps/api/tests/test_llm_qwen_integration.py`:
```python
import pytest
from app.llm.factory import get_provider
from app.config import settings

@pytest.mark.integration
def test_qwen_chat_roundtrip():
    p = get_provider()
    if not p.health()["llm"]:
        pytest.skip("Qwen LLM not reachable")
    out = p.chat([{"role": "user", "content": "Reply with exactly: PONG"}])
    assert "PONG" in out.upper()

@pytest.mark.integration
def test_qwen_embed_dim_and_norm():
    import math
    p = get_provider()
    if not p.health()["embed"]:
        pytest.skip("Qwen embed not reachable")
    [v] = p.embed(["a humbucker is a type of guitar pickup"])
    assert len(v) == settings.embed_dim              # 2560
    assert math.isclose(math.sqrt(sum(x*x for x in v)), 1.0, rel_tol=1e-3)
```

- [ ] **Step 9: Run the smoke against live vLLM**

Run (from the host, with the vLLM containers up; use host URLs):
```bash
cd apps/api && LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1 \
  pytest -m integration tests/test_llm_qwen_integration.py -v
```
Expected: 2 PASS (or SKIP if a server is down — then start it and re-run; do not mark the task done on skips).

- [ ] **Step 10: Commit**

```bash
git add apps/api/app/llm apps/api/tests/test_embeddings_helpers.py apps/api/tests/test_llm_qwen_integration.py
git commit -m "feat(foundations): LLMProvider seam + QwenVLLM (chat/embed/health)"
```

---

## Task 4: Core DB models + Alembic migration

**Files:**
- Create: `apps/api/app/db.py`, `apps/api/app/models/{__init__,base,student,block,knowledge}.py`, `apps/api/alembic.ini`, `apps/api/alembic/env.py`, `apps/api/tests/test_models_roundtrip.py`

**Interfaces:**
- Produces: SQLAlchemy `Base`; models `Student`, `Block` (self-referential `parent_id`), `KnowledgeSource`, `Chunk` (with `embedding` column `Vector(2560)`); `get_db()` session dependency; a first Alembic migration creating these + the `vector` extension.
- Consumes: `settings.database_url`, `settings.embed_dim`.
- Note: `Assignment`, `Progress`, `LessonLog`, `Note`, `Artifact`, `ChatSession/Message` tables are added by their own engine plans; Foundations lands the tree + vector pattern.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_models_roundtrip.py`:
```python
import uuid
from app.db import Base, engine, SessionLocal
from app.models.student import Student
from app.models.block import Block
from app.models.knowledge import KnowledgeSource, Chunk

def setup_module(_):
    Base.metadata.create_all(engine)  # test DB build (Alembic verified separately)

def test_recursive_block_tree_and_vector_roundtrip():
    db = SessionLocal()
    try:
        course = Block(kind="course", title="Guitar Tone & Gear", order=0, language="en")
        db.add(course); db.flush()
        module = Block(kind="module", title="The Guitar", order=0, parent_id=course.id, language="en")
        db.add(module); db.flush()
        assert module.parent_id == course.id

        src = KnowledgeSource(type="pdf", title="Getting Great Guitar Sounds",
                              status="ready", language="en")
        db.add(src); db.flush()
        chunk = Chunk(source_id=src.id, text="A humbucker cancels hum.",
                      section_path="Ch1", page=25, embedding=[0.1] * 2560)
        db.add(chunk); db.commit()

        got = db.query(Chunk).filter_by(id=chunk.id).one()
        assert len(got.embedding) == 2560 and got.source_id == src.id
    finally:
        db.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && pytest tests/test_models_roundtrip.py -v`
Expected: FAIL — `ModuleNotFoundError: app.db`.

- [ ] **Step 3: Write `db.py`**

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 4: Write `models/base.py`**

```python
import uuid
from datetime import datetime
from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

class PkMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 server_default=func.now(),
                                                 onupdate=func.now())
```

- [ ] **Step 5: Write `models/student.py`, `models/block.py`, `models/knowledge.py`**

`student.py`:
```python
import uuid
from datetime import date
from sqlalchemy import String, Date
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models.base import PkMixin, TimestampMixin

class Student(Base, PkMixin, TimestampMixin):
    __tablename__ = "student"
    name: Mapped[str] = mapped_column(String(200))
    birthdate: Mapped[date | None] = mapped_column(Date, nullable=True)
    level: Mapped[str | None] = mapped_column(String(50), nullable=True)
    instrument: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(5), default="el")
    status: Mapped[str] = mapped_column(String(20), default="active")
```

`block.py`:
```python
import uuid
from sqlalchemy import String, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base
from app.models.base import PkMixin, TimestampMixin

class Block(Base, PkMixin, TimestampMixin):
    __tablename__ = "block"
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("block.id", ondelete="CASCADE"), nullable=True, index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30), default="topic")   # soft, relabelable
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    est_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="el")
    is_template: Mapped[bool] = mapped_column(default=False)
    children = relationship("Block", cascade="all, delete-orphan")
```

`knowledge.py`:
```python
import uuid
from sqlalchemy import String, Integer, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.db import Base
from app.config import settings
from app.models.base import PkMixin, TimestampMixin

class KnowledgeSource(Base, PkMixin, TimestampMixin):
    __tablename__ = "knowledge_source"
    type: Mapped[str] = mapped_column(String(20))          # pdf/url/text/note/image
    title: Mapped[str] = mapped_column(String(400))
    status: Mapped[str] = mapped_column(String(20), default="ingesting")
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)

class Chunk(Base, PkMixin, TimestampMixin):
    __tablename__ = "chunk"
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    section_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embed_dim))
```

`models/__init__.py` (so Alembic autogenerate sees them):
```python
from app.models.student import Student          # noqa: F401
from app.models.block import Block              # noqa: F401
from app.models.knowledge import KnowledgeSource, Chunk  # noqa: F401
```

- [ ] **Step 6: Initialize Alembic and wire metadata + pgvector**

Run: `cd apps/api && alembic init alembic`
Then set `sqlalchemy.url` handling and target metadata in `alembic/env.py` — replace its config block with:
```python
from app.config import settings
from app.db import Base
import app.models  # noqa: F401  (registers all tables)
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata
```

- [ ] **Step 7: Create the first migration and add the vector extension**

Run: `cd apps/api && alembic revision --autogenerate -m "core: student, block, knowledge"`
Then edit the generated version file so `upgrade()` **starts** with:
```python
op.execute("CREATE EXTENSION IF NOT EXISTS vector")
```
(so the `Vector` column type resolves).

- [ ] **Step 8: Apply the migration, then run the roundtrip test**

Run:
```bash
cd apps/api && DATABASE_URL=postgresql+psycopg://guitar:guitar@localhost:5432/guitar alembic upgrade head
DATABASE_URL=postgresql+psycopg://guitar:guitar@localhost:5432/guitar pytest tests/test_models_roundtrip.py -v
```
Expected: migration applies cleanly; test PASSES.
*(Ensure the postgres port is published to the host for this: add `ports: ["127.0.0.1:5432:5432"]` to the postgres service, or run pytest inside the api container.)*

- [ ] **Step 9: Commit**

```bash
git add apps/api/app/db.py apps/api/app/models apps/api/alembic apps/api/alembic.ini apps/api/tests/test_models_roundtrip.py
git commit -m "feat(foundations): core DB models (student/block/knowledge) + first migration"
```

---

## Task 5: Next.js shell — theme (light/dark) + i18n (GR/EN) + no-store

**Files:**
- Scaffold + Create: `apps/web/*` (see File Structure), notably `next.config.ts`, `src/middleware.ts`, `src/i18n/{routing,request}.ts`, `src/app/[locale]/{layout,page}.tsx`, `src/components/{theme-provider,theme-toggle}.tsx`, `src/messages/{en,el}.json`, `apps/web/Dockerfile`

**Interfaces:**
- Produces: a Next.js app that serves `/en` and `/el`, toggles light/dark (persisted), sends `Cache-Control: no-store` on HTML, and shows a shell landing page. Consumes `NEXT_PUBLIC_API_BASE`.

- [ ] **Step 1: Scaffold the app and libraries**

Run (verify exact flags via context7 if the CLI changed):
```bash
cd apps && npx create-next-app@latest web --ts --app --tailwind --src-dir --eslint --use-npm --no-import-alias
cd web && npx shadcn@latest init -d && npm i next-intl next-themes && npx shadcn@latest add button
```

- [ ] **Step 2: Write the failing test (Playwright smoke)**

Create `apps/web/tests/shell.spec.ts`:
```ts
import { test, expect } from "@playwright/test";

test("serves Greek and English shell", async ({ page }) => {
  await page.goto("/el");
  await expect(page.getByTestId("app-title")).toBeVisible();
  await page.goto("/en");
  await expect(page.getByTestId("app-title")).toBeVisible();
});

test("theme toggle flips the html class", async ({ page }) => {
  await page.goto("/en");
  const html = page.locator("html");
  const before = await html.getAttribute("class");
  await page.getByTestId("theme-toggle").click();
  await expect(html).not.toHaveClass(before ?? "");
});
```
Install + config: `npm i -D @playwright/test && npx playwright install --with-deps chromium` and add a `playwright.config.ts` with `webServer: { command: "npm run dev", port: 3000 }` and `use: { baseURL: "http://localhost:3000" }`.

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/web && npx playwright test`
Expected: FAIL — routes/testids don't exist yet.

- [ ] **Step 4: Configure i18n routing + request**

`src/i18n/routing.ts`:
```ts
import { defineRouting } from "next-intl/routing";
export const routing = defineRouting({ locales: ["en", "el"], defaultLocale: "el" });
```
`src/i18n/request.ts`:
```ts
import { getRequestConfig } from "next-intl/server";
import { routing } from "./routing";
export default getRequestConfig(async ({ requestLocale }) => {
  let locale = await requestLocale;
  if (!locale || !routing.locales.includes(locale as any)) locale = routing.defaultLocale;
  return { locale, messages: (await import(`../messages/${locale}.json`)).default };
});
```
`src/messages/en.json`: `{ "app": { "title": "Guitar Tutor Copilot" } }`
`src/messages/el.json`: `{ "app": { "title": "Βοηθός Δασκάλου Κιθάρας" } }`

- [ ] **Step 5: Middleware — next-intl + `no-store` on HTML**

`src/middleware.ts`:
```ts
import createMiddleware from "next-intl/middleware";
import { routing } from "./i18n/routing";

const intl = createMiddleware(routing);

export default function middleware(req: Request) {
  const res = intl(req as any);
  res.headers.set("Cache-Control", "no-store"); // App-Router Vary:rsc trap (spec §5.4)
  return res;
}

export const config = { matcher: ["/((?!api|_next|.*\\..*).*)"] }; // excludes /_next → assets stay immutable
```

- [ ] **Step 6: Theme provider + toggle + locale layout/page**

`src/components/theme-provider.tsx`:
```tsx
"use client";
import { ThemeProvider as NextThemes } from "next-themes";
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return <NextThemes attribute="class" defaultTheme="system" enableSystem>{children}</NextThemes>;
}
```
`src/components/theme-toggle.tsx`:
```tsx
"use client";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  return (
    <Button data-testid="theme-toggle" variant="outline"
      onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>🌓</Button>
  );
}
```
`src/app/[locale]/layout.tsx`:
```tsx
import { NextIntlClientProvider } from "next-intl";
import { getMessages } from "next-intl/server";
import { ThemeProvider } from "@/components/theme-provider";
import "../globals.css";

export default async function LocaleLayout(
  { children, params }: { children: React.ReactNode; params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  const messages = await getMessages();
  return (
    <html lang={locale} suppressHydrationWarning>
      <body>
        <ThemeProvider>
          <NextIntlClientProvider messages={messages}>{children}</NextIntlClientProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
```
`src/app/[locale]/page.tsx`:
```tsx
import { useTranslations } from "next-intl";
import { ThemeToggle } from "@/components/theme-toggle";

export default function Home() {
  const t = useTranslations("app");
  return (
    <main className="min-h-screen flex flex-col items-center justify-center gap-6">
      <h1 data-testid="app-title" className="text-3xl font-semibold">{t("title")} 🎸</h1>
      <ThemeToggle />
    </main>
  );
}
```
Delete the default `src/app/page.tsx` / root layout that create-next-app made (the `[locale]` segment replaces them). Set `output: "standalone"` in `next.config.ts`.

- [ ] **Step 7: Run the smoke to verify it passes**

Run: `cd apps/web && npx playwright test`
Expected: PASS (both tests).

- [ ] **Step 8: Write `apps/web/Dockerfile` (standalone) and build**

```dockerfile
FROM node:20-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build
FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
```
Run: `docker compose up -d --build web && sleep 3 && curl -sI http://localhost:8790/el | grep -i cache-control`
Expected: `cache-control: no-store`.

- [ ] **Step 9: Commit**

```bash
git add apps/web
git commit -m "feat(foundations): Next.js shell — light/dark theme + GR/EN i18n + no-store"
```

---

## Task 6: Aggregate readiness + full-stack smoke

**Files:**
- Modify: `apps/api/app/routers/health.py`
- Create: `apps/api/tests/test_health_ready.py`, `scripts/smoke.sh`

**Interfaces:**
- Produces: `GET /health/ready → {"db": bool, "llm": bool, "embed": bool}` (200 if db true; degrades gracefully if models down); `scripts/smoke.sh` that brings the stack up and asserts web + api reachable.
- Consumes: `get_db` (Task 4), `get_provider().health()` (Task 3).

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_health_ready.py`:
```python
from fastapi.testclient import TestClient
from app.main import app

def test_ready_shape():
    r = TestClient(app).get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"db", "llm", "embed"}
    assert isinstance(body["db"], bool)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && pytest tests/test_health_ready.py -v`
Expected: FAIL — no `/health/ready` route.

- [ ] **Step 3: Implement `/health/ready`**

Append to `app/routers/health.py`:
```python
from sqlalchemy import text
from app.db import SessionLocal
from app.llm.factory import get_provider

@router.get("/ready")
def ready():
    db_ok = False
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        pass
    models = get_provider().health()
    return {"db": db_ok, "llm": models["llm"], "embed": models["embed"]}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && pytest tests/test_health_ready.py -v`
Expected: PASS (`db` may be False in the unit env — the shape is what's asserted).

- [ ] **Step 5: Write `scripts/smoke.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
docker network inspect platform-net >/dev/null 2>&1 || docker network create platform-net
docker compose up -d --build
echo "waiting for services…"; sleep 6
curl -sf http://localhost:8791/health/live >/dev/null && echo "api live: OK"
curl -sf http://localhost:8790/el         >/dev/null && echo "web el:   OK"
echo "ready: $(curl -s http://localhost:8791/health/ready)"
```

- [ ] **Step 6: Run the full-stack smoke**

Run: `chmod +x scripts/smoke.sh && ./scripts/smoke.sh`
Expected: `api live: OK`, `web el: OK`, and a `ready:` line. With the vLLM containers up, `llm`/`embed` are `true`.

- [ ] **Step 7: Commit**

```bash
git add apps/api/app/routers/health.py apps/api/tests/test_health_ready.py scripts/smoke.sh
git commit -m "feat(foundations): aggregate readiness + full-stack smoke"
```

---

## Self-Review

**1. Spec coverage (Foundations scope = spec §9.1):** monorepo ✓ (T1) · Docker Compose ✓ (T1) · Postgres+pgvector ✓ (T1/T4) · FastAPI skeleton ✓ (T2) · Next.js skeleton + theme + i18n ✓ (T5) · `LLMProvider` seam + `QwenVLLM` + health ✓ (T3) · DB schema + Alembic for core §4 entities ✓ (T4) · `platform-net` wiring ✓ (T1) · `output:standalone` + no-store ✓ (T5). Deferred-by-design and noted in-plan: streaming/tools/guided-JSON on the provider (Agent plan); Assignment/Progress/LessonLog/Note/Artifact/ChatSession tables (their engine plans).

**2. Placeholder scan:** the `worker` service is an explicit labeled stub (real ingestion is the Brain plan) — not a hidden TODO. All code steps carry complete code; scaffolding steps use exact CLI commands. No "add error handling"-style hand-waving.

**3. Type consistency:** `LLMProvider.chat/embed/health` signatures match between `base.py`, `qwen.py`, and the integration test; `get_provider()` return type is `LLMProvider`; `Vector(settings.embed_dim)` (2560) matches the embed test's `settings.embed_dim` assertion and the `.env` `EMBED_DIM`. `Block.parent_id`/`kind`/`language` match the roundtrip test. `Chunk.embedding` is `list[float]` len 2560 in both model and test.
