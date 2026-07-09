# Async Curriculum Generation + Deploy Implementation Plan (Plan 8)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Make the slow (49–179 s) curriculum generation asynchronous (enqueue → poll) so it survives Cloudflare's ~100 s edge timeout under orange-cloud, then deploy the stack to `guitar` + `guitar-api.cgrigoriadis.online`.

**Architecture:** A generic DB-backed `generation_job` table + in-process execution via FastAPI `BackgroundTasks` (no new infra). `POST /curricula/generate` enqueues → returns 202 `{job_id}`; a runner (its OWN DB session) executes `generate_curriculum` and records status/result; `GET /jobs/{id}` polls; a startup sweep fails restart-orphaned jobs. The frontend polls behind the same spinner. Then deploy: prod web build + nginx orange-cloud vhosts (AlphaTab-safe CSP) + DNS + TLS + live verify.

**Tech Stack:** FastAPI `BackgroundTasks`, SQLAlchemy, Alembic; Next.js/next-intl; nginx, Cloudflare, certbot, Docker.

## Global Constraints (verbatim)

- The background task MUST open its **own** `SessionLocal()` (`app/db.py`) — the request's `Depends(get_db)` session is torn down when the 202 response is sent. **Commit the job row BEFORE returning 202** so an immediate poll sees it.
- **Contract change** (single PoC consumer, unmerged branch): `POST /curricula/generate` now returns **202 `{job_id, status}`**, NOT the tree. No parallel `/generate-async`.
- Error mapping **moves into the runner** and is stored on the job: `GuidedJSONError` → `error_kind="upstream"` (was 502); `openai.APIConnectionError` / `httpx.TransportError` (excluding `HTTPStatusError`) → `"timeout"` (was 504); any other exception → `"internal"`. The runner is the ONE place we catch broadly — an uncaught background exception silently strands the job in `running`.
- **Startup sweep:** at app startup, mark EVERY `pending`/`running` job `failed` (`error_kind="internal"`, error `"interrupted by a restart"`) — all such rows are orphaned at startup.
- **Scope:** async wraps ONLY curriculum generation. Artifact-gen + segmentation stay synchronous.
- Tests isolate to `guitar_test` (conftest). **Bilingual GR/EN** on user-facing strings. Web dev/test **port 3100**.
- **Deploy:** orange-cloud BOTH subdomains. The `guitar` (web) nginx vhost CSP MUST allow `worker-src blob:` + same-origin `script-src`/`connect-src`/`font-src`/`media-src` (AlphaTab). The web image bakes `NEXT_PUBLIC_API_BASE` at Docker **build** time → rebuild with `https://guitar-api.cgrigoriadis.online`.
- **Testing BackgroundTasks:** Starlette's `TestClient` runs background tasks AFTER the response, in-process. So enqueue/integration tests MUST monkeypatch `run_curriculum_job` (or `generate_curriculum`) to a fast stub — otherwise the test triggers a real 49–179 s LLM call.

## File Structure
```
apps/api/app/
  models/generation_job.py        # GenerationJob(kind,status,params,result_root_id,error,error_kind,+mixins)
  models/__init__.py (modify)     # register the model
  jobs/__init__.py jobs/runner.py # run_curriculum_job(job_id) — own session
  jobs/sweep.py                   # sweep_orphaned_jobs(db)
  schemas/jobs.py                 # JobOut
  routers/jobs.py                 # GET /jobs/{job_id}
  routers/curriculum.py (modify)  # POST /curricula/generate -> 202 {job_id}
  main.py (modify)                # lifespan: sweep on startup; include jobs.router
  alembic/versions/<new>.py       # generation_job table (head b2335b2917ed)
apps/web/src/
  lib/api.ts (modify)             # startCurriculumGeneration -> {job_id}; getJob(id)
  components/curriculum/generate-dialog.tsx (modify)  # enqueue -> poll -> render
  messages/{en,el}.json (modify)  # poll/status copy
deploy/
  nginx/guitar.conf guitar-api.conf   # orange-cloud vhosts (AlphaTab CSP)
  README.md                       # ordered go-live runbook
```

---

## Task 1: `generation_job` model + migration
**Files:** Create `app/models/generation_job.py`; modify `app/models/__init__.py` (register); new Alembic migration; test.
**Produces:** `GenerationJob(Base, PkMixin, TimestampMixin)` — `kind: str(30)`, `status: str(20)` default `"pending"`, `params: JSON`, `result_root_id: UUID|None`, `error: Text|None`, `error_kind: str(20)|None`. Alembic migration (parent head `b2335b2917ed`) creating the table; downgrade drops it.
- [ ] TDD: model roundtrip (create a `pending` job with params, read back; status defaults to `pending`; nullable result/error start None). Migration `upgrade` creates the table with all columns; `downgrade` drops it. RED→GREEN. Commit.

