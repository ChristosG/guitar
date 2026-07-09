# Async Curriculum Generation + Deploy — Design (Plan 8)

**Date:** 2026-07-09 · **Branch:** `build/poc` (unmerged integration branch) · **Status:** approved (Chris)

## Context & Goal

The Guitar Tutor Copilot runs locally as a working bilingual cockpit (Plans 1–4:
Foundations, Knowledge Brain, Curriculum+Segmentation, Artifact Engine). We now
deploy it to **`guitar.cgrigoriadis.online`** (web) + **`guitar-api.cgrigoriadis.online`**
(api), fronted by Cloudflare (orange-cloud) → home IP → host nginx — matching the
tutor's other apps.

**Blocker surfaced by deploy recon:** `POST /curricula/generate` runs a blocking
guided-JSON LLM call measured at **49–179 s** (`routers/curriculum.py:144`,
`curriculum/generate.py`). Cloudflare's edge proxy timeout (~100 s, not
configurable on Free/Pro) severs that connection → a **524** at the edge, before
nginx or the app's own 502/504 handling can run. (Segmentation is deterministic
and fast; a single artifact spec generates in seconds — curriculum generation is
the *only* endpoint over the limit.)

**Decision (Chris):** don't route around it with grey-cloud/DNS-only — fix it
properly with a **background-job + poll** pattern, keeping full Cloudflare
protection (orange-cloud) on both subdomains. This is also **foundational**: Plan
5's chat agent will trigger curriculum generation via a tool, and a long LLM call
behind a synchronous HTTP request is fragile everywhere (tab close, proxy timeout,
retry storms), not just behind Cloudflare. Making generation durable + pollable
now pays off twice.

**Goal:** (Phase 1) make curriculum generation asynchronous — enqueue a job,
return immediately, poll for completion — with no new infrastructure; (Phase 2)
deploy the stack to the two subdomains under Cloudflare orange-cloud.

## Decision: job-runner mechanism

**Chosen — (A) DB-backed job row + in-process execution via FastAPI
`BackgroundTasks`.** One `generation_job` table is the durable source of truth for
polling; the enqueue schedules the long work in-process (after the 202 response);
polling reads the row. **No new infrastructure** — no Redis, no Celery/RQ/arq, no
separate worker container. Right-sized for a single-instance PoC Docker deployment.

*Rejected:* (B) a real task queue (Celery/RQ/arq + Redis + worker) — robust and
horizontally scalable, but new infra + ops for a single-box PoC; revisit only if we
outgrow one instance. (C) keep synchronous, just raise nginx/CF timeouts — rejected
by the decision above and fragile independent of Cloudflare.

**Consequence to get right:** the request's DB session (`Depends(get_db)`) is torn
down when the 202 response is sent, so the background task **must open its own
`SessionLocal()`** — it cannot reuse the request session. The job row must be
**committed before the endpoint returns** so an immediate poll sees it.

## Architecture

### Data model — `generation_job` (generic by `kind`)

New table + model (`app/models/generation_job.py`) + Alembic migration
(head is `b2335b2917ed`). Deliberately generic so Plan 5's agent and (if ever
needed) artifact generation can reuse it without a new table:

| column | type | notes |
|---|---|---|
| `id` | UUID pk | `PkMixin` |
| `kind` | str(30) | `"curriculum"` for now |
| `status` | str(20) | `pending` \| `running` \| `succeeded` \| `failed` |
| `params` | JSON | the generation request (title/language/profile/domain/target_minutes_total) |
| `result_root_id` | UUID? | on success, the generated curriculum's root `Block.id` (no FK — keeps the job decoupled from Block lifecycle; validated on read) |
| `error` | text? | on failure, a user-facing message (mirrors the current 502/504 copy) |
| `error_kind` | str(20)? | `upstream` (was 502) \| `timeout` (was 504) \| `internal` — for the client to distinguish retryable cases |
| `created_at`/`updated_at` | ts | `TimestampMixin` |

### API contract

- **`POST /curricula/generate`** — **contract change** (was: run inline, return the
  tree). Now: validate the request, create a `generation_job` (`pending`, params
  captured), **commit**, schedule the runner via `BackgroundTasks`, return **202
  Accepted** with `{ job_id, status: "pending" }`. Single PoC consumer (the web
  app), unmerged branch → clean replacement, no parallel `/generate-async`.
- **`GET /jobs/{job_id}`** — new, generic. Returns
  `{ id, kind, status, result_root_id?, error?, error_kind?, created_at, updated_at }`.
  404 if unknown. On `succeeded`, `result_root_id` is set and the client fetches the
  tree via the **existing** `GET /curricula/{root_id}` (unchanged). Keeping the tree
  fetch on the existing route means the job endpoint stays a thin, reusable status
  probe.

### The runner

`app/curriculum/jobs.py` (or `app/jobs/runner.py`): `run_curriculum_job(job_id)`:
opens its **own** `SessionLocal()`; loads the job; sets `running` (commit); calls
`generate_curriculum(db, **params)`; on success sets `succeeded` + `result_root_id`;
on `GuidedJSONError` sets `failed`/`error_kind="upstream"`; on
`APIConnectionError`/`httpx.TransportError` sets `failed`/`error_kind="timeout"`;
any other exception → `failed`/`error_kind="internal"` (message logged, generic
copy stored — matching the PoC's no-blanket-catch posture, but a job runner is the
one place we DO catch broadly, because an uncaught background exception would
silently strand the job in `running`). Always closes its session.

