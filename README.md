# Guitar Tutor Copilot

A single-tutor PoC: an AI copilot for a guitar teacher — a real 77-page scanned
tone/gear book (OCR'd, page-addressable), students/curricula/lessons/notes/
progress, generated teaching artifacts (chord diagrams, scales, tabs, tone
recipes), and a chat copilot that is **forced** to search the tutor's own
library before it answers a content question, cites the real page it used,
and gates every write behind a human approval card.

No auth — this deploys origin-locked behind Cloudflare for a single user.

## Stack

- **API** — FastAPI + SQLAlchemy, Postgres 16 + pgvector, Alembic migrations.
- **Web** — Next.js (App Router), bilingual (en/el), `react-markdown` for the
  chat transcript, AlphaTab for tab/notation rendering+playback.
- **Model** — a local Qwen3.5-9B (chat + tool-calling + vision-OCR) and a
  Qwen3 embedding model, both served by vLLM on a **separate, pre-existing
  Docker network** (`platform-net`) shared with sibling apps on this host —
  this repo's `docker-compose.yml` does **not** start them; they must already
  be running (`qwen-vllm`, `qwen-emb-vllm` on that network) before `api`
  will report itself healthy.

## Run it

Prerequisites: Docker + Docker Compose, and the two vLLM containers on
`platform-net` already up (`docker network inspect platform-net` should list
`qwen-vllm`/`qwen-emb-vllm` — ask whoever manages this host's shared model
servers if they aren't).

```bash
cd /mnt/nvme2TB/guitar_tutor
cp .env.example .env   # defaults are fine for local dev; edit if needed
docker compose up -d --build
```

This starts three containers:

| Service | Container | Port |
|---|---|---|
| `web` (Next.js) | `guitar_tutor-web-1` | `127.0.0.1:8790` |
| `api` (FastAPI) | `guitar_tutor-api-1` | `127.0.0.1:8791` |
| `postgres` (pgvector) | `guitar_tutor-postgres-1` | `127.0.0.1:5434` |

Open **http://localhost:8790** (redirects to `/en` or `/el`).

Verify the stack is actually healthy before relying on it — a running
container is not the same as a working one:

```bash
curl -s http://localhost:8791/health/ready
# {"db":true,"llm":true,"embed":true}  <- all three must be true
```

If `db` is `false`, or a chat turn 500s with `column message.citations does
not exist` (or any other `UndefinedColumn`), the app DB is behind on
migrations — apply them (safe, additive, never destroys data):

```bash
docker compose exec api alembic upgrade head
docker compose exec api alembic current   # should print the newest revision, "(head)"
```

### Running the test suites

Backend (from `apps/api`, own `.venv`):

```bash
cd apps/api
./.venv/bin/python -m pytest -m "not integration" -q      # fast, no live model — CI gate
LLM_BASE_URL=http://localhost:6888/v1 EMBED_BASE_URL=http://localhost:8090/v1 \
  ./.venv/bin/python -m pytest -m integration -q           # hits the real model + real app DB
```

(`-m integration` tests are read-only against the real `guitar` app DB, or
write only through the same HITL-suspended paths a real chat session would —
see `apps/api/tests/test_copilot_live.py`'s own module docstring. Host-side
runs need `LLM_BASE_URL`/`EMBED_BASE_URL` pointed at the vLLM servers'
host-published ports, since `app.config.Settings`' defaults are the
container-internal `platform-net` hostnames.)

Frontend (from `apps/web`):

```bash
cd apps/web
npx playwright test
```

## What it does

- **Library** — upload a PDF/URL/text source; PDFs are paginated + OCR'd
  (vision model) page by page, each page individually addressable and
  scan-viewable in the **Reader**.
- **Curricula** — LLM-generated (guided-JSON, schema-validated), grounded in
  the Library when asked, deterministically segmented into modules/lessons.
- **Lessons** — author a lesson straight from a Reader text selection
  (async job, grounded in exactly the selected passage); split/merge/add
  sessions deterministically (no LLM in the edit path).
- **Artifacts** — chord diagrams, scale diagrams, tab/notation (AlphaTab,
  playable), tone recipes, signal chains, amp-dial cards, gear cards — every
  one LLM-generated as a **schema-validated spec**, never free-typed prose.
- **Chat copilot** — a ReAct tool-calling loop over a ~20-tool registry
  (reads dispatch inline; every mutation suspends for human approval —
  `ApprovalRequest`/Approve/Edit/Reject, resumed after the decision). Content
  questions **force** a library search before the model's first reply (the
  model never gets a turn where it could decline to look); a grounded answer
  carries citation chips that deep-link into the Reader at the real cited
  page; a question the library doesn't cover is answered honestly, labelled
  general knowledge. Markdown-rendered, SSE-streamed for the plain-answer
  case.

## Implementation plans

Full designs + task-by-task detail: `docs/superpowers/plans/README.md` and
`docs/superpowers/specs/`. Progress log (every task, every finding, every
bug caught and fixed): `.superpowers/sdd/progress.md`.

| # | Sub-project | Plan | Status |
|---|---|---|---|
| A | Library | `docs/superpowers/plans/2026-07-12-library.md` | ✅ **DONE** — the tutor's real 77-page book is in the app, OCR'd, page-addressable, cited answers open the real scan. |
| B | Lesson Authoring | `docs/superpowers/plans/2026-07-12-lesson-authoring.md` | ✅ **DONE** — draft a lesson from a Reader selection, edit deterministically, provenance chip to the real page. |
| C | Copilot Rebuild | `docs/superpowers/plans/2026-07-13-copilot-rebuild.md` | ✅ **DONE** — retrieval is forced (not requested), tabs can't be free-typed as ASCII, markdown + citations + streaming, HITL gate untouched. See `.superpowers/sdd/p11-task-4-report.md` for the full live acceptance run, including an honest, still-open weakness in artifact-generation content quality (below). |

(Earlier foundational plans — monorepo/DB/LLM-provider skeleton, the
Knowledge Brain, curriculum segmentation, the artifact engine, agent
tools+HITL, cockpit integration, seed content, async generation+deploy — are
plans 1-8 in `docs/superpowers/plans/README.md`; all shipped before A/B/C.)

## Known limitations (read before the demo)

- **This is a 9B model.** It is reliable at forced retrieval, at never
  free-typing ASCII tablature into the chat, and at suspending every
  mutation for approval. It is **not** perfectly reliable at labelling an
  out-of-scope answer as "general knowledge" in so many words on every
  single turn (observed ~40-60% of runs at temperature 0.3 on a repeated
  identical question use the literal phrase; the rest still plainly say the
  library doesn't cover the specific point, just without that exact label).
- **`generate_artifact` can produce a schema-valid but musically-empty tab.**
  The Pydantic `TabSpec.alphaTex` field is only validated for non-emptiness,
  not for being real AlphaTab notation syntax — the model has, twice
  (reproducibly), written a plain-English string like `"G Major Scale Tab"`
  into that field instead of actual notation. It passes validation, gets
  persisted, and then AlphaTab throws `No alphaTex data found` in the
  browser and never renders. The chat-side bug (ASCII typed into the
  transcript) is genuinely fixed; this is a **new, narrower** gap one layer
  downstream, in content quality rather than shape. See
  `.superpowers/sdd/p11-task-4-report.md` for the full writeup and a
  suggested fix (a `Field(description=...)` with a real alphaTex example, or
  a syntax-level post-generation validator).
- **`guided_json` calls can be slow under GPU contention** (single shared
  GPU with this host's other services) — comfortably under a minute in
  isolation, but has been observed to hit its own 300s server-side timeout
  and `GuidedJSONError` when something else is using the model concurrently.
  The chat UI degrades honestly when this happens (re-shows the approval
  card rather than losing the request), but a demo run alongside a live
  pytest suite on the same GPU should be avoided.