## Task 2: job runner + status/poll endpoint
**Files:** Create `app/jobs/__init__.py`, `app/jobs/runner.py`, `app/schemas/jobs.py`, `app/routers/jobs.py`; modify `app/main.py` (include `jobs.router`); tests.
**Interfaces — Consumes:** `GenerationJob` (T1); `generate_curriculum(db, *, title, language, profile, domain, target_minutes_total) -> UUID` (`app/curriculum/generate.py`); `SessionLocal` (`app/db.py`, `expire_on_commit=False`); `GuidedJSONError` (`app/llm/errors.py`), `openai.APIConnectionError`, `httpx.TransportError`.
**Produces:**
- `run_curriculum_job(job_id: UUID) -> None` — opens its OWN `SessionLocal()`; loads the job; sets `running` (commit); calls `generate_curriculum(db, **job.params)` on that same session; on success sets `succeeded` + `result_root_id` and commits; `except GuidedJSONError` → `failed`/`error_kind="upstream"`; `except (openai.APIConnectionError, httpx.TransportError)` → `failed`/`error_kind="timeout"`; `except Exception` → `failed`/`error_kind="internal"` (log the detail, store generic copy); `finally` close the session. Failure copy mirrors the current 502/504 user messages.
- `JobOut{id, kind, status, result_root_id, error, error_kind, created_at, updated_at}` (`from_attributes`).
- `GET /jobs/{job_id}` → `JobOut`; 404 if unknown.
- [ ] TDD (all with `generate_curriculum` monkeypatched — no live LLM): success path sets `succeeded` + `result_root_id`; a stubbed `GuidedJSONError` → `failed`/`"upstream"`; a stubbed `APIConnectionError` → `failed`/`"timeout"`; a stubbed generic `Exception` → `failed`/`"internal"`. `GET /jobs/{id}` returns the job; unknown id → 404. RED→GREEN. Commit.

## Task 3: enqueue (contract change) + startup sweep
**Files:** Modify `app/routers/curriculum.py` (the `/curricula/generate` endpoint), `app/main.py` (lifespan); create `app/jobs/sweep.py`; modify `app/schemas/curriculum.py` if a new response model is needed; tests.
**Interfaces — Consumes:** `run_curriculum_job` (T2); `GenerationJob` (T1); `CurriculumGenerateRequest{title, language, profile, domain, target_minutes_total}`; `fastapi.BackgroundTasks`; `SessionLocal`.
**Produces:**
- `POST /curricula/generate` rewritten: build `params` from the request, create `GenerationJob(kind="curriculum", status="pending", params=params)`, `db.add` + **`db.commit()` (before returning)**, `background_tasks.add_task(run_curriculum_job, job.id)`, return **202** `{job_id, status: "pending"}` (new small response model, e.g. `JobAccepted`). The existing `try/except GuidedJSONError/APIConnection...` block is **removed** (that mapping now lives in the runner).
- `sweep_orphaned_jobs(db) -> int` — sets every job whose status is in `("pending","running")` to `failed`, `error_kind="internal"`, `error="interrupted by a restart"`; returns the count.
- `main.py` gains a `lifespan` async context manager that, on startup, runs `sweep_orphaned_jobs` on a fresh `SessionLocal()` (closed after); passed to `FastAPI(lifespan=...)`.
- [ ] TDD (monkeypatch `run_curriculum_job` to a no-op so the TestClient's post-response task doesn't hit the LLM): `POST /curricula/generate` → **202** with a `job_id`; the job row is committed + visible immediately (`GET /jobs/{id}` → `pending`). `sweep_orphaned_jobs` on a seeded `pending` + `running` job flips both to `failed`/`"internal"`; a `succeeded` job is untouched. RED→GREEN. Commit.

## Task 4: frontend poll flow
**Files:** Modify `apps/web/src/lib/api.ts`, `apps/web/src/components/curriculum/generate-dialog.tsx`, `apps/web/src/messages/en.json` + `el.json`; tests (`apps/web/tests/*.spec.ts`).
**Interfaces — Consumes:** `POST /curricula/generate` → `{job_id, status}` (T3); `GET /jobs/{id}` → `JobOut` (T2); existing `getCurriculum(root_id) -> BlockNode` (unchanged).
**Produces:**
- `api.ts`: replace `generateCurriculum(input): Promise<BlockNode>` with `startCurriculumGeneration(input): Promise<{ job_id: string; status: string }>`; add `getJob(id: string): Promise<JobOut>` + the `JobOut` TS interface (mirror `schemas/jobs.py`).
- `generate-dialog.tsx`: on submit → `startCurriculumGeneration` → poll `getJob(job_id)` every ~2 s behind the EXISTING `generate-loading` spinner → on `status==="succeeded"`, `getCurriculum(result_root_id)` → render + close (same success handoff as today); on `status==="failed"`, show `job.error` (via the existing `ApiError`/error UI); a client-side cap (~150 polls ≈ 5 min) → a "still generating — reopen to check" message (job continues server-side). Bilingual EN+EL copy for any new strings (poll/still-generating).
- [ ] Playwright (mocked, 3100): `page.route` mock `POST /curricula/generate`→`{job_id}`, `GET /jobs/{id}`→`pending` then `succeeded`+`result_root_id`, `GET /curricula/{id}`→a tree; assert the dialog polls then renders the tree. A `failed`-job mock → assert the error shows. Keep the whole `apps/web` suite green. RED→GREEN. Commit.