### Startup sweep (orphan recovery)

In-process jobs don't survive a process restart. On app startup (FastAPI lifespan),
mark **every** job still in `pending`/`running` as `failed`
(`error_kind="internal"`, error "interrupted by a restart") — at startup nothing is
actually running, so any such row is definitionally orphaned. Simple, correct, no
time-threshold heuristic needed.

### Frontend poll flow

`components/curriculum/generate-dialog.tsx` (currently `await generateCurriculum()`
→ tree, behind the `generate-loading` spinner) changes to: `POST` → `{ job_id }` →
**poll `GET /jobs/{job_id}` every ~2 s** behind the **same** spinner → on
`succeeded`, `getCurriculum(result_root_id)` → render + close; on `failed`, show
`error`; client-side poll cap (~5 min) → "still generating, reopen to check"
message (the job keeps running server-side regardless). `lib/api.ts`:
`generateCurriculum` becomes `startCurriculumGeneration(input) → { job_id }`; add
`getJob(id) → JobOut`. Same UX, now durable against connection loss / CF timeout.

## Scope

**In:** async wrapping of **curriculum generation only**. **Out (stay
synchronous):** artifact generation (seconds), segmentation (deterministic).
**Out (YAGNI):** a jobs list / history UI, job cancellation, retry-from-UI (the
user just re-submits), a generic queue/worker. The table is generic by `kind` for
*future* reuse, but only curriculum enqueues in this plan.

## Testing strategy

- **Unit:** job creation + status transitions; `run_curriculum_job` with a stubbed
  `generate_curriculum` (success → succeeded+root_id; each error type → the right
  `error_kind`); the startup sweep flips pending/running → failed.
- **Integration (fast, stubbed LLM):** `POST /curricula/generate` → 202 + job_id;
  the row exists immediately (commit-before-return); after the (synchronously-driven
  in test) runner, `GET /jobs/{id}` → succeeded + result_root_id; `GET
  /curricula/{root_id}` → the tree.
- **Integration (live LLM, opt-in marker):** end-to-end enqueue → poll → tree, real
  model.
- **Frontend (Playwright, mocked, 3100):** dialog POST → poll (mock pending→succeeded)
  → tree rendered; the failed path shows the error. Tests isolate to `guitar_test`.

## Phase 2 — Deploy (orange-cloud both subdomains)

Recon (`.superpowers/sdd/deploy-recon.md`) confirmed: API CORS already allows
`https://guitar.cgrigoriadis.online`; the web Dockerfile already produces a correct
standalone build **including** the vendored `public/alphatab` assets. Remaining:

1. **Rebuild the web image** with `NEXT_PUBLIC_API_BASE=https://guitar-api.cgrigoriadis.online`
   (currently baked to `localhost:8791` at Docker build time).
2. **Author + install nginx vhosts** for `guitar` (web) and `guitar-api` (api),
   mirroring sibling vhosts (`themis*`, `zelofood`). The `guitar` vhost sets a
   **CSP that allows AlphaTab**: `worker-src blob:` + same-origin
   `script-src`/`connect-src`/`font-src`/`media-src` — otherwise offline playback
   breaks in prod despite correct bundling. With generation now async, the API vhost
   needs only ordinary proxy timeouts (enqueue + fast polls), not the 300 s
   `proxy_read_timeout` the sync design would have required.
3. **DNS:** A/AAAA records for both subdomains → home IP, **orange-cloud** (proxied).
4. **TLS:** certbot (or Cloudflare origin cert) per the sibling-vhost convention.
5. **Verify live, end-to-end via Cloudflare:** the site loads; Knowledge Q&A,
   Students, Curricula board, and the Artifacts gallery work; and a **real curriculum
   generation completes through the job/poll flow** proxied by Cloudflare (the whole
   point of Phase 1) — screenshot.

**Needs Chris (can't be done headless):** `sudo` to install the nginx configs +
reload; the DNS/Cloudflare records (dashboard or API token); confirming the home
IP + router/port-forward. I'll prepare exact configs + commands and hand them over
to run inline (`! <cmd>`).

## Risks & mitigations

- **In-process job lost on restart** → the startup sweep marks it `failed` (no stuck
  `running`); the user re-submits. Acceptable for a PoC; a real queue is the future
  fix if needed.
- **Threadpool saturation** (each running job holds a Starlette threadpool thread for
  up to ~179 s) → fine at PoC concurrency (one tutor); documented, not engineered
  around now.
- **CF still times out a poll?** Polls are sub-second — never near the limit. Only the
  original synchronous call was at risk; it no longer exists.
- **Deploy foot-guns** (baked API base, CSP, DNS propagation) → all enumerated in
  recon + step 2's CSP note; verified live in step 5 before declaring done.

## Out of scope / future

Real task queue; multi-instance/horizontal scale; job cancellation + history UI;
async artifact generation; auth (the whole PoC is no-auth by design).

## Process

Written up as a task-by-task implementation plan (`writing-plans`), executed
subagent-driven (fresh implementer per task, per-task spec+quality review, a
whole-plan review at the end) with TDD — identical to Plans 1–4. `build/poc` stays
unmerged; Chris reviews/merges the whole branch.
