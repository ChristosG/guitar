# Guitar Tutor Copilot — Design

- **Date:** 2026-07-06
- **Status:** Draft for review
- **Working names:** *Fretwork* · *ToneMentor* · *Maestro* (pick later)
- **Primary user (PoC):** one guitar tutor (the client) — this is *his* tool first
- **Builder:** Chris (dev/integrator); the tutor is the client who will use it, gather feedback, then it becomes a product

---

## 1. Summary & Vision

A **personal AI teaching copilot** for a guitar tutor: his knowledge injected and searchable, plus a generative engine that plans multi-lesson courses, preps a specific student's next lesson, and produces real teaching artifacts (chord diagrams, tab/staff, **and** tone-gear visuals). He interacts by editing cards and by chat with tools, where **every state-changing action is a human-approved proposal**.

**North star:** *his second brain + lesson-prep cockpit.* He opens it, sees at a glance what's going on with each student, pulls his own knowledge, and preps a lesson faster and better than he could alone.

**Trajectory:** Web PoC (Dockerized on Chris's PC, exposed on Chris's domain for the client to try) → feedback & fine-tune → productize (local app and/or hosted single-tenant), with a config-flip from local models to Claude.

**Non-negotiables from the brief:** meticulous, never "AI-lazy"; human-in-the-loop everywhere; bilingual (Greek + English); glanceable; start on a low local stack, scale later.

---

## 2. Who the tutor is (domain reframe) & source material

The tutor teaches the full **zero-to-hero** arc (from ~6-year-olds through adults: theory → the instrument → chords/scales → songs), **but his signature and first-product material is guitar *tone, gear & effects***. Evidence: the one book he supplied and **all eight course links** he supplied are tone/gear/effects courses.

**Conclusion (confirmed with Chris):** build **two** flagship domains, both first-class:

1. **Zero-to-Hero · Foundations** — a *kids track* and an *adult track* (theory basics, parts of the guitar, tuning, posture/hand position, open chords, strumming, first melodies & scales, first songs). Where most of his teaching hours live.
2. **Guitar Tone & Gear** — his differentiator, and almost certainly the "~25-hour end-to-end course" he described.

They connect into one story: *a 7-year-old's first note → dialing in Gilmour's lead.*

### 2.1 Source material inventory