## Task 5: real async e2e (report)
- [ ] Via the running stack (api :8791, web, LLM :6888, DB :5434): drive a real curriculum generation THROUGH THE UI — open the Curricula page in a real browser, submit a generation, confirm the dialog polls and then renders the tree (real LLM, be patient); ALSO verify the raw loop with curl (`POST /curricula/generate` → job_id → poll `GET /jobs/{id}` → `succeeded` → `GET /curricula/{root_id}` → tree). Screenshot. Commit + update `docs/superpowers/plans/README.md`.

## Task 6: deploy artifacts — prod web build + nginx orange-cloud vhosts (AUTHORING ONLY; no live changes)
**Files:** Create `deploy/nginx/guitar.conf`, `deploy/nginx/guitar-api.conf`, `deploy/README.md`; any prod compose/env override needed.
**Interfaces — Consumes:** `.superpowers/sdd/deploy-recon.md` (the drafted vhost blocks, sibling `themis*`/`zelofood` templates, DNS state, the baked `NEXT_PUBLIC_API_BASE`, TLS/cert convention).
**Produces:**
- `deploy/nginx/guitar.conf` — server block for `guitar.cgrigoriadis.online` proxying to the web container; a **CSP** header allowing `worker-src blob:` + same-origin `script-src`/`connect-src`/`font-src`/`media-src` (AlphaTab); standard security headers; TLS cert paths per the sibling convention.
- `deploy/nginx/guitar-api.conf` — server block for `guitar-api.cgrigoriadis.online` proxying to the api container (CORS is app-owned; nginx just proxies); ordinary proxy timeouts (generation is async now — no 300 s needed); SSE/upgrade headers if the app streams.
- A documented **web image rebuild** with `NEXT_PUBLIC_API_BASE=https://guitar-api.cgrigoriadis.online` (build arg / env), and `deploy/README.md`: the exact ordered go-live steps, each tagged with who runs it (subagent-safe vs. needs-Chris-sudo/DNS). NO live system changes in this task.
- [ ] Verify (non-live): the web image builds with the prod API base (`docker build`/compose build succeeds); the drafted nginx configs pass a syntax check (`nginx -t` against a throwaway include, or `nginx -T`-style parse — no install). Commit.

## Task 7: go-live (INTERACTIVE — controller + Chris; NOT a subagent task)
- [ ] Following `deploy/README.md`, WITH Chris for privileged steps: install the two nginx vhosts (sudo) + `nginx -t` + reload; create DNS A/AAAA records for both subdomains → home IP, **orange-cloud** (Chris via Cloudflare); provision TLS (certbot, sudo); rebuild + restart containers with the prod web image; confirm home-IP + router port-forward. Then **VERIFY live end-to-end via Cloudflare**: the site loads over HTTPS; Knowledge Q&A, Students, Curricula board, and the Artifacts gallery all work; a REAL curriculum generation completes through the async job/poll flow **proxied by Cloudflare** (the whole point of Phase 1); AlphaTab tab playback works (CSP correct); screenshot. Update `docs/superpowers/plans/README.md` + the SDD ledger: Plan 8 complete, site live.

## Self-Review
Coverage vs spec: job model+migration (T1) ✓ · runner own-session + error_kind mapping (T2) ✓ · enqueue 202 + startup sweep (T3) ✓ · frontend poll behind same spinner (T4) ✓ · real async e2e (T5) ✓ · deploy configs incl. AlphaTab CSP + prod API base (T6) ✓ · go-live + live verify via Cloudflare (T7) ✓. Scope respected: only curriculum generation goes async; artifact-gen/segmentation untouched. Types: `GenerationJob`→`run_curriculum_job`→`JobOut`→`GET /jobs/{id}`; `startCurriculumGeneration`→`getJob`→`getCurriculum`. Deferred (spec §out-of-scope): real queue, jobs-list UI, async artifact-gen, job cancellation. Interactive caveat: T7 needs Chris (sudo/DNS/Cloudflare) — the only non-subagent task.