- **Book:** *Getting Great Guitar Sounds* — Michael Ross, Hal Leonard, rev. 2nd ed., ~77 pp. Non-technical guide to shaping electric-guitar tone. Parts: The Guitar / The Effects / Tricks of the Trade. Clean two-column prose (great RAG material) + figures. Maps to the intermediate/electric "equipment" stretch.
- **8 course links** (tone/gear/effects): ProAudioExp *Ultimate Guitar Tone School* (David Wills, 8 modules, 7h); Rhett Shull *The Tone Course* (19 lectures, 3h); LickLibrary *Ultimate Guitar Effects Pedals* (Casswell, 9 modules); Pickup Music *Guitar Tone* (Mason Stoops, 4 parts); Berklee Online *Getting Your Guitar Sound* (12-lesson syllabus); TrueFire *Guitar Effects Survival Guide* & *Kings of Tone* (Jeff McErlain); GuitarGearFinder course. *(These are competitor/reference courses, not necessarily his own — but they define the domain and seed the flagship curriculum. The tutor's own written blueprints are still to be collected.)*

### 2.2 The convergent "tone-course spine" (extracted from his references)

These independent pro courses all follow the same **signal-chain-ordered** skeleton, which becomes our seed curriculum for the Tone domain:

| # | Module | Notes |
|---|---|---|
| 0 | **What is tone** | harmonics, distortion; *"tone is in the hands"* (touch & dynamics) |
| 1 | **The Guitar** | types, woods, pickups (single-coil vs humbucker), controls, scale length, setup |
| 2 | **Amps** | tubes, sections, tone stack, gain staging; families: Fender / Marshall / Vox / high-gain |
| 3 | **Speakers & Cabs** | impedance, series/parallel; mic'ing for recording |
| 4 | **Signal flow** | pedal order, buffers, true bypass, FX loop |
| 5 | **Gain fx** | compression, overdrive, distortion, fuzz; *stacking* |
| 6 | **Modulation** | chorus, flanger, phaser, tremolo/vibrato, rotary |
| 7 | **Time fx** | delay (slapback, dotted-⅛, ping-pong), reverb (spring/plate/ambient) |
| 8 | **Other** | wah, EQ, pitch, volume swells |
| 9 | **Rigs** | pedalboard design, wet-dry-wet, 4-cable method |
| 10 | **Iconic tones by era** | Hendrix, Clapton, SRV, Gilmour, The Edge, EVH… |
| 11 | **Context** | live vs studio, genre EQ recipes, amp modeling |

---

## 3. Product shape & primary UX

### 3.1 The cockpit — six surfaces

```
┌──────────────────────────────────────────────────────────────┐
│  🎸 [Studio]         🔎 search his brain…        🌓  GR / EN   │
├───────────┬──────────────────────────────────────────────────┤
│ ▸ Today   │  TODAY · Mon 6 Jul                                │
│ ▸ Students│  ┌────────────────────────────────────────────┐  │
│ ▸ Curricula│ │ 17:00  Giannis (10, yr 1)  ·  Module 3/6    │  │
│ ▸ Knowledge│ │        "open-chord transitions"   [ Prep ▶ ]│  │
│ ▸ Notes   │  ├────────────────────────────────────────────┤  │
│ ▸ Chat    │  │ 18:00  Maria (7, fresh)   ·  Lesson 2       │  │
│           │  │        "first melody on E string" [ Prep ▶ ]│  │
│           │  └────────────────────────────────────────────┘  │
│           │  Recent notes · Jump back in · Ask the assistant │
└───────────┴──────────────────────────────────────────────────┘
```

1. **Today** — the glance: upcoming lessons (who + where they are + one-tap **Prep**), recent notes, a persistent "ask my brain" box.
2. **Students** — roster → student detail: profile, assigned curriculum as an **editable card board**, progress/mastery, **lesson history/log**, notes, *Prep next lesson* / *Chat about this student*.
3. **Curricula** — the library of **blueprints** (templates). Generate, edit as cards, clone, **assign to a student** (clones a personalized instance).
4. **Knowledge** — the Brain: every source (PDF/paste/URL/note), ingestion status, search + preview, domain/language tags, re-index/delete.
5. **Notes** — everything captured (by him or by the AI's `add_note`), filterable by student/lesson; a note can be **promoted into the Brain**.
6. **Chat** — always available, also embedded contextually; tool-powered; **every mutation is an approval card** (HITL).

### 3.2 The killer path — "Prep"

Today → **Prep** on the next lesson → the agent pulls *this student's progress + their curriculum + relevant knowledge* → produces **today's lesson**: objectives, a segment-by-segment plan timed to his 45/60/120-min slot, the exact chords/scales/exercises or tone topics, rendered artifacts, and homework → he tweaks inline or by chat → **approves** → it lands in the lesson log, ready to teach from.

### 3.3 Principles

- **Glanceability:** Today and Student screens answer "what do I teach next" in < 5 seconds.
- **HITL everywhere:** he's always in control; the AI proposes, he disposes.
- **Bilingual:** GR/EN toggle at global + per-student level; every generated artifact stamped with its locale.

---

## 4. Domain model

### 4.1 The abstract Block tree + two planes

Everything curricular is a **Block** — one uniform, **recursive** node type that nests to any depth. Each Block has a *soft, relabelable* `kind` (`course / module / lesson / session / topic`) — **not enforced**, so the tutor can go two levels deep or five. This is the "abstract, I don't know how he'll structure it" requirement.

Two planes over that tree:

| Plane | What | Example |
|---|---|---|
| **Content** ("what") | His material as he thinks of it — pristine master. One giant **25-hour course** Block, a shallow tree, or **auto-seeded from a doc's TOC**. | Whole book → one "Guitar Tone & Gear" course |
| **Delivery** ("how it's taught to *this* student") | The **Sessions** (45–60-min quants) produced by *segmenting* content for a student's slot length, cadence & pace. Becomes the lesson log. | 25h → ~30×50-min sessions (kid) or 12×2h (adult) |

**Key property:** the blueprint is authored *once*; segmentation is a **projection** over it, computed per student — define once, render many.

### 4.2 Entities

- **Tutor/User** — auth (single tutor now; multi-tenant later).
- **Student** — name, birthdate/age, level, instrument (acoustic/electric/classical), start_date, goals, preferred_language, status.
- **Block** — id, parent_id (nullable → tree), order, kind (soft), title, objectives[], body (markdown), est_minutes, language, tags[], artifact_refs[], knowledge_refs[]. A **Curriculum** = a root Block flagged as a program; a **template** vs a **personalized instance**.
- **Assignment** — Student ↔ personalized Curriculum instance.
- **Progress/Mastery** — per (student, objective/skill): `not_started → introduced → practicing → mastered`, updated_at, evidence/notes.
- **LessonLog / Session instance** — a planned/taught lesson: student, date, planned Session block, what actually happened, homework, "next time".
- **KnowledgeSource** — id, type (pdf/url/text/note/image), title, meta, status (ingesting/ready/failed), language, domain tags. → **Chunk** — id, source_id, text, section_path, page, `embedding vector(2560)`.
- **Note** — body, links (student?/lesson?/curriculum?/source?), author (tutor/AI), tags, `promoted_to_knowledge` bool.
- **Artifact** — id, kind, `spec` (JSON), `rendered_svg` (cached), title, tags, source (ai/uploaded), links.
- **ChatSession / Message** — messages + tool_calls + approvals (HITL audit trail).

---

## 5. Architecture & stack

### 5.1 Topology (Docker Compose on Chris's PC → tunneled to his domain)

```
Browser
  ├─ guitar.cgrigoriadis.online     → host nginx → 127.0.0.1:8790  Next.js cockpit (web)
  └─ guitar-api.cgrigoriadis.online → host nginx → 127.0.0.1:8791  FastAPI (REST + SSE)
     Cloudflare orange-cloud → home IP:443 → host nginx · origin-locked · NO auth (PoC)

Next.js cockpit — React/TS · Tailwind+shadcn · light/dark · i18n GR/EN
  renders artifacts CLIENT-side: AlphaTab · svguitar · custom SVG   (HTTP/JSON + SSE)
FastAPI orchestrator (Python)
  ├─ REST: students, curricula, blocks, sessions, notes, artifacts, sources
  ├─ /chat SSE → LangGraph agent (ReAct loop, tool-calling)
  ├─ HITL: LangGraph interrupt() → approval card → resume → commit
  ├─ Ingestion workers: extract → structure-aware chunk → embed → pgvector
  ├─ Artifact validation: music21 + Pydantic spec validators
  └─ LLMProvider seam:  Qwen(local now) ↔ Claude(later)  [one flag]
  │
  ├──────────────┬────────────────────────┬─────────────────────
  ▼              ▼                        ▼
Postgres      vLLM Qwen LLM :6888     vLLM Qwen-Embed :8090
+ pgvector    (existing container)    (existing container)
(state +       swap→ Claude API        Qwen3-Embedding-4B (2560-dim)
 vectors +     via provider iface
 checkpoints)
```

App containers join the existing external `platform-net`, reaching models by alias (`qwen-vllm:6888`, `qwen-emb-vllm:8090`) exactly as the reference stack does.

### 5.2 Stack (decided)

- **Frontend:** Next.js (App Router), TypeScript, Tailwind + **shadcn/ui** (themeable light/dark), **next-intl** (GR/EN). TanStack Query (server state), Zustand (light UI state). Artifacts render client-side.
- **Backend:** **FastAPI + LangGraph** (Python) — reuses the vLLM/LangGraph patterns and the agentic-gotchas playbook; unlocks **`music21`** for server-side music validation. SQLAlchemy + Alembic; pgvector; Pydantic (schemas + guided-JSON + artifact-spec validation).
- **DB:** **Postgres + pgvector** — one store for relational data, embeddings, and LangGraph checkpoints.
- **Provider seam:** `LLMProvider` interface (chat · stream · tools · guided-JSON · embed) with `QwenVLLM` and `Claude` impls; provider-neutral prompts/tools; flag flips it.
- **Deploy:** Docker Compose (`web`, `api`, `worker`, `postgres`); `api` also joins `platform-net` to reach the vLLM containers. Services publish on **loopback ports**, fronted by **host nginx** + Cloudflare per Chris's CG Labs playbook (§5.4). `.env` holds the four model values + provider flag (+ Claude key later).

### 5.3 Model integration specifics (from the vLLM reference)

- LLM `Qwen3.5-9B`, 32k ctx, tool-calling via `--tool-call-parser qwen3_coder`; `chat_template_kwargs.enable_thinking` toggled per call (on only for planning steps).
- Embeddings `qwen3-emb-4b`, 2560-dim; **L2-normalize**; **asymmetric** queries (`Instruct: Given a question, retrieve passages that answer it\nQuery: {q}`); batch 16.
- **Agentic-gotchas playbook baked in:** tool-first imperative system prompt; facts kept out of the prompt (forces retrieval); streaming tool-call arg accumulation by index; hallucinated-tool guard; bounded repair (cap consecutive errors); `MAX_STEPS`; prefix-stable system+tools for KV cache.
- Qwen LLM is a **vision** model (`mm-processor-kwargs`) → we can caption figures/photos during ingest.

### 5.4 Deployment (Cloudflare + host nginx — the CG Labs playbook)

Per `/mnt/nvme2TB/cloudflare/EXPOSE-A-NEW-APP.md`. **Not** a Cloudflare tunnel: Cloudflare orange-cloud proxy → home-IP `:443` (DDNS-managed A record) → **host nginx** (one vhost per subdomain) → docker on `127.0.0.1:PORT`; origin-locked so only Cloudflare edge IPs are answered.

Chat uses **browser SSE**, so there are **two** browser-reachable origins → two single-level subdomains, each its own vhost + cert:

| Subdomain | → loopback | Serves |
|---|---|---|
| `guitar.cgrigoriadis.online` | `127.0.0.1:8790` | Next.js app (web) |
| `guitar-api.cgrigoriadis.online` | `127.0.0.1:8791` | FastAPI (REST + SSE) |

*(ports provisional — must not collide with cglabs 11995 / imatter 58008 / themis 3000–8600.)*

- **No auth** (PoC): single trusted user; app is origin-locked and behind Cloudflare. Real auth deferred.
- **[agent] steps:** containerize (Next.js `output:"standalone"`, loopback ports); create the two DNS records via `/home/chris/dns_resolution` (`ddns.conf` `RECORDS+=…`, `proxied=true`, `./update-dns.sh --force` — never read/print the DNS token); write both vhosts into the app repo's `deploy/nginx/` (the API vhost uses the SSE variant: `proxy_buffering off; proxy_cache off; proxy_set_header Connection ""`, long `proxy_read_timeout`); the **app owns CORS** (allowlist `https://guitar.cgrigoriadis.online`).
- **[sudo → Chris] steps:** install vhosts into `/etc/nginx/sites-{available,enabled}`, `nginx -t && reload`, `certbot --nginx -d <sub>` per subdomain; ensure the origin-lockdown snippet is included; cache rules are per-zone and already applied (`cf-cache-rules.sh cgrigoriadis.online` if ever needed).
- **Next.js caching:** middleware sets `Cache-Control: no-store` on HTML (App-Router `Vary: rsc` trap); `/_next/static/**` stays immutable.
- **Docker networks:** `api` joins both the app network (→ `postgres`) and `platform-net` (→ `qwen-vllm:6888`, `qwen-emb-vllm:8090`).

---

## 6. The four engines

### 6.1 Knowledge Brain (RAG)

- **Ingest (high-variability, best-effort for PoC):** PDF (PyMuPDF), pasted text, **transcripts**, **URL** (readability extraction — his 8 course pages ingest directly), notes, images (VL captioning). Sources may be **EN, GR, or mixed** and of unknown structure; the pipeline degrades gracefully — falls back to sliding-window chunking when no headings are detected.
- **Structure-aware chunking:** detect headings/TOC → chunk on semantic boundaries carrying `section_path` + `page` + `domain tag` (tone / beginner / theory). Metadata does double duty: better retrieval *and* the auto-structure seed for the Curriculum engine.
- **Embed & retrieve** per the reference: Qwen embeddings → L2-normalize → pgvector; asymmetric queries; cosine top-k + filters (source/section/domain/language).
- **Cross-lingual:** store source language, embed as-is (multilingual embeddings), **answer in the target locale** regardless — his English book can ground a Greek answer.
- **Sources UI:** status, chunk preview, domain/language tags, re-index, delete.

### 6.2 Curriculum + Segmentation

- Abstract Block tree + Content/Delivery planes + Assignment/Progress/LessonLog (§4).
- **Generate** (from profile + Brain retrieval) and **auto-structure from a doc** → Content tree, **guided-JSON** so the tree is always schema-valid.
- **Segment:** Block + session length + cadence + pedagogy constraints → Session sequence (guided-JSON). Constraints as prompt + validators: *don't split a concept; review at module seams; kids → shorter segments + games; adults → faster, song-based.*
- **HITL card board:** drag/reorder, split/merge, edit, regenerate-node — every change an approved action. Re-segmentable anytime.

### 6.3 Artifact engine (spec → validate → SVG)

One pipe, many kinds — covering **both** tracks:

| Track | Kinds | Renderer |
|---|---|---|
| Beginner | `chord_diagram` · `scale`/`fretboard` · `tab`/`staff`/`lick` · `strumming` · `progression` | svguitar · custom SVG · **AlphaTab** (with playback) |
| Tone | `signal_chain` · `amp_settings` (knobs) · `pedalboard` · `tone_recipe` · `gear_card` | custom SVG components |

- Every spec is **validated server-side** (`music21` for musical specs; Pydantic for gear/chain specs) so a malformed LLM spec is caught and **bounded-repaired** before render.
- Tutor can **upload his own images** as artifacts.
- Artifacts attach to Sessions/Notes and live in a reusable library.
- **Example `tone_recipe` spec → card:**

```
🎸 STEVIE RAY VAUGHAN — "Texas Flood"
 Guitar  Strat · neck+mid pickups · heavy strings (.013)
 Amp     Fender-style cranked · Gain 7 Bass 6 Mid 6 Treb 6 Rev 3
 Drive   Tube Screamer TS808 as boost · Drive 3 Tone 6 Level 7
 Chain   Guitar → TS808 → amp
 Hands   hard attack, thumb-over-neck, aggressive vibrato
 Listen  "Pride and Joy", "Texas Flood"
```

### 6.4 Agent + Tools + HITL

- LangGraph ReAct loop on the provider seam, full gotchas playbook (§5.3).
- **Tools:**
  - *Read/gen:* `search_knowledge` · `plan_todays_lesson` · `recreate_tone` · `explain_concept`
  - *Artifacts:* `render_chord` · `render_tab` · `render_scale` · `render_signal_chain` · `render_amp_settings` · `render_pedalboard` · `make_tone_recipe`
  - *Mutations (HITL):* `create_student` · `update_student` · `generate_curriculum` · `segment_block` · `update_block` · `assign_curriculum` · `log_progress` · `add_note` · `promote_note_to_knowledge`
- **HITL model:** each *mutating* tool `interrupt()`s instead of applying — the UI shows a **proposed-action card** (preview/diff); tutor approves / edits / rejects; the graph resumes and commits, with a full audit trail. This is Temporal's HITL + durability superpower with **zero extra infra** (just a Postgres checkpoint table). Temporal earns its place later, at multi-tenant scale.
- **Flagship flow — "Recreate this tone":** *"how do I get Gilmour's Comfortably Numb lead?"* → agent (grounded in the Brain) emits a **tone-recipe bundle** (gear card + amp dials + signal chain + technique notes + reference track). Mirrors Module 10 of every reference course.

---

## 7. Cross-cutting

- **i18n (GR/EN):** next-intl for UI; `language` on every content entity; generation in target locale; cross-lingual retrieval.
- **No auth (PoC):** single trusted user; the app is origin-locked behind Cloudflare (direct-to-home-IP → 403). Real multi-tenant auth deferred to productization.
- **Config/secrets:** `.env` (4 model values + provider flag; Claude key later).
- **Observability:** structured logs + a trace of agent steps / tool calls / approvals (feeds the "verify results, judge before moving on" requirement; Logfire-ready).

---

## 8. Seed content (ships with the PoC)

Two expert-crafted flagship curricula, structured so the tutor's real blueprints replace/extend them the moment they arrive:

1. **Zero-to-Hero · Foundations** — *kids track* (short segments, one-finger chords, tab-first, rhythm games, EADGBE mnemonics) and *adult track* (faster theory, open→barre chords, strumming patterns, song-based). Modules: theory basics → the instrument & tuning → posture/hand position → open strings & rhythm → first melodies → first (cowboy) chords → transitions → strumming → power chords → pentatonic → barre chords.
2. **Guitar Tone & Gear** — the §2.2 spine, with a set of **iconic-tone recipe cards** (SRV, Hendrix, Gilmour, The Edge, EVH…) as showcase artifacts.

The book + the 8 course pages are ingested into the Brain as reference knowledge (tagged `tone`).

---

## 9. Build order

Foundations first, then engines bottom-up, then the cockpit that ties them together, then seed content, then deploy. Much of this is **subagent-parallelizable** (independent engines behind clear interfaces).

1. **Foundations** — repo/monorepo layout; Docker Compose (postgres, api, web, worker); FastAPI skeleton; Next.js skeleton + theme + i18n scaffolding; `LLMProvider` seam with `QwenVLLM` impl + health checks against `:6888`/`:8090`; DB schema + Alembic migrations for §4 entities.
2. **Knowledge Brain** — ingestion (PDF/URL/text) → structure-aware chunking → embed → pgvector; retrieval; Sources UI. *Verify:* ingest the book, retrieve grounded answers EN + GR.
3. **Curriculum + Segmentation** — Block tree CRUD; generate; auto-structure-from-doc; segmentation (guided-JSON) + pedagogy validators; card-board UI. *Verify:* book → draft Tone course → segment into sessions.
4. **Artifact engine** — spec schemas + validators; renderers (svguitar, AlphaTab, custom SVGs); library. *Verify:* golden-render tests per kind; a real tone-recipe + a chord diagram + a signal chain.
5. **Agent + Tools + HITL** — LangGraph loop + tools + interrupt/approve; chat UI + approval cards. *Verify:* drive the **real model** end-to-end (see §10) for Prep, Recreate-tone, add-note.
6. **Cockpit integration & polish** — Today/Students/Curricula/Knowledge/Notes wired together; the Prep path; glanceability pass.
7. **Seed content** — author the two flagship curricula + iconic-tone recipes; ingest the book + 8 pages.
8. **Deploy** — per §5.4 (host nginx + Cloudflare, two subdomains, no auth); smoke test on `guitar.cgrigoriadis.online`; hand to the client.

---

## 10. Verification & testing strategy

Chris's explicit ask: meticulous, tested, verify on results, judge before moving on. Approach:

- **TDD where logic is real** (segmentation math/pacing, chunking boundaries, spec validators, retrieval assembly, HITL state transitions).
- **Drive the real model end-to-end — the load-bearing rule.** Per the gotchas: *unit tests pass while the live product never calls a tool.* So every agent/tool feature is verified against the actual vLLM endpoint (tool actually fires, arguments valid, HITL interrupt/resume works), not just mocked.
- **Greek-quality spot-check:** Qwen is reportedly decent in Greek and is the dev/demo default; a small GR eval (curriculum + recipe + chat) confirms it. The provider seam stays ready to flip to Claude Haiku if any surface disappoints.
- **Golden-render tests** for artifacts (spec → SVG snapshot per kind).
- **Retrieval sanity set:** a fixed Q/A set over the book (EN + GR) asserting grounded, cited answers and that fact-questions trigger `search_knowledge`.
- **verification-before-completion:** no feature is "done" until its verification command has been run and its output confirmed.
- **Judge-then-proceed:** after each build phase, review results and decide the next step based on evidence (may reorder later phases).

---

## 11. Provider & cost path

- **Dev & PoC demo:** **local Qwen** (free) is the default for all development — decided. Qwen is decent in Greek.
- **Later:** flip the seam to **Claude (Haiku tier)** — cheap, strong bilingual, native tools + prompt caching; ample for a single-user workload. *Productization note:* a deployed server needs Anthropic **API** access (billed per token) — distinct from a Claude chat subscription; Haiku's low cost makes a flat monthly price for the client easy to model.

## 12. Productization path (later, not now)

- **Local app:** Tauri shell + bundled Python "brain" sidecar, or a hosted single-tenant instance.
- **Multi-tenant:** real auth, per-tutor isolation, billing.
- **Orchestration:** Temporal when long-running jobs go concurrent/multi-tenant.
- **Student-facing layer** on the same spine.

---

## 13. Risks & open questions

- **Qwen Greek quality** — reportedly decent (Chris); spot-checked early (§10), with the Claude-Haiku seam as backstop (§11).
- **Tutor's own blueprints & data** — still to be collected, and expected to be **high-variability** (books, transcripts, notes; EN/GR/mixed). Seed content is a high-quality placeholder, not his verbatim method; the Brain is built to ingest messy heterogeneous text best-effort. His real material sharpens fidelity.
- **Notation edge cases** — AlphaTab/music21 cover a lot; exotic requests may need fallback to "describe + upload image".
- **Student data / minors** — real names & ages of children; local-only for the PoC, but privacy must be designed in before any hosting.
- **Scope** — large; the build order + subagent parallelism + phase-gates keep it tractable. YAGNI enforced (§14).

## 14. Out of scope (YAGNI for the PoC)

Student-facing app · payments/billing · mobile/native packaging · multi-tenant auth · Temporal · advanced analytics · audio input / pitch detection · marketplace of curricula.

## 15. Success criteria (the demo)

The PoC succeeds when, on Chris's domain, the tutor can:

1. See **Today** and, in one tap, **Prep** a named student's next lesson (grounded in their progress + curriculum + his knowledge), then tweak & approve it.
2. **Generate** a "Guitar Tone & Gear" curriculum from the ingested book and **segment** it into ~45–60-min sessions.
3. Ask **"how do I get \<artist\>'s tone?"** and get a correct, rendered **tone-recipe bundle**.
4. Get a correct **chord diagram / tab** for a beginner lesson.
5. Do all the above in **Greek or English**, with **every change approved by him**.
